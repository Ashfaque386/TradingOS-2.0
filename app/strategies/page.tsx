'use client'

import { FormEvent, useEffect, useMemo, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, ChevronRight, Code2, Gauge, MessageSquare, RefreshCw, ShieldCheck, Sparkles, Terminal, TrendingUp, X } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type GoLiveReadinessInput,
  type GoLiveReadinessResult,
  type Strategy,
  type SuggestionDto,
  ApiError,
  approveLiveEligibility,
  checkGoLiveReadiness,
  createStrategy,
  getStrategy,
  listStrategies,
  promoteStrategy,
  regenerateFromSuggestion,
  requestLiveEligibility,
  requestPromotion,
  reviewSuggestion,
  submitSuggestion,
} from '@/lib/api'

type Stage = 'Ideation' | 'Coding' | 'Backtesting' | 'Paper Trading' | 'Live Eligible' | 'Live' | 'Deprecated'
const stages: Stage[] = ['Ideation', 'Coding', 'Backtesting', 'Paper Trading', 'Live Eligible', 'Live', 'Deprecated']

const STAGE_LABELS: Record<string, Stage> = {
  Ideation: 'Ideation',
  Coding: 'Coding',
  Backtesting: 'Backtesting',
  PaperTrading: 'Paper Trading',
  LiveEligible: 'Live Eligible',
  Live: 'Live',
  Deprecated: 'Deprecated',
}

function stageOf(strategy: Strategy): Stage {
  return STAGE_LABELS[strategy.status] ?? 'Ideation'
}

function Badge({ children, tone = 'cyan' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`strategy-badge tone-${tone}`}>{children}</span>
}

const READINESS_DEFAULTS: GoLiveReadinessInput = {
  num_trades: 0,
  calendar_days_running: 0,
  clean_shadow_mode_streak_days: 0,
  live_win_rate: null,
  backtest_win_rate: null,
}

