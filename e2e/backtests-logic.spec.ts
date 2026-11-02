import { expect, test } from '@playwright/test'
import type { BacktestRun, BacktestTrade, MonteCarloResult, OptimizationResult } from '../lib/api'
import {
  type BacktestForm,
  LEDGER_PAGE,
  MIN_BARS,
  PATH_CHOICES,
  WALK_FORWARD_DEFAULTS,
  approximateBars,
  bestWindow,
  defaultWindow,
  describeConfig,
  describeOptimization,
  emptyForm,
  emptyOptimizeForm,
  equityCurve,
  formProblems,
  holdingDays,
  lakeRequest,
  ledgerTrades,
  monteCarloRows,
  optimizeProblems,
  optimizeRequest,
  runnableVersionId,
  summariseTrades,
  walkForwardProblems,
  walkForwardRequest,
  walkForwardSummary,
} from '../lib/backtests'

// What the Backtests page decides, with no browser and no backend: each case states the answer worked out by hand.

const NOW = new Date('2026-11-02T04:00:00Z') // 09:30 in India, Monday 2 November 2026

const form = (over: Partial<BacktestForm> = {}): BacktestForm => ({
  ...emptyForm(NOW),
  strategyId: 's-1',
  symbol: 'RELIANCE',
  ...over,
})

const trade = (over: Partial<BacktestTrade> = {}): BacktestTrade => ({
  entry_date: '2026-01-05',
  exit_date: '2026-01-15',
  entry_price: 100,
  exit_price: 110,
  quantity: 10,
  gross_pnl: 100,
  costs: 12,
  net_pnl: 88,
  ...over,
})

test.describe('the default window', () => {
  test('is two years up to yesterday in India', () => {
    expect(defaultWindow(NOW)).toEqual({ start: '2024-11-01', end: '2026-11-01' })
  })

  test('uses India’s date, not the browser’s, near midnight', () => {
    // 20:00 UTC on the 2nd is already 01:30 on the 3rd in India
    expect(defaultWindow(new Date('2026-11-02T20:00:00Z'))).toEqual({ start: '2024-11-02', end: '2026-11-02' })
  })
})

test.describe('a new backtest form', () => {
  test('as it opens needs a strategy and a share, and nothing else', () => {
    expect(Object.keys(formProblems(emptyForm(NOW), NOW)).sort()).toEqual(['strategyId', 'symbol'])
  })

  test('a filled form has no problems', () => {
    expect(formProblems(form(), NOW)).toEqual({})
  })

  test('says a last day before the first, and one in the future', () => {
    expect(formProblems(form({ start: '2026-05-01', end: '2026-04-01' }), NOW).end).toMatch(/before the first/)
    expect(formProblems(form({ end: '2026-11-03' }), NOW).end).toMatch(/in the future/)
  })

  test('says a window too short to hold the minimum number of bars', () => {
    expect(MIN_BARS).toBe(30)
    expect(formProblems(form({ start: '2026-10-01', end: '2026-10-20' }), NOW).end).toMatch(/at least 30 daily bars/)
    expect(formProblems(form({ start: '2026-09-01', end: '2026-10-20' }), NOW).end).toBeUndefined()
  })

  test('an average must be a whole number of days from 2 to 200, but only the crossover uses one', () => {
    for (const bad of ['', '1', '201', '7.5', 'abc', '-3']) {
      expect(formProblems(form({ smaWindow: bad }), NOW).smaWindow, bad).toBeDefined()
    }
    expect(formProblems(form({ smaWindow: '2' }), NOW).smaWindow).toBeUndefined()
    expect(formProblems(form({ smaWindow: '200' }), NOW).smaWindow).toBeUndefined()
    expect(formProblems(form({ strategy: 'always_long', smaWindow: 'abc' }), NOW).smaWindow).toBeUndefined()
  })

  test('capital must be more than zero', () => {
    expect(formProblems(form({ capital: '0' }), NOW).capital).toBeDefined()
    expect(formProblems(form({ capital: '' }), NOW).capital).toBeDefined()
    expect(formProblems(form({ capital: '250000' }), NOW).capital).toBeUndefined()
  })

  test('is sent with the share in capitals, the numbers as numbers, and the average only for the crossover', () => {
    expect(lakeRequest(form({ symbol: ' reliance ', smaWindow: '10', capital: '250000', delivery: false }), 'v-9')).toEqual({
      strategy_version_id: 'v-9',
      symbol: 'RELIANCE',
      start_date: '2024-11-01',
      end_date: '2026-11-01',
      strategy: 'sma_crossover',
      is_delivery: false,
      initial_capital: 250000,
      sma_window: 10,
    })
    expect(lakeRequest(form({ strategy: 'always_long' }), 'v-9')).not.toHaveProperty('sma_window')
  })
})

