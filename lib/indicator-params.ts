// The indicator parameters a person may choose on Market Analysis, and the rules the server holds them
// to (backend `GET /market-data/indicators/{symbol}`). The server is the authority; these let a box say
// what is wrong before a request is made, and say it in the same words.

export type IndicatorKey =
  | 'lookbackDays'
  | 'smaWindow'
  | 'emaSpan'
  | 'rsiPeriod'
  | 'macdFast'
  | 'macdSlow'
  | 'macdSignal'
  | 'bollingerWindow'
  | 'bollingerStd'

export type IndicatorParams = Record<IndicatorKey, number>
/** What is typed in the boxes. */
export type IndicatorDraft = Record<IndicatorKey, string>
export type IndicatorProblems = Partial<Record<IndicatorKey, string>>

export interface IndicatorField {
  key: IndicatorKey
  label: string
  /** The server's query parameter. */
  param: string
  min: number
  max: number
  /** The deviations count may be fractional, but not zero: the lowest value is excluded. */
  excludeMin?: boolean
  integer: boolean
  step: number
  hint: string
}

export const DEFAULT_PARAMS: IndicatorParams = {
  lookbackDays: 250,
  smaWindow: 20,
  emaSpan: 20,
  rsiPeriod: 14,
  macdFast: 12,
  macdSlow: 26,
  macdSignal: 9,
  bollingerWindow: 20,
  bollingerStd: 2,
}

export const INDICATOR_FIELDS: IndicatorField[] = [
  { key: 'lookbackDays', label: 'History (days)', param: 'lookback_days', min: 5, max: 2000, integer: true, step: 1, hint: 'Calendar days back from today' },
  { key: 'smaWindow', label: 'SMA window', param: 'sma_window', min: 2, max: 400, integer: true, step: 1, hint: 'Bars averaged' },
  { key: 'emaSpan', label: 'EMA span', param: 'ema_span', min: 2, max: 400, integer: true, step: 1, hint: 'Bars the average spans' },
  { key: 'rsiPeriod', label: 'RSI period', param: 'rsi_period', min: 2, max: 100, integer: true, step: 1, hint: 'Wilder smoothing period' },
  { key: 'macdFast', label: 'MACD fast', param: 'macd_fast', min: 2, max: 100, integer: true, step: 1, hint: 'Shorter than the slow average' },
  { key: 'macdSlow', label: 'MACD slow', param: 'macd_slow', min: 3, max: 200, integer: true, step: 1, hint: 'Longer than the fast average' },
  { key: 'macdSignal', label: 'MACD signal', param: 'macd_signal_period', min: 2, max: 100, integer: true, step: 1, hint: 'Average of the MACD line' },
  { key: 'bollingerWindow', label: 'Bollinger window', param: 'bollinger_window', min: 2, max: 400, integer: true, step: 1, hint: 'Bars the bands are drawn around' },
  { key: 'bollingerStd', label: 'Bollinger deviations', param: 'bollinger_std', min: 0, max: 5, excludeMin: true, integer: false, step: 0.1, hint: 'Standard deviations either side' },
]

export function draftOf(params: IndicatorParams): IndicatorDraft {
  return Object.fromEntries(INDICATOR_FIELDS.map((f) => [f.key, String(params[f.key])])) as IndicatorDraft
}

function rangeWords(field: IndicatorField): string {
  return field.excludeMin ? `more than ${field.min} and at most ${field.max}` : `between ${field.min} and ${field.max}`
}

/** What is wrong with each box, by key; empty when every box holds a value the server will accept. */
export function indicatorProblems(draft: IndicatorDraft): IndicatorProblems {
  const problems: IndicatorProblems = {}
  const value: Partial<IndicatorParams> = {}
  for (const field of INDICATOR_FIELDS) {
    const text = draft[field.key].trim()
    const number = Number(text)
    if (text === '' || !Number.isFinite(number)) {
      problems[field.key] = `${field.label} must be a number.`
    } else if (field.integer && !Number.isInteger(number)) {
      problems[field.key] = `${field.label} must be a whole number.`
    } else if (number > field.max || number < field.min || (field.excludeMin && number === field.min)) {
      problems[field.key] = `${field.label} must be ${rangeWords(field)}.`
    } else {
      value[field.key] = number
    }
  }
  if (value.macdFast !== undefined && value.macdSlow !== undefined && value.macdFast >= value.macdSlow) {
    problems.macdFast = `The MACD's fast average (${value.macdFast}) must be shorter than its slow one (${value.macdSlow}).`
  }
  return problems
}

/** The parameters the boxes hold, or null while any box has a problem. */
export function readParams(draft: IndicatorDraft): IndicatorParams | null {
  if (Object.keys(indicatorProblems(draft)).length > 0) return null
  return Object.fromEntries(INDICATOR_FIELDS.map((f) => [f.key, Number(draft[f.key])])) as IndicatorParams
}

/** `?lookback_days=250&sma_window=20...`: every parameter, so what was asked for is in the request. */
export function indicatorQuery(params: IndicatorParams): string {
  return INDICATOR_FIELDS.map((f) => `${f.param}=${params[f.key]}`).join('&')
}

export function isDefault(params: IndicatorParams): boolean {
  return INDICATOR_FIELDS.every((f) => params[f.key] === DEFAULT_PARAMS[f.key])
}

export interface WarmUp {
  name: string
  /** Bars of history before the indicator has its first value (the server leaves it empty until then). */
  needs: number
}

/** How many bars each indicator needs before it shows its first number, as the server computes them
 * (a backend test holds these to the real series): the RSI works on changes, so it needs one more bar
 * than its period; the MACD line starts at its slow average (its signal line needs `macdSignal - 1`
 * more). */
export function warmUps(params: IndicatorParams): WarmUp[] {
  return [
    { name: `SMA(${params.smaWindow})`, needs: params.smaWindow },
    { name: `EMA(${params.emaSpan})`, needs: params.emaSpan },
    { name: `RSI(${params.rsiPeriod})`, needs: params.rsiPeriod + 1 },
    { name: `MACD(${params.macdFast}, ${params.macdSlow}, ${params.macdSignal})`, needs: params.macdSlow },
    { name: `Bollinger(${params.bollingerWindow}, ${params.bollingerStd})`, needs: params.bollingerWindow },
  ]
}

/** The indicators that cannot show anything yet because the history is shorter than they need. */
export function unfilled(params: IndicatorParams, bars: number): WarmUp[] {
  return warmUps(params).filter((w) => w.needs > bars)
}

/** The parameters a series was computed with, as the server echoed them back. */
export function paramsOf(series: {
  lookback_days: number
  sma_window: number
  ema_span: number
  rsi_period: number
  macd_fast: number
  macd_slow: number
  macd_signal_period: number
  bollinger_window: number
  bollinger_std: number
}): IndicatorParams {
  return {
    lookbackDays: series.lookback_days,
    smaWindow: series.sma_window,
    emaSpan: series.ema_span,
    rsiPeriod: series.rsi_period,
    macdFast: series.macd_fast,
    macdSlow: series.macd_slow,
    macdSignal: series.macd_signal_period,
    bollingerWindow: series.bollinger_window,
    bollingerStd: series.bollinger_std,
  }
}
