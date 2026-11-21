'use client'

import dynamic from 'next/dynamic'
import { useCallback, useMemo, useState } from 'react'
import { Activity, AlertTriangle, Bot, Cpu, LineChart, MemoryStick, Radio, ShieldAlert, Timer, TrendingUp, Wifi, Zap } from 'lucide-react'
import { roleLabel, useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'
import {
  type ActivityEvent,
  type ActivityFeedMessage,
  type AgentSummary,
  type LiveTradingSubscriptionDto,
  type OrganizationRun,
  type SignoffSnapshot,
  type SystemVitals,
  type TodaysPaperPnl,
  type UnrealizedPaperPnl,
  getAgents,
  getSystemVitals,
  getTodaysPaperPnl,
  getUnrealizedPaperPnl,
  listLiveSubscriptions,
  listRuns,
} from '@/lib/api'
import { EquityPanel } from '@/components/equity/equity-panel'
import { useConsoleState } from '@/components/shell/console-state'
import { ErrorNotice } from '@/components/ui/error-notice'
import type { PulseState } from '@/components/overview/pulse-scene'
import { describeEvent, entityLabel, isRoutineEvent } from '@/lib/activity-feed'
import { usePolling } from '@/lib/use-polling'
import { useWebSocketChannel } from '@/lib/ws'

// three.js loads after the page is interactive; until then, and if WebGL never comes, a still orb
// in the state's colour (`.pulse-orb-still`) holds its place.
const PulseScene = dynamic(() => import('@/components/overview/pulse-scene'), {
  ssr: false,
  loading: () => <div className="pulse-orb-still" aria-hidden="true" />,
})

const ROUTINE_CHIP = 'Sign-ins and webhooks'

const stateConfig: Record<PulseState, { color: string; glow: string; copy: string }> = {
  IDLE: { color: '#67e8f9', glow: 'rgba(34,211,238,.35)', copy: 'All systems nominal' },
  RESEARCHING: { color: '#60a5fa', glow: 'rgba(59,130,246,.38)', copy: 'Agents are scanning market signals' },
  EXECUTING: { color: '#4ade80', glow: 'rgba(74,222,128,.38)', copy: 'Paper execution mesh active' },
  'AWAITING APPROVAL': { color: '#fbbf24', glow: 'rgba(251,191,36,.42)', copy: 'Human sign-off required' },
  'RISK ALERT': { color: '#f87171', glow: 'rgba(248,113,113,.52)', copy: 'Risk perimeter breached' },
}

const NON_TERMINAL_RUN_STATUSES = new Set(['pending', 'planning', 'running'])
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

function ProviderHealthRow({ entries }: { entries: SystemVitals['llm']['provider_health'] }) {
  if (entries.length === 0) {
    return <p className="mt-3 text-[10px] text-muted-foreground">No provider has completed a call yet this process.</p>
  }
  return (
    <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-muted-foreground">
      {entries.map((entry) => {
        const failedMoreRecently =
          entry.last_failure_at !== null &&
          (entry.last_success_at === null || entry.last_failure_at > entry.last_success_at)
        const dotClass = failedMoreRecently
          ? 'bg-rose-400'
          : entry.last_success_at !== null
            ? 'bg-emerald-400'
            : 'bg-muted-foreground/40'
        const title = failedMoreRecently
          ? `last failure: ${entry.last_failure_at}`
          : entry.last_success_at !== null
            ? `last success: ${entry.last_success_at}`
            : 'not called yet this process'
        return (
          <span key={entry.provider} title={title} className="inline-flex items-center gap-1">
            <span className={`size-1.5 rounded-full ${dotClass}`} />
            {entry.provider}
            {entry.served_as_fallback ? ' (fallback)' : ''}
          </span>
        )
      })}
    </div>
  )
}

function Vitals() {
  const { data: vitals, error, refresh } = usePolling(getSystemVitals, { intervalMs: 15_000 })

  const totalTokensToday = vitals
    ? Object.values(vitals.llm.token_usage_today).reduce((sum, n) => sum + n, 0)
    : null
  const p95 = vitals?.latency.order_dispatch_p95_ms ?? null
  const p50 = vitals?.latency.order_dispatch_p50_ms ?? null
  const overBudget = p95 !== null && vitals !== null && p95 > vitals.latency.budget_ms

  return <section className="pulse-panel p-5"><div className="flex items-center justify-between"><div><p className="eyebrow">SYSTEM VITALS</p><p className="mt-1 text-xs text-muted-foreground">Infrastructure telemetry — current, not historical</p></div><Wifi className="size-4 text-emerald-300" /></div>{error && <ErrorNotice className="mt-3" message={`System vitals are not updating: ${error}`} onRetry={() => void refresh()} />}<div className="mt-5 grid gap-3 sm:grid-cols-2"><div className="vital-tile"><div className="flex justify-between text-xs"><span>LLM active provider</span><span className="text-foreground">{vitals?.llm.active_provider ?? '—'}</span></div><ProviderHealthRow entries={vitals?.llm.provider_health ?? []} /></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Token usage</span><span className="text-foreground">{totalTokensToday === null ? '—' : totalTokensToday.toLocaleString()}</span></div><p className="mt-3 text-[10px] text-muted-foreground">Today (IST calendar day) — resets at midnight IST, not "since server start"</p></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Dispatch latency</span><span className={overBudget ? 'text-rose-300' : 'text-foreground'}>{p95 === null ? 'no recent dispatches' : `p95 ${p95.toFixed(0)}ms`}</span></div><p className="mt-2 font-mono text-[10px] text-muted-foreground">{p50 !== null ? `p50 ${p50.toFixed(0)}ms · ` : ''}trailing {vitals?.latency.window ?? '5m'} · budget {vitals?.latency.budget_ms ?? '—'}ms</p></div><div className="vital-tile"><div className="flex items-center justify-between text-xs"><span className="flex items-center gap-2"><Cpu className="size-3 text-violet-300" /> Host metrics</span><span className="text-foreground">{vitals ? `${vitals.host.cpu_percent.toFixed(0)}% CPU` : '—'}</span></div><div className="mt-3 flex items-center gap-2 text-[10px] text-muted-foreground"><MemoryStick className="size-3 text-cyan-300" />{vitals ? `${vitals.host.memory_percent.toFixed(0)}% mem · ${vitals.host.memory_used_mb.toFixed(0)}MB used — current` : 'Loading…'}</div></div></div></section>
}

export default function Home() {
  const { role } = useAuth()
  const { reduceMotion } = usePreferences() // the manual power-save toggle or the OS's reduced-motion setting
  const isReadOnly = role === 'ReadOnlyAuditor'

  const [activities, setActivities] = useState<ActivityEvent[]>([])
  const [filter, setFilter] = useState('All')
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [runs, setRuns] = useState<OrganizationRun[]>([])
  const {
    killSwitch: { paper: paperKillSwitch, live: liveKillSwitch },
    marketHours,
    refresh: refreshConsoleState,
  } = useConsoleState()
  const [signoff, setSignoff] = useState<SignoffSnapshot | null>(null)
  const [todaysPnl, setTodaysPnl] = useState<TodaysPaperPnl | null>(null)
  const [unrealizedPnl, setUnrealizedPnl] = useState<UnrealizedPaperPnl | null>(null)
  const [liveSubscriptions, setLiveSubscriptions] = useState<LiveTradingSubscriptionDto[]>([])

  const load = useCallback(async () => {
    const [agentsRes, runsRes, pnl, unrealizedPnlRes, liveSubsRes] = await Promise.allSettled([
      getAgents(),
      // Only the runs in progress, however old: that is what the live count and the status line need
      listRuns({ active: true, limit: 200 }),
      getTodaysPaperPnl(),
      getUnrealizedPaperPnl(),
      listLiveSubscriptions(),
    ])
    if (agentsRes.status === 'fulfilled') setAgents(agentsRes.value)
    if (runsRes.status === 'fulfilled') setRuns(runsRes.value)
    if (pnl.status === 'fulfilled') setTodaysPnl(pnl.value)
    if (unrealizedPnlRes.status === 'fulfilled') setUnrealizedPnl(unrealizedPnlRes.value)
    if (liveSubsRes.status === 'fulfilled') setLiveSubscriptions(liveSubsRes.value)
  }, [])

  const { refresh: refreshOverview } = usePolling(load, { intervalMs: 20_000 })
  const refreshAll = useCallback(() => Promise.all([refreshOverview(), refreshConsoleState()]), [refreshOverview, refreshConsoleState])

  useWebSocketChannel<ActivityFeedMessage>('/ws/activity-feed', {}, (message) => {
    if (message.type === 'backfill') {
      setActivities(message.events.slice(-40).reverse())
    } else {
      setActivities((prev) => [message.event, ...prev].slice(0, 40))
      // Kill-switch trips/resets land here via the audit log within ~1.5s
      // (src/api/routes/websockets.py), but paperKillSwitch/liveKillSwitch
      // only otherwise refresh on the 20s poll below -- without this, the
      // Pulse orb and KILL SWITCH strip could show a stale "armed" state
      // for up to 20s after a real trip. Refetch immediately instead of
      // waiting for the next tick.
      if (message.event.entity_type === 'kill-switch') void refreshAll()
    }
  })

  useWebSocketChannel<SignoffSnapshot>('/ws/sign-off-queue', {}, setSignoff)

  // signoff.intents is always [] since Phase 18 -- no LiveOrderIntent is
  // ever pending_approval anymore.
  const pendingSignoffs = signoff?.approvals.length ?? 0
  const autonomousSubscriptions = liveSubscriptions.filter((s) => s.autonomous_trading_enabled && s.is_active)
  const killSwitchTripped = Boolean(paperKillSwitch?.tripped || liveKillSwitch?.tripped)
  // A switch we could not read is not a switch that is off: say so instead of "armed".
  const killSwitchUnknown = !killSwitchTripped && (paperKillSwitch === null || liveKillSwitch === null)
  const liveRunsCount = runs.filter((r) => NON_TERMINAL_RUN_STATUSES.has(r.status)).length
  const activeAgentsCount = agents.filter((a) => a.enabled).length

  const state: PulseState = killSwitchTripped
    ? 'RISK ALERT'
    : pendingSignoffs > 0
      ? 'AWAITING APPROVAL'
      : runs.some((r) => r.status === 'running')
        ? 'EXECUTING'
        : runs.some((r) => r.status === 'planning' || r.status === 'pending')
          ? 'RESEARCHING'
          : 'IDLE'

  const config = stateConfig[state]
  // Sign-ins and webhook deliveries are left out unless their own chip is chosen; the Audit Log keeps all.
  const filteredActivities =
    filter === 'All'
      ? activities.filter((a) => !isRoutineEvent(a))
      : filter === ROUTINE_CHIP
        ? activities.filter((a) => isRoutineEvent(a))
        : activities.filter((a) => !isRoutineEvent(a) && entityLabel(a.entity_type) === filter)
  const entityTypes = useMemo(() => {
    const labels = Array.from(new Set(activities.filter((a) => !isRoutineEvent(a)).map((a) => entityLabel(a.entity_type))))
    return ['All', ...labels, ...(activities.some((a) => isRoutineEvent(a)) ? [ROUTINE_CHIP] : [])]
  }, [activities])

  return <><div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
    <div className={`autonomy-banner ${autonomousSubscriptions.length > 0 ? 'autonomy-banner-active' : 'autonomy-banner-idle'}`}>
      {autonomousSubscriptions.length > 0 ? <Zap /> : <ShieldAlert />}
      <div>
        <strong>{autonomousSubscriptions.length > 0 ? `LIVE AUTONOMOUS TRADING IS ON — ${autonomousSubscriptions.length} strateg${autonomousSubscriptions.length === 1 ? 'y' : 'ies'} trading real capital` : 'No strategies are trading autonomously right now'}</strong>
        <small>{autonomousSubscriptions.length > 0 ? `${autonomousSubscriptions.map((s) => s.symbol).join(', ')} — every real signal submits straight to the broker, gated only by the Kill Switch, Compliance Checker, Correlation Constraint, and each subscription's own standing rate/notional cap.` : 'Every live order requires a strategy’s master autonomy switch to be explicitly enabled first (Strategies page) — no per-order human approval exists once it is.'}</small>
      </div>
    </div>
    <section className={`pulse-hero pulse-state-${state.toLowerCase().replaceAll(' ', '-')}`} style={{ '--pulse-color': config.color, '--pulse-glow': config.glow } as React.CSSProperties}>
      <div className="pulse-header"><div><p className="eyebrow">TRADINGOS // ORGANIZATION PULSE</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">The intelligence layer is <span style={{ color: config.color }}>{state.toLowerCase()}.</span></h1><p className="mt-2 text-sm text-muted-foreground">{config.copy} · NSE / BSE synchronized</p></div></div>
      <div className="pulse-orb"><PulseScene state={state} color={config.color} reduceMotion={reduceMotion} /><div className="pulse-status" style={{ borderColor: config.color, color: config.color }}><span className="status-dot" />{state}<small>{state === 'AWAITING APPROVAL' ? `${pendingSignoffs} pending` : state === 'RISK ALERT' ? 'PERIMETER BREACH' : 'LIVE'}</small></div></div>
      <div className="kpi-strip">
        <div><Bot /><span>ACTIVE AGENTS<strong>{agents.length ? `${activeAgentsCount} / ${agents.length}` : '—'}</strong></span></div>
        <div><Radio /><span>LIVE RUNS<strong>{liveRunsCount.toString().padStart(2, '0')}</strong></span></div>
        <div className={pendingSignoffs > 0 ? 'kpi-alert' : ''}><AlertTriangle /><span>PENDING SIGN-OFFS<strong>{pendingSignoffs.toString().padStart(2, '0')}</strong></span></div>
        <div><TrendingUp /><span>TODAY'S REALIZED P&L · BEFORE COSTS<strong className={todaysPnl === null ? 'text-muted-foreground' : todaysPnl.realized_pnl < 0 ? 'text-rose-300' : todaysPnl.realized_pnl > 0 ? 'text-emerald-300' : ''}>{todaysPnl === null ? '—' : `${todaysPnl.realized_pnl >= 0 ? '+' : '-'}₹${Math.abs(todaysPnl.realized_pnl).toFixed(0)}`}</strong></span></div>
        <div><LineChart /><span>UNREALIZED P&L{unrealizedPnl ? ` · ${unrealizedPnl.price_source === 'none' ? 'NO PRICE FEED' : unrealizedPnl.price_source.toUpperCase()}${unrealizedPnl.unmanaged_positions > 0 ? ` · ${unrealizedPnl.unmanaged_positions} UNMANAGED` : ''}` : ''}<strong className={unrealizedPnl?.unrealized_pnl == null ? 'text-muted-foreground' : unrealizedPnl.unrealized_pnl < 0 ? 'text-rose-300' : unrealizedPnl.unrealized_pnl > 0 ? 'text-emerald-300' : ''}>{unrealizedPnl?.unrealized_pnl == null ? '—' : `${unrealizedPnl.unrealized_pnl >= 0 ? '+' : '-'}₹${Math.abs(unrealizedPnl.unrealized_pnl).toFixed(0)}`}</strong></span></div>
        <div><ShieldAlert /><span>KILL SWITCH<strong className={killSwitchTripped ? 'text-rose-300' : killSwitchUnknown ? 'text-amber-300' : 'text-emerald-300'}>{killSwitchTripped ? 'TRIPPED' : killSwitchUnknown ? 'UNKNOWN' : 'OFF · ARMED'}</strong>
          {([['paper', paperKillSwitch], ['live', liveKillSwitch]] as const).map(([mode, ks]) =>
            ks?.tripped ? (
              <small key={mode} className="mt-1 block text-[11px] font-normal text-rose-200/80">
                {mode}: {ks.trip_reason ?? 'no reason recorded'}
                {ks.tripped_at ? ` · since ${new Date(ks.tripped_at).toLocaleString()}` : ''}
              </small>
            ) : null,
          )}
          {killSwitchUnknown && (
            <small className="mt-1 block text-[11px] font-normal text-amber-200/80">
              Could not read the {paperKillSwitch === null ? 'paper' : 'live'} kill switch
            </small>
          )}
        </span></div>
        <div><Timer /><span>MARKET HOURS<strong>{marketHours === null ? '—' : marketHours.is_open ? 'OPEN' : 'CLOSED'}</strong></span></div>
      </div>
    </section>
    <EquityPanel mode="paper" compact />
    <EquityPanel mode="live" compact />
    <section className="grid gap-5 xl:grid-cols-[1.35fr_1fr]"><article className="pulse-panel overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 p-5"><div><p className="eyebrow">LIVE ACTIVITY FEED</p><p className="mt-1 text-xs text-muted-foreground">Organization-wide agent events · newest first</p></div><div className="flex flex-wrap gap-1 rounded-lg bg-white/4 p-1">{entityTypes.map((item) => <button key={item} type="button" aria-pressed={filter === item} onClick={() => setFilter(item)} className={`filter-chip ${filter === item ? 'filter-chip-active' : ''}`}>{item}</button>)}</div></div><div className="activity-list">{filteredActivities.length === 0 && <div className="p-5 text-xs text-muted-foreground">{filter === 'All' && activities.length > 0 ? `Nothing but sign-ins and webhook deliveries recently; choose “${ROUTINE_CHIP}” to see them.` : 'No activity yet. Actions taken across the organization will appear here in real time.'}</div>}{filteredActivities.map((event) => <div key={event.id} className={`activity-row severity-${severityFor(event)}`}><span className="activity-avatar">{initialsFor(event.actor)}</span><div className="min-w-0 flex-1"><p className="truncate text-sm text-foreground">{describeEvent(event)}</p><p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{relativeTime(event.created_at)}</p></div></div>)}</div></article><Vitals /></section>
    <footer className="flex items-center justify-between pb-3 text-[10px] text-muted-foreground"><span className="flex items-center gap-2"><Activity className="size-3" /> Role: {roleLabel(role)} · {isReadOnly ? 'Read-only observability mode' : 'Control plane enabled'}</span><span>TRADINGOS OS · {autonomousSubscriptions.length > 0 ? `LIVE AUTONOMY ON (${autonomousSubscriptions.length})` : 'LIVE AUTONOMY OFF'}</span></footer>
  </div></>
}
