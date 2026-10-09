'use client'

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertCircle, ArrowRight, CheckCircle2, Circle, Loader2, MinusCircle, RefreshCw } from 'lucide-react'
import {
  type AgentPipelineEvent,
  type AgentPipelineEventMessage,
  ApiError,
  listAgentPipelineEvents,
  runAgentPipeline,
} from '@/lib/api'
import { useWebSocketChannel } from '@/lib/ws'

// Phase 19 Finding #1: what each agent in the LangGraph pipeline is doing,
// step by step, as it happens. History comes from GET
// /agents/pipeline/events, live updates from /ws/agent-pipeline-events --
// both read the same persisted events the backend records
// (src.orchestration.pipeline_events), never anything fabricated client-side.
// It does NOT show streaming LLM tokens: only Anthropic streams for real, so
// a typing effect would be invented on every other provider. Each step shows
// what is actually known -- when it started, whether an LLM or the
// deterministic fallback answered, how long it took, and what it produced.

const MAX_EVENTS = 400

type StepStatus = 'running' | 'done' | 'failed' | 'skipped'

type StepRow = {
  key: string
  stepIndex: number
  node: string
  agentId: string | null
  status: StepStatus
  durationMs: number | null
  summary: string
  error: string | null
}

type RunView = {
  runId: string
  objective: string | null
  status: 'running' | 'completed' | 'failed'
  steps: StepRow[]
  startedAt: string
  /** Lessons recalled from agent memory before the run; null if memory wasn't consulted. */
  lessonsRecalled: number | null
}

function summarizeOutput(output: unknown): string {
  if (!output || typeof output !== 'object') return ''
  const parts: string[] = []
  for (const [key, value] of Object.entries(output as Record<string, unknown>)) {
    if (value && typeof value === 'object') {
      const v = value as Record<string, unknown>
      if ('source' in v) {
        parts.push(`${key}: ${String(v.source)}${v.provider ? ` via ${String(v.provider)}` : ''}`)
      } else if ('chars' in v) {
        parts.push(`${key}: ${String(v.chars)} chars`)
      } else {
        const inner = Object.entries(v)
          .slice(0, 3)
          .map(([k, x]) => `${k}=${String(x)}`)
          .join(' ')
        parts.push(inner ? `${key}: ${inner}` : key)
      }
    } else {
      parts.push(`${key}: ${String(value)}`)
    }
  }
  return parts.join(' · ')
}

/** Folds one run's raw, ordered events into the rows a person reads: a
 * `step.started` followed by its `step.completed`/`step.failed` becomes a
 * single row, matched on (node, step_index) -- the same node can run several
 * times in one run (validation retries, evaluation rejections). */
export function buildRunView(runId: string, events: AgentPipelineEvent[]): RunView {
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence)
  const steps: StepRow[] = []
  let status: RunView['status'] = 'running'
  let objective: string | null = null
  let lessonsRecalled: number | null = null

  const findStep = (node: string | null, stepIndex: number) =>
    steps.find((s) => s.node === node && s.stepIndex === stepIndex)

  for (const event of ordered) {
    const payload = event.payload ?? {}
    const stepIndex = typeof payload.step_index === 'number' ? payload.step_index : steps.length
    const duration = typeof payload.duration_ms === 'number' ? payload.duration_ms : null

    switch (event.event_type) {
      case 'pipeline.started':
        objective = typeof payload.objective === 'string' ? payload.objective : null
        break
      case 'memory.recalled':
        lessonsRecalled = typeof payload.lessons === 'number' ? payload.lessons : null
        break
      case 'pipeline.completed':
        status = 'completed'
        break
      case 'pipeline.failed':
        status = 'failed'
        break
      case 'step.started':
        steps.push({
          key: `${event.run_id}:${event.sequence}`,
          stepIndex,
          node: event.node ?? '?',
          agentId: event.agent_id,
          status: 'running',
          durationMs: null,
          summary: '',
          error: null,
        })
        break
      case 'step.completed': {
        const row = findStep(event.node, stepIndex)
        if (row) {
          row.status = 'done'
          row.durationMs = duration
          row.summary = summarizeOutput(payload.output)
        }
        break
      }
      case 'step.failed': {
        const row = findStep(event.node, stepIndex)
        if (row) {
          row.status = 'failed'
          row.durationMs = duration
          row.error = typeof payload.error === 'string' ? payload.error : 'failed'
        }
        break
      }
      case 'step.skipped':
        steps.push({
          key: `${event.run_id}:${event.sequence}`,
          stepIndex,
          node: event.node ?? '?',
          agentId: event.agent_id,
          status: 'skipped',
          durationMs: null,
          summary: 'agent disabled',
          error: null,
        })
        break
    }
  }
  return { runId, objective, status, steps, startedAt: ordered[0]?.created_at ?? '', lessonsRecalled }
}

function StatusIcon({ status }: { status: StepStatus }) {
  if (status === 'running') return <Loader2 className="size-3.5 shrink-0 animate-spin text-cyan-300" />
  if (status === 'done') return <CheckCircle2 className="size-3.5 shrink-0 text-emerald-300/80" />
  if (status === 'failed') return <AlertCircle className="size-3.5 shrink-0 text-rose-300" />
  return <MinusCircle className="size-3.5 shrink-0 text-muted-foreground" />
}

