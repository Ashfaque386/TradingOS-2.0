'use client'

import { useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from 'recharts'
import { Check, FlaskConical, Plus, RefreshCw, SlidersHorizontal, Sparkles, Wand2, X } from 'lucide-react'
import { ChartContainer } from '@/components/ui/chart'
import { useAuth } from '@/components/auth/auth-provider'
import { NewBacktestDialog } from '@/components/backtests/new-backtest-dialog'
import { OptimizeDialog } from '@/components/backtests/optimize-dialog'
import { TradeLedger } from '@/components/backtests/trade-ledger'
import { WalkForwardDialog } from '@/components/backtests/walk-forward-dialog'
import {
  type BacktestRun,
  type ComparisonResult,
  type Strategy,
  ApiError,
  compareBacktests,
  listBacktests,
  listStrategies,
  runMonteCarlo,
} from '@/lib/api'
import { PATH_CHOICES, describeConfig, equityCurve, monteCarloRows, walkForwardSummary } from '@/lib/backtests'
import { sourceNote } from '@/lib/orders'
import { ErrorNotice } from '@/components/ui/error-notice'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'
import type { BacktestForm } from '@/lib/backtests'

function Panel({ title, eyebrow, children, className = '' }: { title: string; eyebrow?: string; children: React.ReactNode; className?: string }) {
  return <section className={`backtest-panel ${className}`}><div className="flex items-start justify-between gap-3 border-b border-white/8 px-4 py-3"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2 className="mt-1 text-sm font-semibold text-white">{title}</h2></div><SlidersHorizontal className="size-4 text-slate-500" /></div>{children}</section>
}

function fmtPct(v: number | null): string {
  return v === null ? '—' : `${(v * 100).toFixed(1)}%`
}
function fmtNum(v: number | null, digits = 2): string {
  return v === null ? '—' : v.toFixed(digits)
}

type Dialog =
  | { kind: 'new'; initial?: Partial<BacktestForm> }
  | { kind: 'optimize' }
  | { kind: 'walk-forward'; run: BacktestRun }
  | null

export default function BacktestsPage() {
  const { role } = useAuth()
  // Starting a run, a walk-forward, an optimisation or a Monte Carlo is for the administrator and the
  // portfolio manager (the same list on the server); the others read.
  const canCreate = can(role, 'POST /api/v1/backtests/from-lake')
  const canWalkForward = can(role, 'POST /api/v1/backtests/{run_id}/walk-forward')
  const canOptimize = can(role, 'POST /api/v1/backtests/optimize')
  const canOperate = can(role, 'POST /api/v1/backtests/{run_id}/monte-carlo')
  const [strategyFilter, setStrategyFilter] = useState<string>('all')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [comparison, setComparison] = useState<ComparisonResult | null>(null)
  const [running, setRunning] = useState(false)
  const [paths, setPaths] = useState<number>(PATH_CHOICES[0])
  const [dialog, setDialog] = useState<Dialog>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data: loaded, error: loadError, refresh: reload } = usePolling(
    async () => {
      const [runs, strategies] = await Promise.all([listBacktests(), listStrategies()])
      return { runs, strategies }
    },
    { intervalMs: 15_000 },
  )
  const runs: BacktestRun[] = useMemo(() => loaded?.runs ?? [], [loaded])
  const strategies: Strategy[] = useMemo(() => loaded?.strategies ?? [], [loaded])

  // A strategy has many versions and a run belongs to one of them: filter by all of the strategy's versions.
  const filteredRuns = useMemo(() => {
    if (strategyFilter === 'all') return runs
    const versionIds = new Set(strategies.find((s) => s.id === strategyFilter)?.versions.map((v) => v.id))
    return runs.filter((r) => versionIds.has(r.strategy_version_id))
  }, [runs, strategies, strategyFilter])
  const selectedRun = runs.find((r) => r.id === selectedRunId) ?? runs[0] ?? null
  const curve = selectedRun ? equityCurve(selectedRun) : []
  const completed = selectedRun?.status === 'completed'

  async function handleRunMonteCarlo() {
    if (!selectedRun) return
    setRunning(true)
    setError(null)
    try {
      await runMonteCarlo(selectedRun.id, paths)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Monte Carlo run failed')
    } finally {
      setRunning(false)
    }
  }

  async function handleCompare() {
    if (compareIds.length < 2) return
    setError(null)
    try {
      setComparison(await compareBacktests(compareIds))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Comparison failed')
    }
  }

  const afterRun = async (run: BacktestRun, message: string) => {
    setDialog(null)
    setSelectedRunId(run.id)
    setStrategyFilter('all')
    setNotice(message)
    await reload()
  }

  const metrics = selectedRun?.metrics
  const metricCards: [string, string][] = metrics ? [
    ['Total return', fmtPct(metrics.total_return)],
    ['Sharpe', fmtNum(metrics.sharpe)],
    ['CAGR', fmtPct(metrics.cagr)],
    ['Max Drawdown', fmtPct(metrics.max_drawdown)],
    ['Win Rate', fmtPct(metrics.win_rate)],
    ['Profit Factor', fmtNum(metrics.profit_factor)],
    ['Trades', String(metrics.num_trades)],
  ] : []
  const source = selectedRun ? sourceNote(selectedRun.data_source ?? 'unknown') : null
  const wfSummary = selectedRun?.walk_forward_result ? walkForwardSummary(selectedRun.walk_forward_result) : null

  return <><main className="mx-auto w-full max-w-[1700px] pb-10">
    <header className="backtest-header">
      <div>
        <p className="eyebrow">TRADINGOS // QUANT RESEARCH</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-[-.04em] text-white">Backtests</h1>
        <p className="mt-2 text-sm text-slate-400">A strategy run over the daily prices the data lake holds, with real Indian trading costs and engine-computed metrics.</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Strategy" value={strategyFilter} onChange={e => setStrategyFilter(e.target.value)} className="backtest-select"><option value="all">All strategies</option>{strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
        <select aria-label="Run" value={selectedRunId ?? ''} onChange={e => setSelectedRunId(e.target.value)} className="backtest-select">{filteredRuns.map(r => <option key={r.id} value={r.id}>{r.symbol} · {r.id.slice(0, 8)} · {r.status} · data: {r.data_source ?? 'unknown'}</option>)}</select>
        {canCreate && <button type="button" className="backtest-primary" onClick={() => setDialog({ kind: 'new' })}><Plus className="size-4" />New backtest</button>}
        {canOptimize && <button type="button" className="button-secondary" onClick={() => setDialog({ kind: 'optimize' })}><Wand2 className="size-4" />Find best length</button>}
      </div>
    </header>
    {notice && <div className="pt-alert pt-alert-info mb-4" role="status" style={{ gridTemplateColumns: 'auto 1fr auto' }}><span aria-hidden="true" /><div><p style={{ marginTop: 0, color: '#e2e8f0' }}>{notice}</p></div><button type="button" className="button-secondary" onClick={() => setNotice(null)}>Dismiss</button></div>}
    {error && <ErrorNotice className="mb-4" message={error} />}
    {loadError && <ErrorNotice className="mb-4" message={`Could not load the backtests: ${loadError}`} onRetry={() => void reload()} />}
    {loaded && !selectedRun && (
      <div className="rounded-xl border border-white/10 p-8 text-center text-sm text-muted-foreground">
        <p>No backtest runs yet.</p>
        {canCreate ? <button type="button" className="backtest-primary mx-auto mt-4" onClick={() => setDialog({ kind: 'new' })}><Plus className="size-4" />Run the first backtest</button> : <p className="mt-2">An administrator or portfolio manager can start one.</p>}
      </div>
    )}
    {selectedRun && <>
      <div className="bt-run-strip" aria-label="This run">
        <strong>{selectedRun.symbol}</strong>
        <span>{selectedRun.start_date} to {selectedRun.end_date}</span>
        {source && <span className="source-chip" data-caution={source.caution} title={source.explanation}>{source.label}</span>}
        <span className="bt-config">{describeConfig(selectedRun.signal_config)}</span>
      </div>
      {selectedRun.refusal_reason && <ErrorNotice className="mb-4" message={`This run was refused, so it has no result: ${selectedRun.refusal_reason}`} />}
      <div className="metrics-grid">{metricCards.map(([name, value]) => <article className="backtest-metric" key={name}><span>{name}</span><strong>{value}</strong></article>)}</div>
      <div className="backtest-main-grid">
        <Panel title="Equity curve" eyebrow="DAILY RETURNS, COMPOUNDED" className="equity-panel">
          {curve.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No daily-return series recorded for this run.</p> : <ChartContainer config={{ equity: { label: 'Equity', color: 'var(--chart-1)' } }} className="h-[260px] w-full px-2"><AreaChart data={curve} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}><CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} /><XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} /><Tooltip /><Area type="monotone" dataKey="equity" stroke="var(--color-equity)" fill="var(--color-equity)" fillOpacity={.18} /></AreaChart></ChartContainer>}
        </Panel>
        <Panel title="Monte Carlo stress" eyebrow={selectedRun.monte_carlo_result ? `${selectedRun.monte_carlo_result.n_paths.toLocaleString('en-IN')} PATHS` : 'NOT RUN'} className="monte-panel">
          {selectedRun.monte_carlo_result ? (
            <dl className="bt-facts" aria-label="Monte Carlo result">
              {monteCarloRows(selectedRun.monte_carlo_result).map((row) => (
                <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd>{row.note && <small>{row.note}</small>}</div>
              ))}
            </dl>
          ) : <p className="p-4 text-xs text-muted-foreground">Not run yet. It reshuffles this run’s trades into many different orders to see how deep the worst fall could have been.</p>}
          <div className="bt-actions">
            <label className="bt-path-pick"><span>Paths</span>
              <select aria-label="Monte Carlo paths" value={paths} onChange={(e) => setPaths(Number(e.target.value))} disabled={!canOperate || !completed}>{PATH_CHOICES.map((n) => <option key={n} value={n}>{n.toLocaleString('en-IN')}</option>)}</select>
            </label>
            <button type="button" onClick={handleRunMonteCarlo} disabled={running || !canOperate || !completed} title={canOperate ? (completed ? undefined : 'A refused run has no trades to reshuffle') : 'Your role does not have Monte Carlo run access'} className="button-secondary">{running ? <RefreshCw className="size-4 animate-spin" /> : <Sparkles className="size-4" />}{running ? 'Running...' : selectedRun.monte_carlo_result ? 'Run again' : 'Run Monte Carlo'}</button>
          </div>
        </Panel>
      </div>
      <Panel title="Walk-forward optimization" eyebrow="ROBUSTNESS" className="bt-stack">
        {!selectedRun.walk_forward_result ? <p className="p-4 text-xs text-muted-foreground">Not run. It cuts the history into windows and tests each on bars it was not trained on.</p> : <><div className="overflow-x-auto"><table className="backtest-table"><thead><tr><th>Test window</th><th>Expectancy</th><th>Trades</th><th>Gate</th></tr></thead><tbody>{selectedRun.walk_forward_result.windows.map((w, i) => <tr key={i}><td>{w.test_start.slice(0, 10)} → {w.test_end.slice(0, 10)}</td><td>{fmtNum(w.out_of_sample_expectancy, 0)}</td><td>{w.num_trades}</td><td>{w.passed ? <Check className="size-4 text-emerald-300" aria-label="passed" /> : <X className="size-4 text-rose-300" aria-label="failed" />}</td></tr>)}</tbody></table></div>{wfSummary && <div className="wf-summary"><span>{wfSummary.passed} / {wfSummary.total} windows passed</span><strong>{wfSummary.verdict}</strong></div>}</>}
        {canWalkForward && <div className="bt-actions"><button type="button" className="button-secondary" disabled={!completed} title={completed ? undefined : 'A refused run has no trades to test'} onClick={() => setDialog({ kind: 'walk-forward', run: selectedRun })}><FlaskConical className="size-4" />{selectedRun.walk_forward_result ? 'Run walk-forward again' : 'Run walk-forward'}</button></div>}
      </Panel>
      <Panel title="Trade ledger" eyebrow="EVERY TRADE, WITH ITS DATES AND COSTS" className="bt-stack">
        <TradeLedger key={selectedRun.id} run={selectedRun} />
      </Panel>
      <Panel title="Compare runs" eyebrow="RESEARCH WORKBENCH" className="compare-panel">
        <div className="compare-toolbar">{runs.map((r, i) => <button key={r.id} className={compareIds.includes(r.id) ? 'run-chip selected' : 'run-chip'} onClick={() => setCompareIds(prev => prev.includes(r.id) ? prev.filter(x => x !== r.id) : [...prev, r.id])}><span className={`run-dot dot-${i % 6}`} />{r.symbol} · {r.id.slice(0, 6)}</button>)}</div>
        <div className="mt-3 flex items-center gap-2"><button onClick={handleCompare} disabled={compareIds.length < 2} className="button-secondary">Compare selected ({compareIds.length})</button></div>
        {comparison && <div className="mt-4"><p className="eyebrow mb-2">PAIRWISE CORRELATION</p><div className="flex flex-col gap-1 font-mono text-xs">{Object.entries(comparison.correlations).map(([pair, value]) => <div key={pair} className="flex justify-between border-b border-white/5 py-1"><span>{pair}</span>{value === null ? <span className="text-muted-foreground" title="Fewer than 10 overlapping days">n/a · under 10 shared days</span> : <span style={{ opacity: 0.4 + Math.abs(value) * 0.6 }}>{value.toFixed(2)}</span>}</div>)}</div></div>}
      </Panel>
    </>}
  </main>
  {dialog?.kind === 'new' && (
    <NewBacktestDialog
      strategies={strategies}
      initial={dialog.initial}
      onClose={() => setDialog(null)}
      onDone={(run) => void afterRun(run, run.status === 'completed' ? `Ran ${run.symbol}: ${run.metrics?.num_trades ?? 0} trades.` : `${run.symbol} was refused, so it has no result. The reason is on the run.`)}
    />
  )}
  {dialog?.kind === 'optimize' && (
    <OptimizeDialog
      strategies={strategies}
      onClose={() => setDialog(null)}
      onUse={(f) => setDialog({ kind: 'new', initial: { strategyId: f.strategyId, symbol: f.symbol, start: f.start, end: f.end, smaWindow: String(f.smaWindow) } })}
    />
  )}
  {dialog?.kind === 'walk-forward' && (
    <WalkForwardDialog
      run={dialog.run}
      onClose={() => setDialog(null)}
      onDone={(run) => void afterRun(run, `Walk-forward on ${run.symbol} is on the run below.`)}
    />
  )}
  </>
}
