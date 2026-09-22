'use client'

import { Canvas, useFrame } from '@react-three/fiber'
import { Float, OrbitControls, Sparkles } from '@react-three/drei'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Mesh } from 'three'
import { Activity, AlertTriangle, Bot, CheckCircle2, Cpu, MemoryStick, Radio, ShieldAlert, Timer, TrendingUp, Wifi } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { roleLabel, useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'
import {
  type ActivityEvent,
  type ActivityFeedMessage,
  type AgentSummary,
  type KillSwitchMode,
  type KillSwitchState,
  type MarketHours,
  type OrganizationRun,
  type SignoffSnapshot,
  type SystemVitals,
  type TodaysPaperPnl,
  getAgents,
  getKillSwitch,
  getMarketHours,
  getSystemVitals,
  getTodaysPaperPnl,
  listRuns,
  resetKillSwitch,
} from '@/lib/api'
import { useWebSocketChannel } from '@/lib/ws'

type PulseState = 'IDLE' | 'RESEARCHING' | 'EXECUTING' | 'AWAITING APPROVAL' | 'RISK ALERT'

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

function PulseOrb({ state, reduceMotion }: { state: PulseState; reduceMotion: boolean }) {
  const mesh = useMemo(() => ({ current: null as Mesh | null }), [])
  const config = stateConfig[state]
  useFrame((_, delta) => {
    // In reduced-motion / power-save mode the orb holds a static pose — the state
    // color and the text status pill still communicate the current state.
    if (reduceMotion || !mesh.current) return
    mesh.current.rotation.y += delta * (state === 'RISK ALERT' ? 1.8 : state === 'RESEARCHING' ? 0.8 : 0.28)
    mesh.current.rotation.x = Math.sin(Date.now() / 1300) * 0.08
    const pulse = state === 'AWAITING APPROVAL' ? 1 + Math.sin(Date.now() / 260) * .08 : state === 'RISK ALERT' ? 1 + Math.sin(Date.now() / 110) * .045 : 1
    mesh.current.scale.setScalar(pulse)
  })
  return (
    <group>
      <Float enabled={!reduceMotion} speed={state === 'RISK ALERT' ? 4 : 1.2} rotationIntensity={.3} floatIntensity={.35}>
        <mesh ref={mesh}>
          <icosahedronGeometry args={[1.35, 5]} />
          <meshStandardMaterial color={config.color} emissive={config.color} emissiveIntensity={state === 'RISK ALERT' ? 2.2 : 1.2} metalness={.75} roughness={.2} wireframe={state === 'RESEARCHING'} />
        </mesh>
      </Float>
      <mesh scale={1.58}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshBasicMaterial color={config.color} transparent opacity={.07} />
      </mesh>
      {/* Particle flourishes are decorative — dropped in reduced-motion / power-save. */}
      {!reduceMotion && (
        <Sparkles count={state === 'RISK ALERT' ? 180 : 90} scale={state === 'RISK ALERT' ? 5.2 : 4.2} size={state === 'RISK ALERT' ? 4 : 2.4} speed={state === 'EXECUTING' ? 2 : state === 'RISK ALERT' ? 4 : .55} color={config.color} />
      )}
      <pointLight color={config.color} intensity={state === 'RISK ALERT' ? 9 : 5} distance={6} />
    </group>
  )
}

function PulseScene({ state, reduceMotion }: { state: PulseState; reduceMotion: boolean }) {
  return <Canvas frameloop={reduceMotion ? 'demand' : 'always'} camera={{ position: [0, 0, 5.4], fov: 42 }} dpr={[1, 1.5]}><ambientLight intensity={.5} /><PulseOrb state={state} reduceMotion={reduceMotion} /><OrbitControls enableZoom={false} enablePan={false} autoRotate={!reduceMotion} autoRotateSpeed={.35} /></Canvas>
}

function Vitals() {
  const [vitals, setVitals] = useState<SystemVitals | null>(null)

  useEffect(() => {
    const load = () => getSystemVitals().then(setVitals).catch(() => {})
    load()
    const interval = setInterval(load, 15_000)
    return () => clearInterval(interval)
  }, [])

  const totalTokens = vitals ? vitals.llm_token_usage.reduce((sum, e) => sum + e.count, 0) : null
  const dispatchesWithData = vitals ? vitals.order_dispatch.filter((d) => d.dispatch_count > 0) : []
  const totalDispatches = dispatchesWithData.reduce((sum, d) => sum + d.dispatch_count, 0)
  const weightedAvgLatencyMs =
    totalDispatches > 0
      ? dispatchesWithData.reduce((sum, d) => sum + (d.avg_latency_ms ?? 0) * d.dispatch_count, 0) / totalDispatches
      : null
  const totalBudgetBreaches = vitals ? vitals.order_dispatch.reduce((sum, d) => sum + d.budget_breaches, 0) : 0
  const sinceLabel = vitals ? relativeTime(vitals.since) : null

  return <section className="pulse-panel p-5"><div className="flex items-center justify-between"><div><p className="eyebrow">SYSTEM VITALS</p><p className="mt-1 text-xs text-muted-foreground">Infrastructure telemetry</p></div><Wifi className="size-4 text-emerald-300" /></div><div className="mt-5 grid gap-3 sm:grid-cols-2"><div className="vital-tile"><div className="flex justify-between text-xs"><span>LLM provider health</span><span className="text-muted-foreground">not exposed yet</span></div><p className="mt-3 text-[10px] text-muted-foreground">No backend endpoint reports per-provider health — see docs/CLAUDE.md gaps.</p></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Token usage</span><span className="text-foreground">{totalTokens === null ? '—' : totalTokens.toLocaleString()}</span></div><p className="mt-3 text-[10px] text-muted-foreground">{sinceLabel ? `Cumulative since server start (${sinceLabel}) — not "today"` : 'Loading…'}</p></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Dispatch latency</span><span className="text-foreground">{weightedAvgLatencyMs === null ? 'no dispatches yet' : `${weightedAvgLatencyMs.toFixed(0)}ms avg`}</span></div><p className="mt-2 font-mono text-[10px] text-muted-foreground">{totalDispatches} dispatch{totalDispatches === 1 ? '' : 'es'}{totalBudgetBreaches > 0 ? ` · ${totalBudgetBreaches} over budget` : ''} since server start</p></div><div className="vital-tile"><div className="flex items-center justify-between text-xs"><span className="flex items-center gap-2"><Cpu className="size-3 text-violet-300" /> Host metrics</span><span className="text-muted-foreground">not exposed yet</span></div><div className="mt-3 flex items-center gap-2 text-[10px] text-muted-foreground"><MemoryStick className="size-3 text-cyan-300" />No CPU/memory API — see Grafana host dashboard</div></div></div></section>
}

export default function Home() {
  const { role } = useAuth()
  const { powerSave } = usePreferences()
  const isReadOnly = role === 'ReadOnlyAuditor'

  const [activities, setActivities] = useState<ActivityEvent[]>([])
  const [filter, setFilter] = useState('All')
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [runs, setRuns] = useState<OrganizationRun[]>([])
  const [paperKillSwitch, setPaperKillSwitch] = useState<KillSwitchState | null>(null)
  const [liveKillSwitch, setLiveKillSwitch] = useState<KillSwitchState | null>(null)
  const [marketHours, setMarketHours] = useState<MarketHours | null>(null)
  const [signoff, setSignoff] = useState<SignoffSnapshot | null>(null)
  const [resettingMode, setResettingMode] = useState<KillSwitchMode | null>(null)
  const [resetError, setResetError] = useState<string | null>(null)
  const [todaysPnl, setTodaysPnl] = useState<TodaysPaperPnl | null>(null)

  const canResetKillSwitch = role === 'SystemAdministrator' || role === 'RiskManager'

  const load = useCallback(async () => {
    const [agentsRes, runsRes, paperKs, liveKs, hours, pnl] = await Promise.allSettled([
      getAgents(),
      listRuns(),
      getKillSwitch('paper'),
      getKillSwitch('live'),
      getMarketHours(),
      getTodaysPaperPnl(),
    ])
    if (agentsRes.status === 'fulfilled') setAgents(agentsRes.value)
    if (runsRes.status === 'fulfilled') setRuns(runsRes.value)
    if (paperKs.status === 'fulfilled') setPaperKillSwitch(paperKs.value)
    if (liveKs.status === 'fulfilled') setLiveKillSwitch(liveKs.value)
    if (hours.status === 'fulfilled') setMarketHours(hours.value)
    if (pnl.status === 'fulfilled') setTodaysPnl(pnl.value)
  }, [])

  useEffect(() => {
    let cancelled = false
    async function tick() {
      if (!cancelled) await load()
    }
    tick()
    const interval = setInterval(tick, 20000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [load])

  async function handleResetKillSwitch(mode: KillSwitchMode) {
    setResettingMode(mode)
    setResetError(null)
    try {
      await resetKillSwitch(mode, `Reset via Overview console by ${role}`)
      await load()
    } catch {
      setResetError(`Failed to reset the ${mode} kill switch. Try again.`)
    } finally {
      setResettingMode(null)
    }
  }

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
      if (message.event.entity_type === 'kill-switch') load()
    }
  })

  useWebSocketChannel<SignoffSnapshot>('/ws/sign-off-queue', {}, setSignoff)

  const pendingSignoffs = (signoff?.approvals.length ?? 0) + (signoff?.intents.length ?? 0)
  const killSwitchTripped = Boolean(paperKillSwitch?.tripped || liveKillSwitch?.tripped)
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
  const filteredActivities = filter === 'All' ? activities : activities.filter((a) => a.entity_type === filter)
  const entityTypes = useMemo(() => ['All', ...Array.from(new Set(activities.map((a) => a.entity_type)))], [activities])

  return <ShellLayout><div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
    <section className={`pulse-hero pulse-state-${state.toLowerCase().replaceAll(' ', '-')}`} style={{ '--pulse-color': config.color, '--pulse-glow': config.glow } as React.CSSProperties}>
      <div className="pulse-header"><div><p className="eyebrow">TRADINGOS // ORGANIZATION PULSE</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">The intelligence layer is <span style={{ color: config.color }}>{state.toLowerCase()}.</span></h1><p className="mt-2 text-sm text-muted-foreground">{config.copy} · NSE / BSE synchronized</p></div></div>
      <div className="pulse-orb"><PulseScene state={state} reduceMotion={powerSave} /><div className="pulse-status" style={{ borderColor: config.color, color: config.color }}><span className="status-dot" />{state}<small>{state === 'AWAITING APPROVAL' ? `${pendingSignoffs} pending` : state === 'RISK ALERT' ? 'PERIMETER BREACH' : 'LIVE'}</small></div></div>
      <div className="kpi-strip">
        <div><Bot /><span>ACTIVE AGENTS<strong>{activeAgentsCount} / {agents.length || 24}</strong></span></div>
        <div><Radio /><span>LIVE RUNS<strong>{liveRunsCount.toString().padStart(2, '0')}</strong></span></div>
        <div className={pendingSignoffs > 0 ? 'kpi-alert' : ''}><AlertTriangle /><span>PENDING SIGN-OFFS<strong>{pendingSignoffs.toString().padStart(2, '0')}</strong></span></div>
        <div><TrendingUp /><span>TODAY'S REALIZED P&L<strong className={todaysPnl === null ? 'text-muted-foreground' : todaysPnl.realized_pnl < 0 ? 'text-rose-300' : todaysPnl.realized_pnl > 0 ? 'text-emerald-300' : ''}>{todaysPnl === null ? '—' : `${todaysPnl.realized_pnl >= 0 ? '+' : '-'}₹${Math.abs(todaysPnl.realized_pnl).toFixed(0)}`}</strong></span></div>
        <div><ShieldAlert /><span>KILL SWITCH<strong className={killSwitchTripped ? 'text-rose-300' : 'text-emerald-300'}>{killSwitchTripped ? 'TRIPPED' : 'OFF · ARMED'}</strong>
          {killSwitchTripped && canResetKillSwitch && (
            <span className="kill-switch-reset-controls">
              {paperKillSwitch?.tripped && (
                <button
                  type="button"
                  className="kill-switch-reset-btn"
                  disabled={resettingMode === 'paper'}
                  onClick={() => handleResetKillSwitch('paper')}
                >
                  {resettingMode === 'paper' ? 'Resetting paper…' : 'Reset paper kill switch'}
                </button>
              )}
              {liveKillSwitch?.tripped && (
                <button
                  type="button"
                  className="kill-switch-reset-btn"
                  disabled={resettingMode === 'live'}
                  onClick={() => handleResetKillSwitch('live')}
                >
                  {resettingMode === 'live' ? 'Resetting live…' : 'Reset live kill switch'}
                </button>
              )}
              {resetError && <small className="kill-switch-reset-error">{resetError}</small>}
            </span>
          )}
        </span></div>
        <div><Timer /><span>MARKET HOURS<strong>{marketHours === null ? '—' : marketHours.is_open ? 'OPEN' : 'CLOSED'}</strong></span></div>
      </div>
    </section>
    <section className="grid gap-5 xl:grid-cols-[1.35fr_1fr]"><article className="pulse-panel overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 p-5"><div><p className="eyebrow">LIVE ACTIVITY FEED</p><p className="mt-1 text-xs text-muted-foreground">Organization-wide agent events · newest first</p></div><div className="flex gap-1 rounded-lg bg-white/4 p-1">{entityTypes.map((item) => <button key={item} onClick={() => setFilter(item)} className={`filter-chip ${filter === item ? 'filter-chip-active' : ''}`}>{item}</button>)}</div></div><div className="activity-list">{filteredActivities.length === 0 && <div className="p-5 text-xs text-muted-foreground">No activity yet. Actions taken across the organization will appear here in real time.</div>}{filteredActivities.map((event) => <div key={event.id} className={`activity-row severity-${severityFor(event)}`}><span className="activity-avatar">{initialsFor(event.actor)}</span><div className="min-w-0 flex-1"><p className="truncate text-sm text-foreground">{event.actor} · {event.action} {event.entity_type} {event.entity_id}</p><p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{relativeTime(event.created_at)} · verified</p></div><CheckCircle2 className="size-3 shrink-0 text-emerald-300/70" /></div>)}</div></article><Vitals /></section>
    <footer className="flex items-center justify-between pb-3 text-[10px] text-muted-foreground"><span className="flex items-center gap-2"><Activity className="size-3" /> Role: {roleLabel(role)} · {isReadOnly ? 'Read-only observability mode' : 'Control plane enabled'}</span><span>TRADINGOS OS · PAPER ENVIRONMENT</span></footer>
  </div></ShellLayout>
}
