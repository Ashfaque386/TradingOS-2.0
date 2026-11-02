/**
 * What the Backtests page decides, kept free of React so a logic spec can drive it: the forms that
 * start a run, a walk-forward and an optimisation (and what each refuses before the server is asked),
 * the ledger of trades, and how a run's settings, a Monte Carlo result and an optimisation are
 * put into words.
 */
import type {
  BacktestRun,
  BacktestSignalConfig,
  BacktestTrade,
  BuiltinStrategy,
  LakeBacktestRequest,
  MonteCarloResult,
  OptimizationResult,
  OptimizeRequest,
  WalkForwardRequest,
} from './api'
import { formatPnl, formatRupees } from './orders'

export const STRATEGY_LABEL: Record<BuiltinStrategy, string> = {
  sma_crossover: 'Moving-average crossover',
  always_long: 'Always long',
}

/** The server will not run a backtest on fewer daily bars than this. */
export const MIN_BARS = 30
const DAY_MS = 86_400_000

// ---- A new backtest ----

export type BacktestForm = {
  strategyId: string
  symbol: string
  start: string
  end: string
  strategy: BuiltinStrategy
  smaWindow: string
  delivery: boolean
  capital: string
}

/** Today's date in India, as `YYYY-MM-DD`. */
export const indiaDay = (now: Date): string => now.toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })

/** Two years up to yesterday: a window the lake can usually cover, since its newest bar is the day before. */
export function defaultWindow(now: Date): { start: string; end: string } {
  const end = new Date(Date.parse(`${indiaDay(now)}T00:00:00Z`) - DAY_MS)
  const start = new Date(end.getTime())
  start.setUTCFullYear(start.getUTCFullYear() - 2)
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) }
}

export function emptyForm(now: Date): BacktestForm {
  return { strategyId: '', symbol: '', ...defaultWindow(now), strategy: 'sma_crossover', smaWindow: '15', delivery: true, capital: '100000' }
}

