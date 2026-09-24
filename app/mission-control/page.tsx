'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertCircle, ArrowRight, CalendarClock, Check, CheckCircle2, ChevronRight, Clock3, Inbox, Layers3, MessageSquarePlus, Pause, Play, RefreshCw, RotateCcw, ShieldCheck, Sparkles, TimerReset, Trash2, X } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type ActivityEvent,
  type ActivityFeedMessage,
  type ApprovalRequestDto,
  type OperatorGuidanceDto,
  type OrganizationEventMessage,
  type OrganizationRun,
  type ScheduledJob,
  type SignoffSnapshot,
  ApiError,
  continueRun,
  createOperatorGuidance,
  createRun,
  deactivateOperatorGuidance,
  decideApproval,
  getScheduledJobs,
  listOperatorGuidance,
  listRuns,
  pauseRun,
  retryRun,
  rerunRun,
} from '@/lib/api'
import { useWebSocketChannel } from '@/lib/ws'

// Same severity/relative-time/initials conventions as the Overview page's
// LIVE ACTIVITY FEED panel (app/page.tsx) -- both read the identical real
// /ws/activity-feed channel backed by the hash-chained audit log
// (src/observability/audit_middleware.py), never fabricated events. Kept
// as a local copy rather than a shared import since each page's feed is
// independently filtered and neither page depends on the other.
const CRITICAL_ACTIONS = /reject|trip|breach|fail/i
const WARNING_ACTIONS = /pause|reduce|degrade/i

function severityFor(event: ActivityEvent): 'critical' | 'warning' | 'success' | 'info' {
  if (CRITICAL_ACTIONS.test(event.action)) return 'critical'
  if (WARNING_ACTIONS.test(event.action)) return 'warning'
  if (/approve|fill|complete|pass/i.test(event.action)) return 'success'
  return 'info'
}

