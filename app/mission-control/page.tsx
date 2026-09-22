'use client'

import { AnimatePresence, motion } from 'framer-motion'
import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, ArrowRight, Check, ChevronRight, Clock3, Code2, Inbox, Layers3, Pause, Play, RefreshCw, RotateCcw, ShieldCheck, Sparkles, TimerReset, X } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type ApprovalRequestDto,
  type LiveOrderIntentDto,
  type OrganizationEventMessage,
  type OrganizationRun,
  type SignoffSnapshot,
  ApiError,
  approveLiveIntent,
  continueRun,
  createRun,
  decideApproval,
  listRuns,
  pauseRun,
  rejectLiveIntent,
  retryRun,
  rerunRun,
} from '@/lib/api'
import { useWebSocketChannel } from '@/lib/ws'

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

function Countdown({ expiresAt, serverTime }: { expiresAt: string; serverTime: string }) {
  const totalWindowMs = 90_000
  const [remainingMs, setRemainingMs] = useState(() => new Date(expiresAt).getTime() - new Date(serverTime).getTime())

  useEffect(() => {
    // Anchored to the server's own clock, not a client-started timer --
    // every new snapshot (every 2s over the sign-off-queue WS) re-derives
    // this from the real expires_at/server_time pair, so a page refresh
    // reconnects and lands on the exact same real deadline.
    setRemainingMs(new Date(expiresAt).getTime() - new Date(serverTime).getTime())
  }, [expiresAt, serverTime])

  useEffect(() => {
    const id = window.setInterval(() => setRemainingMs((value) => Math.max(0, value - 1000)), 1000)
    return () => window.clearInterval(id)
  }, [])

  const seconds = Math.max(0, Math.round(remainingMs / 1000))
  const color = seconds < 10 ? '#f87171' : seconds < 30 ? '#fbbf24' : '#67e8f9'
  const progress = Math.min(100, Math.max(0, (remainingMs / totalWindowMs) * 100))
  return <div className="relative grid size-20 place-items-center rounded-full" style={{ background: `conic-gradient(${color} ${progress}%, rgba(255,255,255,.08) 0)` }}><div className="grid size-[68px] place-items-center rounded-full bg-[#080c1c] text-center"><span className="font-mono text-lg font-semibold" style={{ color }}>{seconds}s</span><span className="text-[9px] uppercase tracking-widest text-muted-foreground">expires</span></div></div>
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
  // Strategy-promotion approvals (POST /approvals/{id}/decide) and live
  // order intent approve/reject (POST /live-trading/intents/{id}/approve|
  // reject) are both SystemAdministrator/RiskManager-only server-side
  // (Build Spec §12's human-in-the-loop sign-off) -- narrower than "not
  // ReadOnlyAuditor". A PortfolioManager must see these disabled, not a
  // button that 403s silently.
  const canDecide = role === 'SystemAdministrator' || role === 'RiskManager'
  const [view, setView] = useState<'kanban' | 'queue'>('kanban')
  const [runs, setRuns] = useState<OrganizationRun[]>([])
  const [selected, setSelected] = useState<OrganizationRun | null>(null)
  const [signoff, setSignoff] = useState<SignoffSnapshot | null>(null)
  const [objective, setObjective] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)

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

  const pendingCount = (signoff?.approvals.length ?? 0) + (signoff?.intents.length ?? 0)

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

  async function handleIntentAction(intent: LiveOrderIntentDto, approve: boolean) {
    setDecisionError(null)
    try {
      if (approve) await approveLiveIntent(intent.id)
      else await rejectLiveIntent(intent.id)
    } catch (err) {
      setDecisionError(err instanceof ApiError ? err.message : 'Failed to record decision')
    }
  }

  return <ShellLayout><div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5"><header className="mission-hero"><div><p className="eyebrow flex items-center gap-2"><Sparkles className="size-3 text-cyan-300" />CONTROL PLANE // MISSION CONTROL</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">Operator command center</h1><p className="mt-2 max-w-2xl text-sm text-muted-foreground">Coordinate the agent organization, review strategy promotions, and keep every live order intent human-supervised.</p></div><div className="flex items-center gap-2 rounded-xl border border-amber-300/20 bg-amber-300/[.06] px-3 py-2 text-xs text-amber-100"><AlertCircle className="size-4" />{pendingCount ? `${pendingCount} item${pendingCount === 1 ? '' : 's'} pending sign-off` : 'No approvals waiting'}</div></header>
    {canCreateRun && <form onSubmit={handleCreateRun} className="flex flex-wrap items-center gap-2 rounded-xl border border-white/10 bg-white/[.02] p-3"><Inbox className="size-4 shrink-0 text-cyan-300" /><input value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="Give the organization a new objective..." className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" /><button type="submit" disabled={creating || !objective.trim()} className="button-primary">{creating ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}Dispatch</button>{createError && <span className="text-xs text-rose-300">{createError}</span>}</form>}
    <div className="flex items-center justify-between border-b border-white/10"><div className="flex gap-1"><button className={`mission-tab ${view === 'kanban' ? 'mission-tab-active' : ''}`} onClick={() => setView('kanban')}><Layers3 className="size-4" />Kanban board</button><button className={`mission-tab ${view === 'queue' ? 'mission-tab-active' : ''}`} onClick={() => setView('queue')}><ShieldCheck className="size-4" />Sign-off queue <span className="badge-count">{pendingCount}</span></button></div><span className="hidden font-mono text-[10px] uppercase tracking-widest text-muted-foreground md:block">human-in-the-loop / paper environment</span></div>
    {view === 'kanban' ? <div className="flex gap-3 overflow-x-auto pb-3">{columns.map((column) => { const columnRuns = runs.filter((run) => columnForRun(run) === column); return <section key={column} className="kanban-column"><div className="mb-3 flex items-center justify-between"><div className="flex items-center gap-2"><span className="size-2 rounded-full bg-cyan-300 shadow-[0_0_12px_currentColor]" /><h2 className="text-xs font-semibold uppercase tracking-wider">{column}</h2></div><span className="font-mono text-[10px] text-muted-foreground">{columnRuns.length.toString().padStart(2, '0')}</span></div><div className="flex min-h-40 flex-col gap-3">{columnRuns.map((run) => <motion.article layoutId={run.id} onClick={() => setSelected(run)} key={run.id} className="task-card"><div className="flex items-start gap-2"><h3 className="flex-1 text-xs font-medium leading-relaxed">{run.objective}</h3><ChevronRight className="size-3 text-muted-foreground" /></div><div className="mt-4 flex items-center justify-between"><div className="avatar-stack">{agentInitials(run).map((agent, i) => <span key={`${run.id}-${agent}-${i}`}>{agent}</span>)}</div><span className="font-mono text-[10px] text-muted-foreground"><Clock3 className="mr-1 inline size-3" />{runElapsed(run)}</span></div><div className="mt-3 flex items-center gap-2"><span className="font-mono text-[9px] uppercase tracking-wide shrink-0" style={{ color: accentFor(run) }}>{runStatusLabel(run)}</span><div className="progress-track flex-1"><span style={{ width: `${runProgress(run)}%`, background: accentFor(run) }} /></div><span className="font-mono text-[9px]" style={{ color: accentFor(run) }}>{runProgress(run)}%</span></div></motion.article>)}</div></section> })}</div> : <SignoffQueue signoff={signoff} canDecide={canDecide} decisionError={decisionError} onDecideApproval={handleDecideApproval} onIntentAction={handleIntentAction} />}
  </div>{selected && <TaskDrawer run={selected} onClose={() => setSelected(null)} onAction={(action) => handleRunAction(selected, action)} canAct={!readOnly} />}</ShellLayout>
}

