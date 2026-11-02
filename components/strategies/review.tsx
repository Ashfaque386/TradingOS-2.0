'use client'

import { useMemo, useState } from 'react'
import { ArrowLeft, Check, Code2, Gauge, History, MessageSquare, RefreshCw, ShieldCheck, Sparkles, Terminal } from 'lucide-react'
import {
  type Strategy,
  type StrategyVersion,
  type SuggestionDto,
  ApiError,
  getStrategy,
  listStrategySuggestions,
  promoteStrategy,
  regenerateFromSuggestion,
  requestLiveEligibility,
  requestPromotion,
  reviewSuggestion,
  submitSuggestion,
} from '@/lib/api'
import { GoLiveEvidencePanel } from '@/components/strategies/go-live-evidence'
import { LiveTradingPanel } from '@/components/strategies/live-trading-panel'
import { ShadowModePanel } from '@/components/strategies/shadow-mode-panel'
import { Badge, stageOf } from '@/components/strategies/shared'
import { VersionsPanel } from '@/components/strategies/versions-panel'
import { ErrorNotice } from '@/components/ui/error-notice'
import { TabList } from '@/components/ui/tab-list'
import { newestFirst, sandboxNote, versionLabel } from '@/lib/strategy-versions'
import { useQuery } from '@/lib/use-query'

const TABS = ['Overview', 'Code', 'Versions', 'Options Legs', 'Lifecycle', 'Shadow Mode'] as const
export type ReviewTab = (typeof TABS)[number]

/** A tab named in the address (`?tab=Versions`), or the first. */
export function tabFrom(name: string | null | undefined): ReviewTab {
  return TABS.find((tab) => tab === name) ?? 'Overview'
}

/** One strategy's review workspace: what it is, its code version by version and what changed between them,
 * the suggestions made for it, and the lifecycle gates it has to pass. It has an address of its own
 * (`/strategies/{id}`, and `?tab=` for a tab), so it can be linked to and reloaded. */
