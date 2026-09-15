'use client'

import { Canvas, useFrame } from '@react-three/fiber'
import { Float, OrbitControls, Sparkles } from '@react-three/drei'
import { useMemo, useState } from 'react'
import type { Mesh } from 'three'
import { Activity, AlertTriangle, Bot, CheckCircle2, ChevronDown, Cpu, Gauge, LockKeyhole, MemoryStick, Radio, ShieldAlert, Sparkles as SparklesIcon, Timer, TrendingUp, Wifi } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { roleLabel, useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'

type PulseState = 'IDLE' | 'RESEARCHING' | 'EXECUTING' | 'AWAITING APPROVAL' | 'RISK ALERT'

const stateConfig: Record<PulseState, { color: string; glow: string; copy: string }> = {
  IDLE: { color: '#67e8f9', glow: 'rgba(34,211,238,.35)', copy: 'All systems nominal' },
  RESEARCHING: { color: '#60a5fa', glow: 'rgba(59,130,246,.38)', copy: 'Agents are scanning market signals' },
  EXECUTING: { color: '#4ade80', glow: 'rgba(74,222,128,.38)', copy: 'Paper execution mesh active' },
  'AWAITING APPROVAL': { color: '#fbbf24', glow: 'rgba(251,191,36,.42)', copy: 'Human sign-off required' },
  'RISK ALERT': { color: '#f87171', glow: 'rgba(248,113,113,.52)', copy: 'Risk perimeter breached' },
}

const activities = [
  ['AG', 'Strategy Generator proposed NIFTY-IronCondor-v3', '12s ago', 'info'],
  ['RM', 'Risk Manager flagged BANKNIFTY correlation breach', '28s ago', 'critical'],
  ['PT', 'Paper order filled: RELIANCE · 50 qty', '41s ago', 'success'],
  ['MS', 'Momentum Scout detected TCS breakout pattern', '1m ago', 'info'],
  ['OR', 'Orchestrator routed HDFCBANK signal to 4 agents', '2m ago', 'info'],
  ['RS', 'Risk Sentinel reduced exposure by ₹1.8L', '3m ago', 'warning'],
  ['AG', 'Alpha Engine completed NIFTY volatility scan', '4m ago', 'success'],
  ['SY', 'System health check passed across 7 providers', '5m ago', 'success'],
  ['PT', 'Paper order queued: INFY · 120 qty', '6m ago', 'info'],
  ['RM', 'Risk Manager approved RELIANCE position sizing', '7m ago', 'success'],
  ['MS', 'Mean Reversion agent refreshed watchlist', '8m ago', 'info'],
  ['OR', 'Orchestrator paused strategy: BANKNIFTY-ORB', '9m ago', 'warning'],
  ['AG', 'Research agent published TCS earnings brief', '10m ago', 'info'],
  ['PT', 'Paper order filled: HDFCBANK · 75 qty', '12m ago', 'success'],
  ['SY', 'Market gateway NSE heartbeat acknowledged', '14m ago', 'success'],
  ['RS', 'Risk Sentinel recalculated sector concentration', '16m ago', 'info'],
  ['AG', 'Strategy Generator backtested NIFTY-Pairs-v2', '18m ago', 'info'],
  ['RM', 'Risk Manager lowered BANKNIFTY leverage cap', '21m ago', 'warning'],
  ['MS', 'Momentum Scout closed RELIANCE signal loop', '24m ago', 'success'],
  ['SY', 'Audit log checkpoint sealed', '28m ago', 'success'],
] as const

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
  return <section className="pulse-panel p-5"><div className="flex items-center justify-between"><div><p className="eyebrow">SYSTEM VITALS</p><p className="mt-1 text-xs text-muted-foreground">Infrastructure telemetry</p></div><Wifi className="size-4 text-emerald-300" /></div><div className="mt-5 grid gap-3 sm:grid-cols-2"><div className="vital-tile"><div className="flex justify-between text-xs"><span>LLM provider health</span><span className="text-emerald-300">7 / 7</span></div><div className="mt-3 flex gap-1.5">{['GPT','CLD','GEM','GRO','MST','XAI','LOC'].map((provider, i) => <span key={provider} title={provider} className={`provider-dot ${i === 0 ? 'provider-active' : ''}`} />)}</div></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Token usage</span><span className="font-mono text-cyan-200">68%</span></div><div className="progress-track mt-3"><span style={{ width: '68%' }} /></div><p className="mt-2 font-mono text-[10px] text-muted-foreground">6.8M / 10M budget</p></div><div className="vital-tile"><div className="flex justify-between text-xs"><span>Dispatch latency</span><span className="text-emerald-300">18ms</span></div><div className="sparkline mt-3"><span /><span /><span /><span /><span /><span /><span /><i /></div><p className="mt-2 font-mono text-[10px] text-muted-foreground">50ms budget line</p></div><div className="vital-tile"><div className="flex items-center justify-between text-xs"><span className="flex items-center gap-2"><Cpu className="size-3 text-violet-300" /> CPU</span><span className="font-mono text-violet-200">42%</span></div><div className="mt-3 flex items-center gap-2"><div className="progress-track flex-1"><span className="bg-violet-300!" style={{ width: '42%' }} /></div><MemoryStick className="size-3 text-cyan-300" /><span className="font-mono text-[10px]">58%</span></div></div></div></section>
}