function SignoffQueue({ signoff, canDecide, decisionError, onDecideApproval, onIntentAction }: { signoff: SignoffSnapshot | null; canDecide: boolean; decisionError: string | null; onDecideApproval: (approval: ApprovalRequestDto, approve: boolean) => void; onIntentAction: (intent: LiveOrderIntentDto, approve: boolean) => void }) {
  const approvals = signoff?.approvals ?? []
  const intents = signoff?.intents ?? []
  return <div className="grid gap-5 xl:grid-cols-[1fr_1.25fr]">
    {decisionError && <div className="xl:col-span-2 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{decisionError}</div>}
    {!canDecide && <div className="xl:col-span-2 rounded-lg border border-amber-300/20 bg-amber-300/[.06] p-3 text-xs text-amber-100">Your role can view the sign-off queue but only SystemAdministrator/RiskManager can approve or reject.</div>}
    <section className="pulse-panel p-5"><div className="flex items-start justify-between"><div><p className="eyebrow">STRATEGY PROMOTIONS</p><p className="mt-1 text-xs text-muted-foreground">Human review before a strategy advances (promotion, live-eligibility).</p></div><TimerReset className="size-4 text-violet-300" /></div><div className="mt-5 flex flex-col gap-3">
      {approvals.length === 0 && <p className="text-xs text-muted-foreground">Nothing pending. Approved and rejected requests drop off this list.</p>}
      {approvals.map((approval) => <div key={approval.id} className="queue-card"><div className="flex items-start justify-between"><div><h3 className="text-sm font-semibold">{approval.subject_type} · {approval.subject_id.slice(0, 8)}</h3><p className="mt-1 text-xs text-muted-foreground">{approval.transition_type} · requested by {approval.requested_by ?? 'unknown'}</p></div><span className="status-chip status-amber">REVIEW</span></div><div className="mt-4 flex flex-wrap gap-2"><button disabled={!canDecide} onClick={() => onDecideApproval(approval, true)} className="button-primary"><Check className="size-3" />Approve</button><button disabled={!canDecide} onClick={() => onDecideApproval(approval, false)} className="button-danger"><X className="size-3" />Reject</button></div></div>)}
    </div></section>
    <section className="pulse-panel p-5"><div className="flex items-start justify-between"><div><p className="eyebrow">LIVE ORDER INTENTS</p><p className="mt-1 text-xs text-muted-foreground">No approved intent can execute without your explicit sign-off.</p></div><Play className="size-4 text-cyan-300" /></div><div className="mt-5 flex flex-col gap-3">
      {intents.length === 0 && <p className="text-xs text-muted-foreground">No live order intents awaiting approval.</p>}
      {signoff && intents.map((intent) => <div key={intent.id} className="queue-card flex flex-col gap-4 md:flex-row md:items-center"><Countdown expiresAt={intent.expires_at} serverTime={signoff.server_time} /><div className="min-w-0 flex-1"><div className="flex items-center gap-2"><h3 className="text-sm font-semibold">{intent.symbol}</h3><span className={`status-chip ${intent.side === 'BUY' ? 'status-green' : 'status-red'}`}>{intent.side}</span></div><p className="mt-1 text-xs text-muted-foreground">{intent.quantity} qty · {intent.intent_type} · generated {new Date(intent.generated_at).toLocaleTimeString()}</p><div className="mt-3 flex gap-2"><button disabled={!canDecide} onClick={() => onIntentAction(intent, true)} className="button-primary"><Check className="size-3" />Approve</button><button disabled={!canDecide} onClick={() => onIntentAction(intent, false)} className="button-danger"><X className="size-3" />Reject</button></div></div></div>)}
    </div></section>
  </div>
}