export function StrategyReview({
  strategyId,
  onBack,
  canOperate,
  canSignOffLive,
  initialTab,
}: {
  strategyId: string
  onBack: () => void
  canOperate: boolean
  canSignOffLive: boolean
  initialTab?: string | null
}) {
  const strategyQuery = useQuery(`strategy:${strategyId}`, () => getStrategy(strategyId))
  const suggestionsQuery = useQuery(`suggestions:${strategyId}`, () => listStrategySuggestions(strategyId))
  const strategy: Strategy | null = strategyQuery.data
  const suggestions: SuggestionDto[] = useMemo(() => suggestionsQuery.data ?? [], [suggestionsQuery.data])

  const [tab, setTab] = useState<ReviewTab>(() => tabFrom(initialTab))
  const [viewing, setViewing] = useState<string | null>(null)
  const [feedback, setFeedback] = useState('')
  const [suggestion, setSuggestion] = useState<SuggestionDto | null>(null)
  const [submittingSuggestion, setSubmittingSuggestion] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reloadAll = async () => {
    await Promise.all([strategyQuery.refresh(), suggestionsQuery.refresh()])
  }

  const chooseTab = (next: ReviewTab) => {
    setTab(next)
    window.history.replaceState(null, '', `${window.location.pathname}${next === 'Overview' ? '' : `?tab=${encodeURIComponent(next)}`}`)
  }

  if (!strategy) {
    return <div className="mx-auto w-full max-w-[1700px] p-8 text-sm text-muted-foreground">
      {strategyQuery.error
        ? <ErrorNotice message={`Could not load this strategy: ${strategyQuery.error}`} onRetry={() => void strategyQuery.refresh()} />
        : 'Loading strategy…'}
      <button type="button" className="button-secondary mt-4" onClick={onBack}><ArrowLeft className="size-3" />Back to lifecycle board</button>
    </div>
  }

  const versions = newestFirst(strategy.versions)
  const latestVersion = versions[0] ?? null
  const viewedVersion: StrategyVersion | null = versions.find((v) => v.id === viewing) ?? latestVersion
  const stage = stageOf(strategy)
  const isOptions = strategy.instrument_class === 'options'
  const tabs = TABS.filter((item) => (item === 'Options Legs' ? isOptions : item === 'Shadow Mode' ? stage !== 'Backtesting' : true))

  async function handleSubmitFeedback() {
    if (!feedback.trim() || !viewedVersion) return
    setSubmittingSuggestion(true)
    setActionError(null)
    try {
      const submitted = await submitSuggestion(strategy!.id, viewedVersion.id, feedback.trim())
      const reviewed = await reviewSuggestion(strategy!.id, submitted.id)
      setSuggestion(reviewed)
      await suggestionsQuery.refresh()
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
      setViewing(null) // the new version is the newest
      await reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Failed to regenerate')
    } finally {
      setBusy(false)
    }
  }

  async function handleLifecycleAction(action: 'request-promotion' | 'promote' | 'request-live') {
    setBusy(true)
    setActionError(null)
    try {
      if (action === 'request-promotion') await requestPromotion(strategy!.id)
      else if (action === 'promote') await promoteStrategy(strategy!.id)
      else await requestLiveEligibility(strategy!.id)
      await reloadAll()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : `Failed: ${action}`)
    } finally {
      setBusy(false)
    }
  }

  return <div className="mx-auto w-full max-w-[1700px]">
    <button onClick={onBack} className="mb-4 flex items-center gap-2 text-xs text-cyan-200 transition hover:text-white"><ArrowLeft className="size-4" /> Back to lifecycle board</button>
    <div className="strategy-review-hero"><div><div className="flex flex-wrap items-center gap-2"><Badge>{strategy.instrument_class}</Badge><Badge tone="green">{stage}</Badge><span className="font-mono text-[10px] text-slate-500">STRATEGY ID · {strategy.id.slice(0, 8).toUpperCase()}</span></div><h1 className="mt-3 text-2xl font-semibold tracking-tight text-white">{strategy.name}</h1><p className="mt-2 max-w-3xl text-sm text-slate-400">{strategy.objective}</p></div><div className="strategy-review-score"><span>VERSIONS</span><strong>{strategy.versions.length}</strong><small>{latestVersion?.static_validation_passed ? 'latest passes static validation' : 'latest failing validation'}</small></div></div>
    {actionError && <div className="mt-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{actionError}</div>}
    <TabList label="Strategy views" className="strategy-tabs">{tabs.map(item => <button key={item} role="tab" aria-selected={tab === item} tabIndex={tab === item ? 0 : -1} onClick={() => chooseTab(item)} className={tab === item ? 'active' : ''}>{item === 'Code' ? <Code2 className="size-3.5" /> : item === 'Lifecycle' ? <Gauge className="size-3.5" /> : item === 'Versions' ? <History className="size-3.5" /> : null}{item}</button>)}</TabList>
    <div className="strategy-review-panel">
      {tab === 'Overview' && <div className="review-content"><div className="review-icon"><Sparkles className="size-5" /></div><p className="eyebrow">STRATEGY OBJECTIVE</p><h2>{strategy.objective}</h2><div className="logic-grid"><div><span>INSTRUMENT CLASS</span><strong>{strategy.instrument_class}</strong></div><div><span>STATUS</span><strong>{strategy.status}</strong></div><div><span>STATIC VALIDATION</span><strong>{latestVersion?.static_validation_passed ? 'Passed' : 'Failed'}</strong></div><div><span>SANDBOX</span><strong>{latestVersion?.sandbox_passed === null ? 'Not run' : latestVersion?.sandbox_passed ? 'Passed' : 'Failed'}</strong></div></div>{latestVersion && <p className="mt-3 text-xs text-slate-400">{versionLabel(latestVersion)}: {sandboxNote(latestVersion)}. <button type="button" className="audit-link" onClick={() => chooseTab('Versions')}>See every version and what changed</button></p>}{latestVersion?.static_validation_errors && latestVersion.static_validation_errors.length > 0 && <div className="mt-4 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100">{latestVersion.static_validation_errors.join(', ')}</div>}</div>}
      {tab === 'Code' && <div className="review-content"><div className="code-toolbar"><span><Terminal className="size-4 text-cyan-300" /> generated_strategy.py · {viewedVersion ? versionLabel(viewedVersion) : 'v—'}</span><div className="flex items-center gap-2">{versions.length > 1 && <select aria-label="Version" className="backtest-select" value={viewedVersion?.id ?? ''} onChange={(e) => setViewing(e.target.value === latestVersion?.id ? null : e.target.value)}>{versions.map((v) => <option key={v.id} value={v.id}>{versionLabel(v)}{v.id === latestVersion?.id ? ' (latest)' : ''}</option>)}</select>}<Badge tone="green">READ ONLY</Badge></div></div><pre className="strategy-code"><code>{viewedVersion?.code ?? '// no version generated yet'}</code></pre>{canOperate && <div className="feedback-box"><label htmlFor="feedback">Suggest Improvement{viewedVersion && versions.length > 1 ? ` to ${versionLabel(viewedVersion)}` : ''}</label><div className="flex gap-2"><textarea id="feedback" value={feedback} onChange={e => setFeedback(e.target.value)} placeholder="Ask the AI reviewer to improve risk controls, clarity, or execution logic..." /><button disabled={!feedback.trim() || submittingSuggestion || !viewedVersion} onClick={handleSubmitFeedback}>{submittingSuggestion ? <RefreshCw className="size-4 animate-spin" /> : <MessageSquare className="size-4" />} Submit</button></div>{suggestion?.ai_verdict && <div className="verdict"><Sparkles className="size-4 text-violet-300" /><div><strong>AI Review Verdict · {String(suggestion.ai_verdict.verdict ?? suggestion.status)}</strong><p>{String(suggestion.ai_verdict.notes ?? suggestion.ai_verdict.summary ?? JSON.stringify(suggestion.ai_verdict))}</p><button disabled={busy} onClick={handleRegenerate} className="button-secondary mt-3"><RefreshCw className="size-3" />Regenerate version from suggestion</button></div></div>}</div>}</div>}
      {tab === 'Versions' && <VersionsPanel versions={strategy.versions} suggestions={suggestions} suggestionsError={suggestionsQuery.error} onRetrySuggestions={() => void suggestionsQuery.refresh()} onViewCode={(id) => { setViewing(id === latestVersion?.id ? null : id); chooseTab('Code') }} />}
      {tab === 'Options Legs' && <div className="review-content"><div className="flex items-center justify-between"><div><p className="eyebrow">DEFINED RISK STRUCTURE</p><h2 className="mt-2">Options legs</h2></div>{latestVersion?.options_legs && <Badge tone="green"><ShieldCheck className="size-3" /> {String((latestVersion.options_legs as Record<string, unknown>).naked_scan_status ?? 'grounded')}</Badge>}</div><pre className="strategy-code mt-4"><code>{latestVersion?.options_legs ? JSON.stringify(latestVersion.options_legs, null, 2) : 'No options legs recorded for this version.'}</code></pre></div>}
      {tab === 'Shadow Mode' && <ShadowModePanel strategyId={strategy.id} defaultSymbol="" canRun={canOperate || canSignOffLive} />}
      {tab === 'Lifecycle' && <div className="review-content">
        <div className="readiness-banner"><Gauge className="size-5" /><div><strong>Current stage: {stage}</strong><p>Advance this strategy through the real pipeline gates below.</p></div></div>
        {canOperate && stage === 'Backtesting' && <div className="mt-4 flex gap-2"><button disabled={busy} onClick={() => handleLifecycleAction('request-promotion')} className="button-secondary">Request promotion to paper trading</button><button disabled={busy} onClick={() => handleLifecycleAction('promote')} className="button-primary"><Check className="size-3" />Promote (after approval)</button></div>}
        {canSignOffLive && stage === 'Paper Trading' && <div className="mt-4"><button disabled={busy} onClick={() => handleLifecycleAction('request-live')} className="button-secondary">Request live-eligibility sign-off</button></div>}
        {(stage === 'Paper Trading' || stage === 'Live Eligible') && <GoLiveEvidencePanel strategyId={strategy.id} stage={stage} canSignOff={canSignOffLive} onChanged={() => void reloadAll()} />}
        {(stage === 'Live Eligible' || stage === 'Live') && <LiveTradingPanel strategyId={strategy.id} canOperate={canOperate} canSignOffLive={canSignOffLive} />}
        {!canOperate && !canSignOffLive && <p className="mt-4 text-xs text-muted-foreground">Your role has read-only access to lifecycle actions.</p>}
      </div>}
    </div>
  </div>
}