const isDay = (value: string): boolean => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`))
const isWholeNumber = (value: string): boolean => /^\d+$/.test(value.trim())

export type FormProblems = Record<string, string>

/** What is wrong with a backtest form, by field. Empty when it can be sent. */
export function formProblems(form: BacktestForm, now: Date): FormProblems {
  const problems: FormProblems = {}
  if (!form.strategyId) problems.strategyId = 'Pick the strategy this run belongs to.'
  if (!form.symbol.trim()) problems.symbol = 'Name the share the data lake holds prices for.'
  if (!isDay(form.start)) problems.start = 'Give the first day.'
  if (!isDay(form.end)) problems.end = 'Give the last day.'
  if (isDay(form.start) && isDay(form.end)) {
    if (form.end < form.start) problems.end = 'The last day is before the first.'
    else if (form.end > indiaDay(now)) problems.end = 'The last day is in the future: the lake cannot hold prices for it.'
    else if ((Date.parse(`${form.end}T00:00:00Z`) - Date.parse(`${form.start}T00:00:00Z`)) / DAY_MS < MIN_BARS)
      problems.end = `Too short: a backtest needs at least ${MIN_BARS} daily bars, which is a window of about ${Math.ceil(MIN_BARS * 1.5)} days.`
  }
  if (form.strategy === 'sma_crossover') {
    if (!isWholeNumber(form.smaWindow) || Number(form.smaWindow) < 2 || Number(form.smaWindow) > 200)
      problems.smaWindow = 'The average is a whole number of days, from 2 to 200.'
  }
  if (!(Number(form.capital) > 0)) problems.capital = 'Capital must be more than zero.'
  return problems
}

export function lakeRequest(form: BacktestForm, versionId: string): LakeBacktestRequest {
  const request: LakeBacktestRequest = {
    strategy_version_id: versionId,
    symbol: form.symbol.trim().toUpperCase(),
    start_date: form.start,
    end_date: form.end,
    strategy: form.strategy,
    is_delivery: form.delivery,
    initial_capital: Number(form.capital),
  }
  if (form.strategy === 'sma_crossover') request.sma_window = Number(form.smaWindow)
  return request
}

/** How a run's signals were made, in one line; a run from before this was kept says it cannot say. */
export function describeConfig(config: BacktestSignalConfig | null | undefined): string {
  if (!config) return 'Settings were not recorded for this run.'
  const strategy =
    config.strategy === 'sma_crossover' ? `${STRATEGY_LABEL.sma_crossover}, ${config.sma_window}-day average` : STRATEGY_LABEL[config.strategy]
  return [
    strategy,
    config.is_delivery ? 'delivery' : 'intraday',
    `capital ${formatRupees(config.initial_capital)}`,
    config.source === 'lake' ? 'prices from the data lake' : 'prices posted by a caller',
  ].join(' · ')
}

// ---- Walk-forward ----

export type WalkForwardForm = { train: string; test: string; step: string }
export const WALK_FORWARD_DEFAULTS: WalkForwardForm = { train: '120', test: '30', step: '30' }

/** The window length of a run, in the bars it used (the trades' span is not it): from its two dates. */
export function approximateBars(run: Pick<BacktestRun, 'start_date' | 'end_date'>): number {
  const days = (Date.parse(`${run.end_date}T00:00:00Z`) - Date.parse(`${run.start_date}T00:00:00Z`)) / DAY_MS
  return Math.round((days * 5) / 7)
}

export function walkForwardProblems(form: WalkForwardForm, run: Pick<BacktestRun, 'start_date' | 'end_date'>): FormProblems {
  const problems: FormProblems = {}
  for (const [field, label] of [['train', 'Training window'], ['test', 'Test window']] as const) {
    if (!isWholeNumber(form[field]) || Number(form[field]) < 1) problems[field] = `${label} is a whole number of bars, 1 or more.`
  }
  if (form.step.trim() !== '' && (!isWholeNumber(form.step) || Number(form.step) < 1)) problems.step = 'Step is a whole number of bars, 1 or more, or empty to step by the test window.'
  if (!problems.train && !problems.test) {
    const need = Number(form.train) + Number(form.test)
    const have = approximateBars(run)
    if (need > have) problems.test = `The training and test windows together (${need} bars) are longer than this run (about ${have} bars).`
  }
  return problems
}

export function walkForwardRequest(form: WalkForwardForm): WalkForwardRequest {
  const request: WalkForwardRequest = { train_bars: Number(form.train), test_bars: Number(form.test) }
  if (form.step.trim() !== '') request.step_bars = Number(form.step)
  return request
}

/** How many windows passed, and whether the run as a whole is robust. */
export function walkForwardSummary(result: NonNullable<BacktestRun['walk_forward_result']>): { passed: number; total: number; verdict: string } {
  const passed = result.windows.filter((w) => w.passed).length
  return { passed, total: result.windows.length, verdict: result.passed ? 'Robust' : 'Not robust' }
}

// ---- Optimise ----

export type OptimizeForm = {
  strategyId: string
  symbol: string
  start: string
  end: string
  trials: string
  objective: 'sharpe' | 'total_return' | 'cagr'
  windowMin: string
  windowMax: string
}

export const OBJECTIVE_LABEL: Record<OptimizeForm['objective'], string> = {
  sharpe: 'Sharpe ratio',
  total_return: 'Total return',
  cagr: 'Yearly growth (CAGR)',
}

export function emptyOptimizeForm(now: Date): OptimizeForm {
  return { strategyId: '', symbol: '', ...defaultWindow(now), trials: '30', objective: 'sharpe', windowMin: '5', windowMax: '50' }
}

export function optimizeProblems(form: OptimizeForm, now: Date): FormProblems {
  const base = formProblems({ ...emptyForm(now), strategyId: form.strategyId, symbol: form.symbol, start: form.start, end: form.end }, now)
  const problems: FormProblems = {}
  for (const key of ['strategyId', 'symbol', 'start', 'end'] as const) if (base[key]) problems[key] = base[key]
  if (!isWholeNumber(form.trials) || Number(form.trials) < 1 || Number(form.trials) > 200) problems.trials = 'Trials are a whole number, from 1 to 200.'
  if (!isWholeNumber(form.windowMin) || Number(form.windowMin) < 2) problems.windowMin = 'The shortest average is a whole number of days, 2 or more.'
  if (!isWholeNumber(form.windowMax) || Number(form.windowMax) < 2) problems.windowMax = 'The longest average is a whole number of days, 2 or more.'
  if (!problems.windowMin && !problems.windowMax && Number(form.windowMax) < Number(form.windowMin)) problems.windowMax = 'The longest average is shorter than the shortest.'
  return problems
}

export function optimizeRequest(form: OptimizeForm, versionId: string): OptimizeRequest {
  return {
    strategy_version_id: versionId,
    symbol: form.symbol.trim().toUpperCase(),
    start_date: form.start,
    end_date: form.end,
    n_trials: Number(form.trials),
    objective_metric: form.objective,
    sma_window_min: Number(form.windowMin),
    sma_window_max: Number(form.windowMax),
  }
}

/** The average length an optimisation found best, or null when it found none. */
export function bestWindow(result: Pick<OptimizationResult, 'best_params'>): number | null {
  const value = result.best_params?.window
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : null
}

/** What the optimisation found, in a sentence, with the caution that makes it honest: the best of many
 * tries on one history is the one that fit that history best. */
export function describeOptimization(result: OptimizationResult, objective: OptimizeForm['objective']): string {
  const window = bestWindow(result)
  if (window === null) return `${result.n_trials} tries found no average length that worked.`
  const value = result.best_value === null ? 'no score' : result.best_value.toFixed(2)
  return `Best of ${result.n_trials} tries: a ${window}-day average, ${OBJECTIVE_LABEL[objective].toLowerCase()} ${value}. It is the length that fitted this history best, so check it on a walk-forward before trusting it.`
}

// ---- Monte Carlo ----

export const PATH_CHOICES = [1000, 5000, 10000] as const

export function monteCarloRows(result: MonteCarloResult): { label: string; value: string; note?: string }[] {
  const pct = (v: number | null) => (v === null ? '—' : `${(v * 100).toFixed(1)}%`)
  const rupees = (v: number | null) => (v === null ? '—' : formatPnl(v))
  return [
    { label: 'Paths', value: result.n_paths.toLocaleString('en-IN'), note: 'Each reshuffles the same trades into a different order.' },
    { label: '95th percentile drawdown', value: pct(result.percentile_95_max_drawdown), note: 'In 95 of 100 orders the worst fall was no deeper than this.' },
    { label: 'Worst path drawdown', value: pct(result.worst_path_max_drawdown), note: 'The deepest fall in any single ordering.' },
    { label: 'Mean final P&L', value: rupees(result.mean_final_pnl) },
    { label: 'Median final P&L', value: rupees(result.median_final_pnl) },
  ]
}

// ---- The ledger ----

export const LEDGER_PAGE = 25

export type TradeSummary = {
  count: number
  wins: number
  losses: number
  net: number
  costs: number
  best: BacktestTrade | null
  worst: BacktestTrade | null
  averageDays: number | null
}

/** Calendar days a trade was held. */
export function holdingDays(trade: Pick<BacktestTrade, 'entry_date' | 'exit_date'>): number {
  return Math.max(0, Math.round((Date.parse(`${trade.exit_date}T00:00:00Z`) - Date.parse(`${trade.entry_date}T00:00:00Z`)) / DAY_MS))
}

export function summariseTrades(trades: BacktestTrade[]): TradeSummary {
  if (trades.length === 0) return { count: 0, wins: 0, losses: 0, net: 0, costs: 0, best: null, worst: null, averageDays: null }
  const sorted = [...trades].sort((a, b) => b.net_pnl - a.net_pnl)
  return {
    count: trades.length,
    wins: trades.filter((t) => t.net_pnl > 0).length,
    losses: trades.filter((t) => t.net_pnl < 0).length,
    net: trades.reduce((sum, t) => sum + t.net_pnl, 0),
    costs: trades.reduce((sum, t) => sum + t.costs, 0),
    best: sorted[0],
    worst: sorted[sorted.length - 1],
    averageDays: trades.reduce((sum, t) => sum + holdingDays(t), 0) / trades.length,
  }
}

/** Trades for the ledger: the run's dated trades when it kept them, else just its results (an older run). */
export function ledgerTrades(run: Pick<BacktestRun, 'trades' | 'trade_pnls'>): { dated: BacktestTrade[] | null; resultsOnly: number[] } {
  if (run.trades && run.trades.length > 0) return { dated: run.trades, resultsOnly: [] }
  return { dated: null, resultsOnly: run.trade_pnls ?? [] }
}

/** The run's equity, starting at 100 and compounding its daily returns (what the chart draws). */
export function equityCurve(run: Pick<BacktestRun, 'daily_returns'>): { date: string; equity: number }[] {
  if (!run.daily_returns || run.daily_returns.length === 0) return []
  let equity = 100
  return run.daily_returns.map(([date, ret]) => {
    equity *= 1 + ret
    return { date: date.slice(0, 10), equity: Number(equity.toFixed(2)) }
  })
}

/** The newest version of a strategy that passed validation, else its newest. */
export function runnableVersionId(strategy: { versions: { id: string; version_number: number; static_validation_passed: boolean }[] }): string | null {
  const versions = [...strategy.versions].sort((a, b) => b.version_number - a.version_number)
  return (versions.find((v) => v.static_validation_passed) ?? versions[0])?.id ?? null
}