test.describe('how a run was made', () => {
  test('is said in one line', () => {
    expect(describeConfig({ strategy: 'sma_crossover', sma_window: 15, is_delivery: true, initial_capital: 100000, source: 'lake' })).toBe(
      'Moving-average crossover, 15-day average · delivery · capital ₹1,00,000.00 · prices from the data lake',
    )
    expect(describeConfig({ strategy: 'always_long', sma_window: 15, is_delivery: false, initial_capital: 50000, source: 'supplied' })).toBe(
      'Always long · intraday · capital ₹50,000.00 · prices posted by a caller',
    )
  })

  test('a run from before the settings were kept says so', () => {
    expect(describeConfig(null)).toBe('Settings were not recorded for this run.')
  })
})

test.describe('a walk-forward', () => {
  const run = { start_date: '2024-01-01', end_date: '2024-12-31' } // 365 days: about 261 bars

  test('guesses a run’s length in trading bars from its dates', () => {
    expect(approximateBars(run)).toBe(261)
  })

  test('opens on 120, 30 and 30', () => {
    expect(WALK_FORWARD_DEFAULTS).toEqual({ train: '120', test: '30', step: '30' })
    expect(walkForwardProblems(WALK_FORWARD_DEFAULTS, run)).toEqual({})
  })

  test('refuses windows that are not whole positive numbers', () => {
    expect(walkForwardProblems({ train: '0', test: '30', step: '' }, run).train).toBeDefined()
    expect(walkForwardProblems({ train: '120', test: '2.5', step: '' }, run).test).toBeDefined()
    expect(walkForwardProblems({ train: '120', test: '30', step: 'x' }, run).step).toBeDefined()
  })

  test('refuses windows that together outrun the run', () => {
    expect(walkForwardProblems({ train: '250', test: '30', step: '' }, run).test).toMatch(/longer than this run/)
  })

  test('an empty step is left out of the request, a given one is sent', () => {
    expect(walkForwardRequest({ train: '120', test: '30', step: '' })).toEqual({ train_bars: 120, test_bars: 30 })
    expect(walkForwardRequest({ train: '120', test: '30', step: '15' })).toEqual({ train_bars: 120, test_bars: 30, step_bars: 15 })
  })

  test('says how many windows passed and the verdict', () => {
    const window = (passed: boolean) => ({ train_start: 'a', train_end: 'b', test_start: 'c', test_end: 'd', out_of_sample_expectancy: 1, num_trades: 3, passed })
    expect(walkForwardSummary({ passed: false, windows: [window(true), window(false), window(true)] })).toEqual({ passed: 2, total: 3, verdict: 'Not robust' })
    expect(walkForwardSummary({ passed: true, windows: [window(true)] })).toEqual({ passed: 1, total: 1, verdict: 'Robust' })
  })
})

test.describe('an optimisation', () => {
  const filled = { ...emptyOptimizeForm(NOW), strategyId: 's-1', symbol: 'INFY' }

  test('as it opens needs a strategy and a share', () => {
    expect(Object.keys(optimizeProblems(emptyOptimizeForm(NOW), NOW)).sort()).toEqual(['strategyId', 'symbol'])
    expect(optimizeProblems(filled, NOW)).toEqual({})
  })

  test('refuses tries out of range and a range of averages that runs backwards', () => {
    expect(optimizeProblems({ ...filled, trials: '0' }, NOW).trials).toBeDefined()
    expect(optimizeProblems({ ...filled, trials: '201' }, NOW).trials).toBeDefined()
    expect(optimizeProblems({ ...filled, trials: '200' }, NOW).trials).toBeUndefined()
    expect(optimizeProblems({ ...filled, windowMin: '30', windowMax: '10' }, NOW).windowMax).toMatch(/shorter than the shortest/)
    expect(optimizeProblems({ ...filled, windowMin: '1' }, NOW).windowMin).toBeDefined()
  })

  test('shares the window rules with a backtest', () => {
    expect(optimizeProblems({ ...filled, start: '2026-10-01', end: '2026-10-20' }, NOW).end).toMatch(/at least 30 daily bars/)
  })

  test('is sent with the numbers as numbers and the share in capitals', () => {
    expect(optimizeRequest({ ...filled, symbol: 'infy', trials: '12', objective: 'cagr', windowMin: '6', windowMax: '40' }, 'v-9')).toEqual({
      strategy_version_id: 'v-9',
      symbol: 'INFY',
      start_date: '2024-11-01',
      end_date: '2026-11-01',
      n_trials: 12,
      objective_metric: 'cagr',
      sma_window_min: 6,
      sma_window_max: 40,
    })
  })

  const found = (over: Partial<OptimizationResult> = {}): OptimizationResult => ({ id: 'o', n_trials: 30, best_params: { window: 17 }, best_value: 1.234, param_importance: null, ...over })

  test('the best length is the window the search found, as a whole number', () => {
    expect(bestWindow(found())).toBe(17)
    expect(bestWindow(found({ best_params: { window: 17.4 } }))).toBe(17)
    expect(bestWindow(found({ best_params: null }))).toBeNull()
    expect(bestWindow(found({ best_params: { window: 'x' } }))).toBeNull()
  })

  test('the result says what was found and warns that it fits the history it was found on', () => {
    expect(describeOptimization(found(), 'sharpe')).toBe(
      'Best of 30 tries: a 17-day average, sharpe ratio 1.23. It is the length that fitted this history best, so check it on a walk-forward before trusting it.',
    )
    expect(describeOptimization(found({ best_params: null }), 'sharpe')).toBe('30 tries found no average length that worked.')
  })
})