export default function Home() {
  const { role } = useAuth()
  const isReadOnly = role === 'ReadOnlyAuditor'
  const [state, setState] = useState<PulseState>('RESEARCHING')
  const [filter, setFilter] = useState('All')
  const config = stateConfig[state]
  return <ShellLayout><div className="mx-auto flex w-full max-w-[1700px] flex-col gap-5">
    <section className={`pulse-hero pulse-state-${state.toLowerCase().replaceAll(' ', '-')}`} style={{ '--pulse-color': config.color, '--pulse-glow': config.glow } as React.CSSProperties}>
      <div className="pulse-header"><div><p className="eyebrow">TRADINGOS // ORGANIZATION PULSE</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em]">The intelligence layer is <span style={{ color: config.color }}>{state.toLowerCase()}.</span></h1><p className="mt-2 text-sm text-muted-foreground">{config.copy} · NSE / BSE synchronized</p></div><div className="pulse-dev-control"><span className="eyebrow text-[9px]">DEV STATE</span><select value={state} onChange={(event) => setState(event.target.value as PulseState)} aria-label="Organization Pulse state">{Object.keys(stateConfig).map((name) => <option key={name}>{name}</option>)}</select></div></div>
      <div className="pulse-orb"><PulseScene state={state} /><div className="pulse-status" style={{ borderColor: config.color, color: config.color }}><span className="status-dot" />{state}<small>{state === 'AWAITING APPROVAL' ? '00:42' : state === 'RISK ALERT' ? 'PERIMETER BREACH' : 'LIVE'}</small></div></div>
      <div className="kpi-strip"><div><Bot /><span>ACTIVE AGENTS<strong>22 / 24</strong></span></div><div><Radio /><span>LIVE RUNS<strong>08</strong></span></div><div className="kpi-alert"><AlertTriangle /><span>PENDING SIGN-OFFS<strong>03</strong></span></div><div><TrendingUp /><span>TODAY'S PAPER P&L<strong className="text-emerald-300">+₹84,260</strong></span></div><div><ShieldAlert /><span>KILL SWITCH<strong className="text-emerald-300">OFF · ARMED</strong></span></div><div><Timer /><span>MARKET HOURS<strong>Closes in 2h 14m</strong></span></div></div>
    </section>
    <section className="grid gap-5 xl:grid-cols-[1.35fr_1fr]"><article className="pulse-panel overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 p-5"><div><p className="eyebrow">LIVE ACTIVITY FEED</p><p className="mt-1 text-xs text-muted-foreground">Organization-wide agent events · newest first</p></div><div className="flex gap-1 rounded-lg bg-white/4 p-1">{['All', 'Orchestration', 'Risk', 'Trading', 'System'].map((item) => <button key={item} onClick={() => setFilter(item)} className={`filter-chip ${filter === item ? 'filter-chip-active' : ''}`}>{item}</button>)}</div></div><div className="activity-list">{activities.map(([avatar, text, time, severity]) => <div key={text} className={`activity-row severity-${severity}`}><span className="activity-avatar">{avatar}</span><div className="min-w-0 flex-1"><p className="truncate text-sm text-foreground">{text}</p><p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{time} · verified</p></div><CheckCircle2 className="size-3 shrink-0 text-emerald-300/70" /></div>)}</div></article><Vitals /></section>
    <footer className="flex items-center justify-between pb-3 text-[10px] text-muted-foreground"><span className="flex items-center gap-2"><LockKeyhole className="size-3" /> Role: {roleLabel(role)} · {isReadOnly ? 'Read-only observability mode' : 'Control plane enabled'}</span><span>TRADINGOS OS · PAPER ENVIRONMENT</span></footer>
  </div></ShellLayout>
}