function Review({ strategyId, onBack, canOperate, canSignOffLive }: { strategyId: string; onBack: () => void; canOperate: boolean; canSignOffLive: boolean }) {
  const [strategy, setStrategy] = useState<Strategy | null>(null)
  const [tab, setTab] = useState('Overview')
  const [feedback, setFeedback] = useState('')
  const [suggestion, setSuggestion] = useState<SuggestionDto | null>(null)
  const [submittingSuggestion, setSubmittingSuggestion] = useState(false)
  const [readinessInput, setReadinessInput] = useState<GoLiveReadinessInput>(READINESS_DEFAULTS)
  const [readinessResult, setReadinessResult] = useState<GoLiveReadinessResult | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function reload() {
    const data = await getStrategy(strategyId)
    setStrategy(data)
  }

  useEffect(() => {
    reload()
  }, [strategyId])

  if (!strategy) return <div className="mx-auto w-full max-w-[1700px] p-8 text-sm text-muted-foreground">Loading strategy…</div>

  const latestVersion = strategy.versions[strategy.versions.length - 1] ?? null
  const stage = stageOf(strategy)
  const isOptions = strategy.instrument_class === 'options'

  async function handleSubmitFeedback() {
    if (!feedback.trim() || !latestVersion) return
    setSubmittingSuggestion(true)
    setActionError(null)
    try {
      const submitted = await submitSuggestion(strategy!.id, latestVersion.id, feedback.trim())
      const reviewed = await reviewSuggestion(strategy!.id, submitted.id)
      setSuggestion(reviewed)
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Failed to submit suggestion')
    } finally {
      setSubmittingSuggestion(false)
    }
  }

  async function handleRegenerate() {
    if (!suggestion) return
    setBusy(true)
    setActionError(null)
    try {
      await regenerateFromSuggestion(strategy!.id, suggestion.id)
      setSuggestion(null)
      setFeedback('')
      await reload()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Failed to regenerate')
    } finally {
      setBusy(false)
    }
  }

  async function handleLifecycleAction(action: 'request-promotion' | 'promote' | 'request-live' | 'approve-live') {
    setBusy(true)
    setActionError(null)
    try {
      if (action === 'request-promotion') await requestPromotion(strategy!.id)
      else if (action === 'promote') await promoteStrategy(strategy!.id)
      else if (action === 'request-live') await requestLiveEligibility(strategy!.id)
      else await approveLiveEligibility(strategy!.id, readinessInput)
      await reload()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Failed: ${action}`)
    } finally {
      setBusy(false)
    }
  }

  async function handleCheckReadiness() {
    setBusy(true)
    setActionError(null)
    try {
      setReadinessResult(await checkGoLiveReadiness(readinessInput))
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Failed to check readiness')
    } finally {
      setBusy(false)
    }
  }

  return <div className="mx-auto w-full max-w-[1700px]">
    <button onClick={onBack} className="mb-4 flex items-center gap-2 text-xs text-cyan-200 transition hover:text-white"><ArrowLeft className="size-4" /> Back to lifecycle board</button>
    <div className="strategy-review-hero"><div><div className="flex flex-wrap items-center gap-2"><Badge>{strategy.instrument_class}</Badge><Badge tone="green">{stage}</Badge><span className="font-mono text-[10px] text-slate-500">STRATEGY ID · {strategy.id.slice(0, 8).toUpperCase()}</span></div><h1 className="mt-3 text-2xl font-semibold tracking-tight text-white">{strategy.name}</h1><p className="mt-2 max-w-3xl text-sm text-slate-400">{strategy.objective}</p></div><div className="strategy-review-score"><span>VERSIONS</span><strong>{strategy.versions.length}</strong><small>{latestVersion?.static_validation_passed ? 'latest passes static validation' : 'latest failing validation'}</small></div></div>
    {actionError && <div className="mt-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{actionError}</div>}
    <div className="strategy-tabs">{['Overview', 'Code', ...(isOptions ? ['Options Legs'] : []), 'Lifecycle'].map(item => <button key={item} onClick={() => setTab(item)} className={tab === item ? 'active' : ''}>{item === 'Code' ? <Code2 className="size-3.5" /> : item === 'Lifecycle' ? <Gauge className="size-3.5" /> : null}{item}</button>)}</div>
    <div className="strategy-review-panel">
      {tab === 'Overview' && <div className="review-content"><div className="review-icon"><Sparkles className="size-5" /></div><p className="eyebrow">STRATEGY OBJECTIVE</p><h2>{strategy.objective}</h2><div className="logic-grid"><div><span>INSTRUMENT CLASS</span><strong>{strategy.instrument_class}</strong></div><div><span>STATUS</span><strong>{strategy.status}</strong></div><div><span>STATIC VALIDATION</span><strong>{latestVersion?.static_validation_passed ? 'Passed' : 'Failed'}</strong></div><div><span>SANDBOX</span><strong>{latestVersion?.sandbox_passed === null ? 'Not run' : latestVersion?.sandbox_passed ? 'Passed' : 'Failed'}</strong></div></div>{latestVersion?.static_validation_errors && latestVersion.static_validation_errors.length > 0 && <div className="mt-4 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">{latestVersion.static_validation_errors.join(', ')}</div>}</div>}
      {tab === 'Code' && <div className="review-content"><div className="code-toolbar"><span><Terminal className="size-4 text-cyan-300" /> generated_strategy.py · v{latestVersion?.version_number ?? '—'}</span><Badge tone="green">READ ONLY</Badge></div><pre className="strategy-code"><code>{latestVersion?.code ?? '// no version generated yet'}</code></pre>{canOperate && <div className="feedback-box"><label htmlFor="feedback">Suggest Improvement</label><div className="flex gap-2"><textarea id="feedback" value={feedback} onChange={e => setFeedback(e.target.value)} placeholder="Ask the AI reviewer to improve risk controls, clarity, or execution logic..." /><button disabled={!feedback.trim() || submittingSuggestion || !latestVersion} onClick={handleSubmitFeedback}>{submittingSuggestion ? <RefreshCw className="size-4 animate-spin" /> : <MessageSquare className="size-4" />} Submit</button></div>{suggestion?.ai_verdict && <div className="verdict"><Sparkles className="size-4 text-violet-300" /><div><strong>AI Review Verdict · {String(suggestion.ai_verdict.verdict ?? suggestion.status)}</strong><p>{String(suggestion.ai_verdict.notes ?? suggestion.ai_verdict.summary ?? JSON.stringify(suggestion.ai_verdict))}</p><button disabled={busy} onClick={handleRegenerate} className="button-secondary mt-3"><RefreshCw className="size-3" />Regenerate version from suggestion</button></div></div>}</div>}</div>}
      {tab === 'Options Legs' && <div className="review-content"><div className="flex items-center justify-between"><div><p className="eyebrow">DEFINED RISK STRUCTURE</p><h2 className="mt-2">Options legs</h2></div>{latestVersion?.options_legs && <Badge tone="green"><ShieldCheck className="size-3" /> {String((latestVersion.options_legs as Record<string, unknown>).naked_scan_status ?? 'grounded')}</Badge>}</div><pre className="strategy-code mt-4"><code>{latestVersion?.options_legs ? JSON.stringify(latestVersion.options_legs, null, 2) : 'No options legs recorded for this version.'}</code></pre></div>}
      {tab === 'Lifecycle' && <div className="review-content">
        <div className="readiness-banner"><Gauge className="size-5" /><div><strong>Current stage: {stage}</strong><p>Advance this strategy through the real pipeline gates below.</p></div></div>
        {canOperate && stage === 'Backtesting' && <div className="mt-4 flex gap-2"><button disabled={busy} onClick={() => handleLifecycleAction('request-promotion')} className="button-secondary">Request promotion to paper trading</button><button disabled={busy} onClick={() => handleLifecycleAction('promote')} className="button-primary"><Check className="size-3" />Promote (after approval)</button></div>}
        {canSignOffLive && stage === 'Paper Trading' && <div className="mt-4"><button disabled={busy} onClick={() => handleLifecycleAction('request-live')} className="button-secondary">Request live-eligibility sign-off</button></div>}
        {canSignOffLive && (stage === 'Paper Trading' || stage === 'Live Eligible') && <div className="mt-6 rounded-xl border border-white/10 p-4">
          <p className="eyebrow">GO-LIVE READINESS INPUTS</p>
          <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
            <label className="flex flex-col gap-1">Trades<input type="number" value={readinessInput.num_trades} onChange={e => setReadinessInput({ ...readinessInput, num_trades: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
            <label className="flex flex-col gap-1">Calendar days running<input type="number" value={readinessInput.calendar_days_running} onChange={e => setReadinessInput({ ...readinessInput, calendar_days_running: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
            <label className="flex flex-col gap-1">Clean shadow-mode streak (days)<input type="number" value={readinessInput.clean_shadow_mode_streak_days} onChange={e => setReadinessInput({ ...readinessInput, clean_shadow_mode_streak_days: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
            <label className="flex flex-col gap-1">Live win rate (0-1)<input type="number" step="0.01" value={readinessInput.live_win_rate ?? ''} onChange={e => setReadinessInput({ ...readinessInput, live_win_rate: e.target.value === '' ? null : Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
            <label className="flex flex-col gap-1">Backtest win rate (0-1)<input type="number" step="0.01" value={readinessInput.backtest_win_rate ?? ''} onChange={e => setReadinessInput({ ...readinessInput, backtest_win_rate: e.target.value === '' ? null : Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" /></label>
          </div>
          <div className="mt-3 flex gap-2"><button disabled={busy} onClick={handleCheckReadiness} className="button-secondary">Check readiness</button><button disabled={busy} onClick={() => handleLifecycleAction('approve-live')} className="button-primary"><Check className="size-3" />Approve live-eligibility</button></div>
          {readinessResult && <div className="readiness-list mt-4">{Object.entries(readinessResult.checks).map(([label, pass]) => <div className="readiness-row" key={label}><div className="flex items-center justify-between gap-3"><span>{label}</span><strong className={pass ? 'pass' : ''}>{pass ? 'PASS' : 'PENDING'}</strong></div></div>)}{readinessResult.reasons.length > 0 && <p className="mt-2 text-xs text-amber-200">{readinessResult.reasons.join(' · ')}</p>}</div>}
        </div>}
        {!canOperate && !canSignOffLive && <p className="mt-4 text-xs text-muted-foreground">Your role has read-only access to lifecycle actions.</p>}
      </div>}
    </div>
  </div>
}

export default function StrategiesPage() {
  const { role } = useAuth()
  const canOperate = role === 'SystemAdministrator' || role === 'PortfolioManager'
  const canSignOffLive = role === 'SystemAdministrator' || role === 'RiskManager'
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [objective, setObjective] = useState('')
  const [instrumentClass, setInstrumentClass] = useState('equity')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  async function reload() {
    setStrategies(await listStrategies())
  }

  useEffect(() => {
    reload()
    const interval = setInterval(reload, 15000)
    return () => clearInterval(interval)
  }, [])

  const grouped = useMemo(
    () => Object.fromEntries(stages.map((stage) => [stage, strategies.filter((s) => stageOf(s) === stage)])) as Record<Stage, Strategy[]>,
    [strategies],
  )

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim() || !objective.trim()) return
    setCreating(true)
    setCreateError(null)
    try {
      await createStrategy(name.trim(), objective.trim(), instrumentClass)
      setName('')
      setObjective('')
      await reload()
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : 'Failed to create strategy')
    } finally {
      setCreating(false)
    }
  }

  if (selectedId) return <ShellLayout><Review strategyId={selectedId} onBack={() => setSelectedId(null)} canOperate={canOperate} canSignOffLive={canSignOffLive} /></ShellLayout>

  return <ShellLayout><div className="mx-auto w-full max-w-[1700px]">
    <div className="strategy-page-header"><div><p className="eyebrow">TRADINGOS // STRATEGY CONTROL PLANE</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em] text-white">Strategy lifecycle</h1><p className="mt-2 text-sm text-slate-400">From generated hypothesis to controlled live capital. Select a strategy to open its review workspace.</p></div><div className="strategy-header-stats"><div><strong>{strategies.length}</strong><span>TRACKED</span></div><div><strong>{strategies.filter(s => !['Ideation', 'Live', 'Deprecated'].includes(stageOf(s))).length.toString().padStart(2, '0')}</strong><span>IN FLIGHT</span></div><div><strong className="text-emerald-300">{strategies.filter(s => stageOf(s) === 'Live').length.toString().padStart(2, '0')}</strong><span>LIVE</span></div></div></div>
    {canOperate && <form onSubmit={handleCreate} className="mb-5 flex flex-wrap items-center gap-2 rounded-xl border border-white/10 bg-white/[.02] p-3">
      <input value={name} onChange={e => setName(e.target.value)} placeholder="Strategy name" className="min-w-0 flex-1 basis-48 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
      <input value={objective} onChange={e => setObjective(e.target.value)} placeholder="Objective for the AI generator..." className="min-w-0 flex-[2] basis-64 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground" />
      <select value={instrumentClass} onChange={e => setInstrumentClass(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs text-foreground"><option value="equity">Equity</option><option value="options">Options</option></select>
      <button type="submit" disabled={creating || !name.trim() || !objective.trim()} className="button-primary">{creating ? <RefreshCw className="size-3 animate-spin" /> : <ArrowRight className="size-3" />}Generate</button>
      {createError && <span className="text-xs text-rose-300">{createError}</span>}
    </form>}
    <div className="strategy-board">{stages.map((stage, index) => <section className="strategy-column" key={stage}><div className="strategy-column-title"><div><span className="stage-index">0{index + 1}</span><h2>{stage}</h2></div><span className="stage-count">{grouped[stage].length}</span></div><div className="strategy-cards">{grouped[stage].map(item => <button className="strategy-card" key={item.id} onClick={() => setSelectedId(item.id)}><div className="flex items-start justify-between gap-2"><Badge tone={item.instrument_class === 'options' ? 'violet' : 'cyan'}>{item.instrument_class}</Badge><span className="font-mono text-[10px] text-slate-500">{item.versions.length} ver</span></div><h3>{item.name}</h3><div className="flex items-center justify-between gap-2"><span className="flex items-center gap-1 text-[10px] text-muted-foreground"><TrendingUp className="size-3" /> {item.versions[item.versions.length - 1]?.static_validation_passed ? 'validated' : 'awaiting validation'}</span></div><div className="mt-3 flex items-center justify-between border-t border-white/8 pt-2 text-[10px] text-slate-500"><span>REVIEW WORKSPACE</span><ChevronRight className="size-3.5 text-cyan-300" /></div></button>)}</div></section>)}</div>
  </div></ShellLayout>
}