test.describe('Monte Carlo', () => {
  const result: MonteCarloResult = { n_paths: 5000, percentile_95_max_drawdown: 0.123, worst_path_max_drawdown: 0.31, mean_final_pnl: 1250, median_final_pnl: -300.5 }

  test('offers a few path counts, the smallest first', () => {
    expect([...PATH_CHOICES]).toEqual([1000, 5000, 10000])
  })

  test('shows every figure with what it means', () => {
    const rows = monteCarloRows(result)
    expect(rows.map((r) => [r.label, r.value])).toEqual([
      ['Paths', '5,000'],
      ['95th percentile drawdown', '12.3%'],
      ['Worst path drawdown', '31.0%'],
      ['Mean final P&L', '+₹1,250.00'],
      ['Median final P&L', '-₹300.50'],
    ])
    expect(rows[2].note).toMatch(/deepest fall in any single ordering/)
  })

  test('a figure that could not be worked out is a dash, not zero', () => {
    const rows = monteCarloRows({ ...result, percentile_95_max_drawdown: null, worst_path_max_drawdown: null, mean_final_pnl: null })
    expect(rows[1].value).toBe('—')
    expect(rows[2].value).toBe('—')
    expect(rows[3].value).toBe('—')
  })
})

test.describe('the ledger', () => {
  test('counts wins and losses and adds up the net result and the costs', () => {
    const summary = summariseTrades([trade({ net_pnl: 88, costs: 12 }), trade({ net_pnl: -40, costs: 10 }), trade({ net_pnl: 0, costs: 5 })])
    expect(summary).toMatchObject({ count: 3, wins: 1, losses: 1, net: 48, costs: 27 })
    expect(summary.best?.net_pnl).toBe(88)
    expect(summary.worst?.net_pnl).toBe(-40)
  })

  test('averages how long trades were held', () => {
    const summary = summariseTrades([trade({ entry_date: '2026-01-01', exit_date: '2026-01-11' }), trade({ entry_date: '2026-02-01', exit_date: '2026-02-06' })])
    expect(summary.averageDays).toBe(7.5)
    expect(holdingDays(trade({ entry_date: '2026-01-05', exit_date: '2026-01-05' }))).toBe(0)
  })

  test('no trades is an empty summary, not a division by zero', () => {
    expect(summariseTrades([])).toEqual({ count: 0, wins: 0, losses: 0, net: 0, costs: 0, best: null, worst: null, averageDays: null })
  })

  test('a run that kept its trades shows them with their dates', () => {
    const trades = [trade()]
    expect(ledgerTrades({ trades, trade_pnls: [88] })).toEqual({ dated: trades, resultsOnly: [] })
  })

  test('an older run shows only its results, and says it has no dates', () => {
    expect(ledgerTrades({ trades: null, trade_pnls: [88, -40] })).toEqual({ dated: null, resultsOnly: [88, -40] })
    expect(ledgerTrades({ trades: [], trade_pnls: null })).toEqual({ dated: null, resultsOnly: [] })
  })

  test('is shown 25 trades at a time', () => {
    expect(LEDGER_PAGE).toBe(25)
  })
})

test.describe('the equity curve', () => {
  const run = (daily_returns: BacktestRun['daily_returns']) => ({ daily_returns })

  test('starts at 100 and compounds each day’s return', () => {
    expect(equityCurve(run([['2026-01-01T00:00:00', 0.1], ['2026-01-02T00:00:00', -0.1]]))).toEqual([
      { date: '2026-01-01', equity: 110 },
      { date: '2026-01-02', equity: 99 },
    ])
  })

  test('no returns is no curve', () => {
    expect(equityCurve(run(null))).toEqual([])
    expect(equityCurve(run([]))).toEqual([])
  })
})

test.describe('which version of a strategy a run belongs to', () => {
  const version = (id: string, version_number: number, static_validation_passed: boolean) => ({ id, version_number, static_validation_passed })

  test('is the newest that passed validation', () => {
    expect(runnableVersionId({ versions: [version('a', 1, true), version('b', 2, true), version('c', 3, false)] })).toBe('b')
  })

  test('is the newest of all when none passed, and nothing when there are none', () => {
    expect(runnableVersionId({ versions: [version('a', 1, false), version('b', 2, false)] })).toBe('b')
    expect(runnableVersionId({ versions: [] })).toBeNull()
  })
})