export function AgentPipelinePanel({ canRun }: { canRun: boolean }) {
  const [events, setEvents] = useState<AgentPipelineEvent[]>([])
  const [objective, setObjective] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const append = useCallback((incoming: AgentPipelineEvent[]) => {
    setEvents((prev) => {
      const seen = new Set(prev.map((e) => `${e.run_id}:${e.sequence}`))
      const fresh = incoming.filter((e) => !seen.has(`${e.run_id}:${e.sequence}`))
      return fresh.length === 0 ? prev : [...prev, ...fresh].slice(-MAX_EVENTS)
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    listAgentPipelineEvents(MAX_EVENTS)
      .then((history) => {
        if (!cancelled) append(history)
      })
      .catch(() => {
        // best-effort backfill -- the live channel still fills the panel
      })
    return () => {
      cancelled = true
    }
  }, [append])

  useWebSocketChannel<AgentPipelineEventMessage>('/ws/agent-pipeline-events', {}, (message) => {
    if (message.type === 'event') append([message.event])
  })

  // The most recently active run, by its latest event.
  const view = useMemo(() => {
    if (events.length === 0) return null
    const byRun = new Map<string, AgentPipelineEvent[]>()
    for (const event of events) {
      const list = byRun.get(event.run_id) ?? []
      list.push(event)
      byRun.set(event.run_id, list)
    }
    let latestRunId = events[events.length - 1].run_id
    let latestAt = ''
    for (const [runId, list] of byRun) {
      const last = list.reduce((a, b) => (a.created_at >= b.created_at ? a : b))
      if (last.created_at >= latestAt) {
        latestAt = last.created_at
        latestRunId = runId
      }
    }
    return buildRunView(latestRunId, byRun.get(latestRunId) ?? [])
  }, [events])

  async function handleRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!objective.trim()) return
    setRunning(true)
    setError(null)
    try {
      // Resolves when the whole pipeline finishes; its steps arrive live over
      // the WebSocket in the meantime. Reconcile with the stored history
      // afterwards in case the socket missed anything.
      const result = await runAgentPipeline(objective.trim())
      setObjective('')
      append(await listAgentPipelineEvents(MAX_EVENTS, result.run_id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to run the pipeline')
    } finally {
      setRunning(false)
    }
  }

  const statusLabel =
    view?.status === 'running' ? 'RUNNING' : view?.status === 'completed' ? 'COMPLETED' : view?.status === 'failed' ? 'FAILED' : ''

  return (
    <section className="pulse-panel flex flex-col overflow-hidden" aria-label="Agent pipeline steps">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 p-4">
        <div>
          <p className="eyebrow flex items-center gap-2">
            <Activity className="size-3 text-cyan-300" />
            AGENT PIPELINE · LIVE STEPS
            {statusLabel && <span className="font-mono text-[9px] tracking-wider text-muted-foreground">{statusLabel}</span>}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {view?.objective ? `Objective: ${view.objective}` : 'What each agent is doing as the pipeline runs, step by step'}
            {view?.lessonsRecalled ? ` · ${view.lessonsRecalled} lesson${view.lessonsRecalled === 1 ? '' : 's'} recalled from memory` : ''}
          </p>
        </div>
        {canRun && (
          <form onSubmit={handleRun} className="flex min-w-[260px] flex-1 items-center gap-2 rounded-xl border border-white/10 bg-white/[.02] px-3 py-2 md:max-w-xl">
            <input
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              placeholder="Run the agent pipeline on an objective..."
              aria-label="Pipeline objective"
              className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
            />
            <button type="submit" disabled={running || !objective.trim()} className="button-primary">
              {running ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}
              Run pipeline
            </button>
          </form>
        )}
      </div>
      {error && <p className="px-4 pt-3 text-xs text-rose-300">{error}</p>}
      <div className="max-h-[320px] overflow-y-auto">
        {!view && (
          <div className="p-4 text-xs text-muted-foreground">
            No pipeline runs yet. Each step of a run appears here as it starts and finishes.
          </div>
        )}
        {view?.steps.map((step) => (
          <div key={step.key} className="flex items-start gap-3 border-b border-white/5 px-4 py-2.5" data-step-status={step.status}>
            <StatusIcon status={step.status} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs text-foreground">
                <span className="font-mono text-[10px] text-muted-foreground">{String(step.stepIndex + 1).padStart(2, '0')}</span>{' '}
                {step.agentId ?? step.node} <span className="text-muted-foreground">· {step.node}</span>
              </p>
              {(step.summary || step.error) && (
                <p className={`mt-0.5 truncate font-mono text-[10px] ${step.error ? 'text-rose-300' : 'text-muted-foreground'}`}>
                  {step.error ?? step.summary}
                </p>
              )}
            </div>
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
              {step.status === 'running' ? 'running…' : step.durationMs !== null ? `${Math.round(step.durationMs)}ms` : ''}
            </span>
          </div>
        ))}
        {view && view.steps.length === 0 && (
          <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
            <Circle className="size-3" /> Run started — waiting for the first step…
          </div>
        )}
      </div>
    </section>
  )
}