function initialsFor(actor: string): string {
  const cleaned = actor.replace(/[^a-zA-Z0-9 ]/g, ' ').trim()
  if (!cleaned) return '??'
  const parts = cleaned.split(/\s+/)
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

function relativeTime(iso: string): string {
  const deltaMs = Date.now() - new Date(iso).getTime()
  const seconds = Math.max(0, Math.round(deltaMs / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return `${hours}h ago`
}

// Scheduler names as src/main.py's lifespan registers them ->
// plain-language labels for the panel (docs/phase19-audit.md Part 1.1's
// finding: 15 real jobs across 6 schedulers existed with zero UI surface).
const SCHEDULER_LABELS: Record<string, string> = {
  market_data: 'Market Data',
  paper_trading: 'Paper Trading',
  live_trading: 'Live Trading',
  audit: 'Audit',
  notification: 'Notifications',
  screener: 'Screener',
  post_trade_review: 'Post-Trade Review',
  investor_reporting: 'Investor Reporting',
}

function schedulerLabel(name: string): string {
  return SCHEDULER_LABELS[name] ?? name
}

function nextRunLabel(iso: string | null): string {
  if (!iso) return 'not scheduled'
  const deltaMs = new Date(iso).getTime() - Date.now()
  if (deltaMs <= 0) return 'due now'
  const minutes = Math.round(deltaMs / 60000)
  if (minutes < 60) return `in ${minutes}m`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `in ${hours}h`
  const days = Math.round(hours / 24)
  return `in ${days}d`
}

function LiveActivityPanel({ activities }: { activities: ActivityEvent[] }) {
  const recent = activities.slice(0, 6)
  return (
    <section className="pulse-panel flex flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-white/8 p-4">
        <div>
          <p className="eyebrow flex items-center gap-2"><Activity className="size-3 text-cyan-300" />LIVE ACTIVITY FEED</p>
          <p className="mt-1 text-xs text-muted-foreground">Real-time agent &amp; operator events, newest first</p>
        </div>
      </div>
      <div className="activity-list max-h-[280px] overflow-y-auto">
        {recent.length === 0 && <div className="p-4 text-xs text-muted-foreground">No activity yet. Actions taken across the organization will appear here in real time.</div>}
        {recent.map((event) => (
          <div key={event.id} className={`activity-row severity-${severityFor(event)}`}>
            <span className="activity-avatar">{initialsFor(event.actor)}</span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs text-foreground">{event.actor} · {event.action} {event.entity_type}</p>
              <p className="mt-1 font-mono text-[9px] uppercase tracking-wider text-muted-foreground">{relativeTime(event.created_at)}</p>
            </div>
            <CheckCircle2 className="size-3 shrink-0 text-emerald-300/70" />
          </div>
        ))}
      </div>
    </section>
  )
}

function AutomationSchedulePanel({ jobs, heartbeatSeconds }: { jobs: ScheduledJob[]; heartbeatSeconds: number | null }) {
  const sorted = [...jobs].sort((a, b) => {
    if (!a.next_run_time) return 1
    if (!b.next_run_time) return -1
    return new Date(a.next_run_time).getTime() - new Date(b.next_run_time).getTime()
  })
  return (
    <section className="pulse-panel flex flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-white/8 p-4">
        <div>
          <p className="eyebrow flex items-center gap-2"><CalendarClock className="size-3 text-cyan-300" />AUTOMATION SCHEDULE</p>
          <p className="mt-1 text-xs text-muted-foreground">Every real scheduled job, read live off the running scheduler</p>
        </div>
      </div>
      <div className="max-h-[280px] overflow-y-auto p-2">
        {sorted.length === 0 && <p className="p-2 text-xs text-muted-foreground">No scheduled jobs reported yet.</p>}
        {sorted.map((job) => (
          <div key={`${job.scheduler}-${job.job_id}`} className="flex items-center justify-between gap-2 rounded-lg px-2 py-2 text-xs hover:bg-white/[.03]">
            <div className="min-w-0">
              <p className="truncate text-foreground">{schedulerLabel(job.scheduler)} <span className="text-muted-foreground">· {job.job_id}</span></p>
              <p className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">{job.trigger}</p>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-cyan-200">{nextRunLabel(job.next_run_time)}</span>
          </div>
        ))}
        {heartbeatSeconds !== null && (
          <div className="flex items-center justify-between gap-2 rounded-lg px-2 py-2 text-xs hover:bg-white/[.03]">
            <div className="min-w-0">
              <p className="truncate text-foreground">Heartbeat <span className="text-muted-foreground">· fixed interval, not an APScheduler job</span></p>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-cyan-200">every {heartbeatSeconds}s</span>
          </div>
        )}
      </div>
    </section>
  )
}

function OperatorGuidancePanel({
  guidance,
  canSubmit,
  message,
  setMessage,
  submitting,
  error,
  onSubmit,
  onDeactivate,
}: {
  guidance: OperatorGuidanceDto[]
  canSubmit: boolean
  message: string
  setMessage: (v: string) => void
  submitting: boolean
  error: string | null
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  onDeactivate: (id: string) => void
}) {
  const active = guidance.filter((g) => g.is_active)
  return (
    <section className="pulse-panel flex flex-col overflow-hidden">
      <div className="border-b border-white/8 p-4">
        <p className="eyebrow flex items-center gap-2"><MessageSquarePlus className="size-3 text-cyan-300" />OPERATOR GUIDANCE</p>
        <p className="mt-1 text-xs text-muted-foreground">Notes the CEO Agent folds into its next planning cycle objective</p>
      </div>
      {canSubmit && (
        <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2 border-b border-white/8 p-3">
          <input
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="e.g. Favor low-volatility instruments this week..."
            className="min-w-0 flex-1 rounded-lg border border-white/10 bg-white/[.02] px-3 py-2 text-xs text-foreground outline-none placeholder:text-muted-foreground"
          />
          <button type="submit" disabled={submitting || !message.trim()} className="button-primary">
            {submitting ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}Add
          </button>
        </form>
      )}
      {error && <div className="border-b border-rose-400/30 bg-rose-400/10 p-2 text-xs text-rose-200">{error}</div>}
      <div className="max-h-[220px] overflow-y-auto p-2">
        {active.length === 0 && <p className="p-2 text-xs text-muted-foreground">No active guidance. The next planning cycle uses the operator's objective as-is.</p>}
        {active.map((g) => (
          <div key={g.id} className="flex items-start justify-between gap-2 rounded-lg px-2 py-2 text-xs hover:bg-white/[.03]">
            <div className="min-w-0 flex-1">
              <p className="text-foreground">{g.message}</p>
              <p className="mt-1 font-mono text-[9px] text-muted-foreground">{g.created_by} · {relativeTime(g.created_at)}</p>
            </div>
            {canSubmit && (
              <button onClick={() => onDeactivate(g.id)} aria-label="Deactivate guidance note" className="icon-button shrink-0">
                <Trash2 className="size-3" />
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

type Column = 'Inbox' | 'Planning' | 'In Progress' | 'Backtesting / Review' | 'Sign-off' | 'Live / Done'
const columns: Column[] = ['Inbox', 'Planning', 'In Progress', 'Backtesting / Review', 'Sign-off', 'Live / Done']

// RunStatus (pending/planning/running/paused/cannot_plan/completed/failed)
// doesn't map 1:1 onto this board's 6 columns -- "Backtesting / Review" has
// no corresponding run status (real backtesting is its own subsystem, see
// the Backtests page) and is intentionally always empty here; a paused run
// is a human decision point, the closest real fit for "Sign-off".
function columnForRun(run: OrganizationRun): Column {
  switch (run.status) {
    case 'pending':
      return 'Inbox'
    case 'planning':
      return 'Planning'
    case 'running':
      return 'In Progress'
    case 'paused':
      return 'Sign-off'
    default:
      return 'Live / Done' // cannot_plan, completed, failed -- all terminal
  }
}

function runProgress(run: OrganizationRun): number {
  if (run.tasks.length === 0) return run.status === 'completed' ? 100 : 0
  const succeeded = run.tasks.filter((t) => t.status === 'succeeded').length
  return Math.round((succeeded / run.tasks.length) * 100)
}

function runElapsed(run: OrganizationRun): string {
  const start = run.started_at ? new Date(run.started_at) : new Date(run.created_at)
  const end = run.completed_at ? new Date(run.completed_at) : new Date()
  const totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes.toString().padStart(2, '0')}m ${seconds.toString().padStart(2, '0')}s`
}

function agentInitials(run: OrganizationRun): string[] {
  const capabilities = Array.from(new Set(run.tasks.map((t) => t.capability)))
  return capabilities.slice(0, 4).map((c) => c.slice(0, 2).toUpperCase())
}

function accentFor(run: OrganizationRun): string {
  if (run.status === 'failed' || run.status === 'cannot_plan') return '#f87171'
  if (run.status === 'completed') return '#4ade80'
  if (run.status === 'paused') return '#fbbf24'
  return '#67e8f9'
}

// Text label for the same run states accentFor() colors -- several terminal
// statuses (completed / failed / cannot_plan) land in the same "Live / Done"
// kanban column, so the card needs a status word, not just an accent color,
// to stay legible for colorblind users / forced-grayscale displays (WCAG 1.4.1).
function runStatusLabel(run: OrganizationRun): string {
  switch (run.status) {
    case 'pending': return 'Pending'
    case 'planning': return 'Planning'
    case 'running': return 'Running'
    case 'paused': return 'Paused'
    case 'cannot_plan': return 'Cannot plan'
    case 'completed': return 'Completed'
    case 'failed': return 'Failed'
    default: return run.status
  }
}

function TaskDrawer({ run, onClose, onAction, canAct }: { run: OrganizationRun; onClose: () => void; onAction: (action: 'pause' | 'continue' | 'retry' | 'rerun') => void; canAct: boolean }) {
  return <AnimatePresence><motion.aside initial={{ x: 440 }} animate={{ x: 0 }} exit={{ x: 440 }} className="fixed right-0 top-0 z-[60] flex h-full w-full max-w-[440px] flex-col border-l border-cyan-300/15 bg-[#080b1b]/95 p-6 shadow-2xl shadow-cyan-950/30 backdrop-blur-2xl"><div className="flex items-start justify-between"><div><p className="eyebrow">RUN INSPECTOR</p><h2 className="mt-2 text-xl font-semibold">{run.objective}</h2><p className="mt-1 font-mono text-xs text-cyan-200">RUN-{run.id.slice(0, 8).toUpperCase()} · {runElapsed(run)} · {run.status}</p></div><button onClick={onClose} aria-label="Close task inspector" className="icon-button"><X className="size-4" /></button></div>
    {run.error && <div className="mt-4 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{run.error}</div>}
    <div className="mt-6"><p className="eyebrow">TASK GRAPH ({run.tasks.length})</p><div className="mt-3 flex flex-col gap-3">{run.tasks.length === 0 && <p className="text-xs text-muted-foreground">No tasks planned yet.</p>}{run.tasks.map((task) => <div key={task.id} className="flex gap-3 text-xs"><span className={`mt-1 size-1.5 shrink-0 rounded-full ${task.status === 'succeeded' ? 'bg-emerald-300' : task.status === 'failed' ? 'bg-rose-400' : task.status === 'running' ? 'bg-cyan-300' : 'bg-white/30'}`} /><span className="text-muted-foreground">{task.name} <small className="ml-2 font-mono text-[9px] text-cyan-200/60">{task.status}</small>{task.last_error && <span className="mt-1 block text-rose-300">{task.last_error}</span>}</span></div>)}</div></div>
    {canAct && <div className="mt-6 flex flex-wrap gap-2">
      {run.status === 'running' && <button onClick={() => onAction('pause')} className="button-secondary"><Pause className="size-3" />Pause</button>}
      {run.status === 'paused' && <button onClick={() => onAction('continue')} className="button-primary"><Play className="size-3" />Continue</button>}
      {run.status === 'failed' && run.failure_class === 'transient' && <button onClick={() => onAction('retry')} className="button-secondary"><RotateCcw className="size-3" />Retry</button>}
      {(run.status === 'completed' || run.status === 'failed') && <button onClick={() => onAction('rerun')} className="button-secondary"><RefreshCw className="size-3" />Rerun</button>}
    </div>}
  </motion.aside></AnimatePresence>
}

export default function MissionControlPage() {
  const { role } = useAuth()
  const readOnly = role === 'ReadOnlyAuditor'
  const canCreateRun = role === 'SystemAdministrator' || role === 'PortfolioManager'
  // Strategy-promotion approvals (POST /approvals/{id}/decide) are
  // SystemAdministrator/RiskManager-only server-side -- narrower than
  // "not ReadOnlyAuditor". A PortfolioManager must see this disabled,
  // not a button that 403s silently. (Live order intents no longer go
  // through a sign-off step at all -- Phase 18, Non-Negotiable Rule #1 --
  // see the Strategies page's per-strategy autonomy switch instead.)
  const canDecide = role === 'SystemAdministrator' || role === 'RiskManager'
  const [view, setView] = useState<'kanban' | 'queue'>('kanban')
  const [runs, setRuns] = useState<OrganizationRun[]>([])
  const [selected, setSelected] = useState<OrganizationRun | null>(null)
  const [signoff, setSignoff] = useState<SignoffSnapshot | null>(null)
  const [objective, setObjective] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const canGuide = !readOnly

  const [activities, setActivities] = useState<ActivityEvent[]>([])
  const [scheduledJobs, setScheduledJobs] = useState<ScheduledJob[]>([])
  const [heartbeatSeconds, setHeartbeatSeconds] = useState<number | null>(null)
  const [guidance, setGuidance] = useState<OperatorGuidanceDto[]>([])
  const [guidanceMessage, setGuidanceMessage] = useState('')
  const [guidanceSubmitting, setGuidanceSubmitting] = useState(false)
  const [guidanceError, setGuidanceError] = useState<string | null>(null)

  const refreshSchedule = useCallback(async () => {
    try {
      const data = await getScheduledJobs()
      setScheduledJobs(data.jobs)
      setHeartbeatSeconds(data.heartbeat_interval_seconds)
    } catch {
      // best-effort poll -- panel just keeps its last known state
    }
  }, [])

  const refreshGuidance = useCallback(async () => {
    try {
      setGuidance(await listOperatorGuidance())
    } catch {
      // best-effort poll
    }
  }, [])

  useEffect(() => {
    refreshSchedule()
    refreshGuidance()
    const interval = setInterval(refreshSchedule, 30000)
    return () => clearInterval(interval)
  }, [refreshSchedule, refreshGuidance])

  useWebSocketChannel<ActivityFeedMessage>('/ws/activity-feed', {}, (message) => {
    if (message.type === 'backfill') {
      setActivities(message.events.slice(0, 30))
    } else if (message.type === 'event') {
      setActivities((prev) => [message.event, ...prev].slice(0, 30))
    }
  })

  async function handleGuidanceSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!guidanceMessage.trim()) return
    setGuidanceSubmitting(true)
    setGuidanceError(null)
    try {
      await createOperatorGuidance(guidanceMessage.trim())
      setGuidanceMessage('')
      await refreshGuidance()
    } catch (err) {
      setGuidanceError(err instanceof ApiError ? err.message : 'Failed to add guidance')
    } finally {
      setGuidanceSubmitting(false)
    }
  }

  async function handleGuidanceDeactivate(id: string) {
    setGuidanceError(null)
    try {
      await deactivateOperatorGuidance(id)
      await refreshGuidance()
    } catch (err) {
      setGuidanceError(err instanceof ApiError ? err.message : 'Failed to deactivate guidance')
    }
  }

  const refreshRuns = useCallback(async () => {
    try {
      const data = await listRuns()
      setRuns(data)
      setSelected((prev) => (prev ? (data.find((r) => r.id === prev.id) ?? null) : null))
    } catch {
      // best-effort poll -- next tick / WS-triggered refresh retries
    }
  }, [])

  useEffect(() => {
    refreshRuns()
    const interval = setInterval(refreshRuns, 8000)
    return () => clearInterval(interval)
  }, [refreshRuns])

  useWebSocketChannel<OrganizationEventMessage>('/ws/organization-events', {}, () => {
    refreshRuns()
  })

  useWebSocketChannel<SignoffSnapshot>('/ws/sign-off-queue', {}, setSignoff)

  // signoff.intents is always [] since Phase 18 -- no LiveOrderIntent is
  // ever pending_approval anymore, see SignoffSnapshot's own type comment.
  const pendingCount = signoff?.approvals.length ?? 0

  async function handleCreateRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!objective.trim()) return
    setCreating(true)
    setCreateError(null)
    try {
      await createRun(objective.trim())
      setObjective('')
      await refreshRuns()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : 'Failed to create run')
    } finally {
      setCreating(false)
    }
  }

  async function handleRunAction(run: OrganizationRun, action: 'pause' | 'continue' | 'retry' | 'rerun') {
    const fn = { pause: pauseRun, continue: continueRun, retry: retryRun, rerun: rerunRun }[action]
    try {
      await fn(run.id)
      await refreshRuns()
    } catch {
      // surfaced implicitly -- the run's status won't change and the card stays put
    }
  }

  async function handleDecideApproval(approval: ApprovalRequestDto, approve: boolean) {
    setDecisionError(null)
    try {
      await decideApproval(approval.id, approve)
    } catch (err) {
      setDecisionError(err instanceof ApiError ? err.message : 'Failed to record decision')
    }
  }

  return <ShellLayout><div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5"><header className="mission-hero"><div><p className="eyebrow flex items-center gap-2"><Sparkles className="size-3 text-cyan-300" />CONTROL PLANE // MISSION CONTROL</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">Operator command center</h1><p className="mt-2 max-w-2xl text-sm text-muted-foreground">Coordinate the agent organization and review strategy promotions -- live order intents now execute autonomously per strategy, see the Strategies page.</p></div><div className="flex items-center gap-2 rounded-xl border border-amber-300/20 bg-amber-300/[.06] px-3 py-2 text-xs text-amber-100"><AlertCircle className="size-4" />{pendingCount ? `${pendingCount} item${pendingCount === 1 ? '' : 's'} pending sign-off` : 'No approvals waiting'}</div></header>
    {canCreateRun && <form onSubmit={handleCreateRun} className="flex flex-wrap items-center gap-2 rounded-xl border border-white/10 bg-white/[.02] p-3"><Inbox className="size-4 shrink-0 text-cyan-300" /><input value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="Give the organization a new objective..." className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" /><button type="submit" disabled={creating || !objective.trim()} className="button-primary">{creating ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}Dispatch</button>{createError && <span className="text-xs text-rose-300">{createError}</span>}</form>}
    <div className="grid gap-4 xl:grid-cols-3">
      <LiveActivityPanel activities={activities} />
      <AutomationSchedulePanel jobs={scheduledJobs} heartbeatSeconds={heartbeatSeconds} />
      <OperatorGuidancePanel
        guidance={guidance}
        canSubmit={canGuide}
        message={guidanceMessage}
        setMessage={setGuidanceMessage}
        submitting={guidanceSubmitting}
        error={guidanceError}
        onSubmit={handleGuidanceSubmit}
        onDeactivate={handleGuidanceDeactivate}
      />
    </div>
    <div className="flex items-center justify-between border-b border-white/10"><div className="flex gap-1"><button className={`mission-tab ${view === 'kanban' ? 'mission-tab-active' : ''}`} onClick={() => setView('kanban')}><Layers3 className="size-4" />Kanban board</button><button className={`mission-tab ${view === 'queue' ? 'mission-tab-active' : ''}`} onClick={() => setView('queue')}><ShieldCheck className="size-4" />Sign-off queue <span className="badge-count">{pendingCount}</span></button></div><span className="hidden font-mono text-[10px] uppercase tracking-widest text-muted-foreground md:block">human-in-the-loop / paper environment</span></div>
    {view === 'kanban' ? <div className="flex gap-3 overflow-x-auto pb-3">{columns.map((column) => { const columnRuns = runs.filter((run) => columnForRun(run) === column); return <section key={column} className="kanban-column"><div className="mb-3 flex items-center justify-between"><div className="flex items-center gap-2"><span className="size-2 rounded-full bg-cyan-300 shadow-[0_0_12px_currentColor]" /><h2 className="text-xs font-semibold uppercase tracking-wider">{column}</h2></div><span className="font-mono text-[10px] text-muted-foreground">{columnRuns.length.toString().padStart(2, '0')}</span></div><div className="flex min-h-40 flex-col gap-3">{columnRuns.map((run) => <motion.article layoutId={run.id} onClick={() => setSelected(run)} key={run.id} className="task-card"><div className="flex items-start gap-2"><h3 className="flex-1 text-xs font-medium leading-relaxed">{run.objective}</h3><ChevronRight className="size-3 text-muted-foreground" /></div><div className="mt-4 flex items-center justify-between"><div className="avatar-stack">{agentInitials(run).map((agent, i) => <span key={`${run.id}-${agent}-${i}`}>{agent}</span>)}</div><span className="font-mono text-[10px] text-muted-foreground"><Clock3 className="mr-1 inline size-3" />{runElapsed(run)}</span></div><div className="mt-3 flex items-center gap-2"><span className="font-mono text-[9px] uppercase tracking-wide shrink-0" style={{ color: accentFor(run) }}>{runStatusLabel(run)}</span><div className="progress-track flex-1"><span style={{ width: `${runProgress(run)}%`, background: accentFor(run) }} /></div><span className="font-mono text-[9px]" style={{ color: accentFor(run) }}>{runProgress(run)}%</span></div></motion.article>)}</div></section> })}</div> : <SignoffQueue signoff={signoff} canDecide={canDecide} decisionError={decisionError} onDecideApproval={handleDecideApproval} />}
  </div>{selected && <TaskDrawer run={selected} onClose={() => setSelected(null)} onAction={(action) => handleRunAction(selected, action)} canAct={!readOnly} />}</ShellLayout>
}

function SignoffQueue({ signoff, canDecide, decisionError, onDecideApproval }: { signoff: SignoffSnapshot | null; canDecide: boolean; decisionError: string | null; onDecideApproval: (approval: ApprovalRequestDto, approve: boolean) => void }) {
  const approvals = signoff?.approvals ?? []
  return <div className="grid gap-5 xl:grid-cols-[1fr_1.25fr]">
    {decisionError && <div className="xl:col-span-2 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{decisionError}</div>}
    {!canDecide && <div className="xl:col-span-2 rounded-lg border border-amber-300/20 bg-amber-300/[.06] p-3 text-xs text-amber-100">Your role can view the sign-off queue but only SystemAdministrator/RiskManager can approve or reject.</div>}
    <section className="pulse-panel p-5"><div className="flex items-start justify-between"><div><p className="eyebrow">STRATEGY PROMOTIONS</p><p className="mt-1 text-xs text-muted-foreground">Human review before a strategy advances (promotion, live-eligibility).</p></div><TimerReset className="size-4 text-violet-300" /></div><div className="mt-5 flex flex-col gap-3">
      {approvals.length === 0 && <p className="text-xs text-muted-foreground">Nothing pending. Approved and rejected requests drop off this list.</p>}
      {approvals.map((approval) => <div key={approval.id} className="queue-card"><div className="flex items-start justify-between"><div><h3 className="text-sm font-semibold">{approval.subject_type} · {approval.subject_id.slice(0, 8)}</h3><p className="mt-1 text-xs text-muted-foreground">{approval.transition_type} · requested by {approval.requested_by ?? 'unknown'}</p></div><span className="status-chip status-amber">REVIEW</span></div><div className="mt-4 flex flex-wrap gap-2"><button disabled={!canDecide} onClick={() => onDecideApproval(approval, true)} className="button-primary"><Check className="size-3" />Approve</button><button disabled={!canDecide} onClick={() => onDecideApproval(approval, false)} className="button-danger"><X className="size-3" />Reject</button></div></div>)}
    </div></section>
    <section className="pulse-panel p-5"><div className="flex items-start justify-between"><div><p className="eyebrow">LIVE ORDER INTENTS</p><p className="mt-1 text-xs text-muted-foreground">Phase 18: no per-order sign-off exists anymore. Once a strategy's autonomy switch is on (Strategies page), every real signal submits straight to the broker -- only the Kill Switch, Compliance Checker, Correlation Constraint, and that subscription's own standing rate/notional caps can stop it.</p></div><Play className="size-4 text-cyan-300" /></div><div className="mt-5 flex flex-col gap-2 text-xs text-muted-foreground"><span>See the Strategies page to enroll a subscription and flip its autonomy switch.</span><span>See Orders &amp; Trades for the full, real-time history of every autonomously-resolved intent.</span></div></section>
  </div>
}
