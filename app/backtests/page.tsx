'use client'

import { useEffect, useMemo, useState } from 'react'
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from 'recharts'
import { Check, RefreshCw, SlidersHorizontal, Sparkles, X } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { ChartContainer } from '@/components/ui/chart'
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

function Panel({ title, eyebrow, children, className = '' }: { title: string; eyebrow?: string; children: React.ReactNode; className?: string }) {
  return <section className={`backtest-panel ${className}`}><div className="flex items-start justify-between gap-3 border-b border-white/8 px-4 py-3"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2 className="mt-1 text-sm font-semibold text-white">{title}</h2></div><SlidersHorizontal className="size-4 text-slate-500" /></div>{children}</section>
}

function equityCurve(run: BacktestRun): { date: string; equity: number }[] {
  if (!run.daily_returns || run.daily_returns.length === 0) return []
  let equity = 100
  return run.daily_returns.map(([date, ret]) => {
    equity *= 1 + ret
    return { date, equity: Number(equity.toFixed(2)) }
  })
}

function fmtPct(v: number | null): string {
  return v === null ? '—' : `${(v * 100).toFixed(1)}%`
}
function fmtNum(v: number | null, digits = 2): string {
  return v === null ? '—' : v.toFixed(digits)
}

export default function BacktestsPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [runs, setRuns] = useState<BacktestRun[]>([])
  const [strategyFilter, setStrategyFilter] = useState<string>('all')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [comparison, setComparison] = useState<ComparisonResult | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    const data = await listBacktests()
    setRuns(data)
    setSelectedRunId((prev) => prev ?? data[0]?.id ?? null)
  }

  useEffect(() => {
    listStrategies().then(setStrategies)
    reload()
    const interval = setInterval(reload, 15000)
    return () => clearInterval(interval)
  }, [])

  const filteredRuns = strategyFilter === 'all' ? runs : runs.filter((r) => r.strategy_version_id === strategyFilter)
  const selectedRun = runs.find((r) => r.id === selectedRunId) ?? null
  const curve = selectedRun ? equityCurve(selectedRun) : []

  async function handleRunMonteCarlo() {
    if (!selectedRun) return
    setRunning(true)
    setError(null)
    try {
      await runMonteCarlo(selectedRun.id)
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

  const metrics = selectedRun?.metrics
  const metricCards: [string, string][] = metrics ? [
    ['Sharpe', fmtNum(metrics.sharpe)],
    ['CAGR', fmtPct(metrics.cagr)],
    ['Max Drawdown', fmtPct(metrics.max_drawdown)],
    ['Win Rate', fmtPct(metrics.win_rate)],
    ['Profit Factor', fmtNum(metrics.profit_factor)],
    ['Trades', String(metrics.num_trades)],
  ] : []

  return <ShellLayout><main className="mx-auto w-full max-w-[1700px] pb-10">
    <header className="backtest-header"><div><p className="eyebrow">TRADINGOS // QUANT RESEARCH</p><h1 className="mt-2 text-3xl font-semibold tracking-[-.04em] text-white">Backtests</h1><p className="mt-2 text-sm text-slate-400">Real backtest runs, real engine-computed metrics — creating a fresh run requires OHLCV bars, which this console doesn't yet collect (POST /backtests API only).</p></div><div className="flex flex-wrap items-center gap-2"><select value={strategyFilter} onChange={e => setStrategyFilter(e.target.value)} className="backtest-select"><option value="all">All strategies</option>{strategies.map(s => <option key={s.id} value={s.versions[s.versions.length - 1]?.id}>{s.name}</option>)}</select><select value={selectedRunId ?? ''} onChange={e => setSelectedRunId(e.target.value)} className="backtest-select">{filteredRuns.map(r => <option key={r.id} value={r.id}>{r.symbol} · {r.id.slice(0, 8)} · {r.status}</option>)}</select><button onClick={handleRunMonteCarlo} disabled={running || !selectedRun} className="backtest-primary">{running ? <RefreshCw className="size-4 animate-spin" /> : <Sparkles className="size-4" />}{running ? 'Running...' : 'Run Monte Carlo'}</button></div></header>
    {error && <div className="mb-4 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}
    {!selectedRun && <div className="rounded-xl border border-white/10 p-8 text-center text-sm text-muted-foreground">No backtest runs yet. Create one via POST /api/v1/backtests with historical bars.</div>}
    {selectedRun && <>
      <div className="metrics-grid">{metricCards.map(([name, value]) => <article className="backtest-metric" key={name}><span>{name}</span><strong>{value}</strong></article>)}</div>
      <div className="backtest-main-grid">
        <Panel title="Equity curve" eyebrow="DAILY RETURNS, COMPOUNDED" className="equity-panel">
          {curve.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No daily-return series recorded for this run.</p> : <ChartContainer config={{ equity: { label: 'Equity', color: 'var(--chart-1)' } }} className="h-[260px] w-full px-2"><AreaChart data={curve} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}><CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} /><XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} /><YAxis tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} /><Tooltip /><Area type="monotone" dataKey="equity" stroke="var(--color-equity)" fill="var(--color-equity)" fillOpacity={.18} /></AreaChart></ChartContainer>}
        </Panel>
        <Panel title="Monte Carlo stress" eyebrow={selectedRun.monte_carlo_result ? `${selectedRun.monte_carlo_result.n_paths} PATHS` : 'NOT RUN'} className="monte-panel">
          {selectedRun.monte_carlo_result ? <div className="monte-callout"><Sparkles className="size-4" /><div><span>95th percentile drawdown</span><strong>{fmtPct(selectedRun.monte_carlo_result.percentile_95_max_drawdown)}</strong><small>Mean final P&L {fmtNum(selectedRun.monte_carlo_result.mean_final_pnl, 0)} · Median {fmtNum(selectedRun.monte_carlo_result.median_final_pnl, 0)}</small></div></div> : <p className="p-4 text-xs text-muted-foreground">Not run yet — click "Run Monte Carlo" above.</p>}
        </Panel>
      </div>
      <div className="backtest-two-col">
        <Panel title="Trade P&L ledger" eyebrow="PER-TRADE NET P&L">
          {!selectedRun.trade_pnls || selectedRun.trade_pnls.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No trades recorded.</p> : <div className="overflow-x-auto"><table className="backtest-table"><thead><tr><th>#</th><th>Net P&L</th></tr></thead><tbody>{selectedRun.trade_pnls.map((pnl, i) => <tr key={i}><td>{i + 1}</td><td className={pnl < 0 ? 'loss' : 'gain'}>{pnl.toFixed(0)}</td></tr>)}</tbody></table></div>}
        </Panel>
        <Panel title="Walk-forward optimization" eyebrow="ROBUSTNESS">
          {!selectedRun.walk_forward_result ? <p className="p-4 text-xs text-muted-foreground">Not run — walk-forward requires submitting historical bars via POST /backtests/{'{run_id}'}/walk-forward.</p> : <><div className="overflow-x-auto"><table className="backtest-table"><thead><tr><th>Test window</th><th>Expectancy</th><th>Trades</th><th>Gate</th></tr></thead><tbody>{selectedRun.walk_forward_result.windows.map((w, i) => <tr key={i}><td>{w.test_start} → {w.test_end}</td><td>{fmtNum(w.out_of_sample_expectancy, 0)}</td><td>{w.num_trades}</td><td>{w.passed ? <Check className="size-4 text-emerald-300" /> : <X className="size-4 text-rose-300" />}</td></tr>)}</tbody></table></div><div className="wf-summary"><span>{selectedRun.walk_forward_result.windows.filter(w => w.passed).length} / {selectedRun.walk_forward_result.windows.length} windows passed</span><strong>{selectedRun.walk_forward_result.passed ? 'Robust' : 'Not robust'}</strong></div></>}
        </Panel>
      </div>
      <Panel title="Compare runs" eyebrow="RESEARCH WORKBENCH" className="compare-panel">
        <div className="compare-toolbar">{runs.map((r, i) => <button key={r.id} className={compareIds.includes(r.id) ? 'run-chip selected' : 'run-chip'} onClick={() => setCompareIds(prev => prev.includes(r.id) ? prev.filter(x => x !== r.id) : [...prev, r.id])}><span className={`run-dot dot-${i % 6}`} />{r.symbol} · {r.id.slice(0, 6)}</button>)}</div>
        <div className="mt-3 flex items-center gap-2"><button onClick={handleCompare} disabled={compareIds.length < 2} className="button-secondary">Compare selected ({compareIds.length})</button></div>
        {comparison && <div className="mt-4"><p className="eyebrow mb-2">PAIRWISE CORRELATION</p><div className="flex flex-col gap-1 font-mono text-xs">{Object.entries(comparison.correlations).map(([pair, value]) => <div key={pair} className="flex justify-between border-b border-white/5 py-1"><span>{pair}</span><span style={{ opacity: 0.4 + Math.abs(value) * 0.6 }}>{value.toFixed(2)}</span></div>)}</div></div>}
      </Panel>
    </>}
  </main></ShellLayout>
}
