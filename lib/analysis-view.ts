// What Market Analysis is showing, kept in its address (`/analysis?tab=technical&symbol=RELIANCE`) so a
// reload, a bookmark or a link lands on the same view. Anything in the address that is not a view the
// page knows is ignored, not obeyed.

export const ANALYSIS_TABS = ['pulse', 'technical', 'freshness', 'datalake', 'holidays', 'providers', 'chain'] as const
export type AnalysisTab = (typeof ANALYSIS_TABS)[number]

export interface AnalysisView {
  tab: AnalysisTab
  /** The share the Technical Indicators and Data Freshness tabs show; null until one is chosen. */
  symbol: string | null
  /** The year the Holiday Calendar shows. */
  year: number
}

// A listed share's symbol: letters, digits and the few marks one can hold (M&M, BAJAJ-AUTO).
const SYMBOL = /^[A-Za-z0-9&_.-]{1,32}$/

export function viewFromQuery(search: string, thisYear: number): AnalysisView {
  const params = new URLSearchParams(search)
  const tab = params.get('tab')
  const symbol = params.get('symbol')
  const year = Number(params.get('year'))
  return {
    tab: (ANALYSIS_TABS as readonly string[]).includes(tab ?? '') ? (tab as AnalysisTab) : 'pulse',
    symbol: symbol !== null && SYMBOL.test(symbol) ? symbol.toUpperCase() : null,
    year: Number.isInteger(year) && year >= 2000 && year <= 2100 ? year : thisYear,
  }
}

/** `?tab=holidays&year=2027`, or an empty string for the view the page opens on. */
export function viewToQuery(view: AnalysisView, thisYear: number): string {
  const params = new URLSearchParams()
  if (view.tab !== 'pulse') params.set('tab', view.tab)
  if (view.symbol && (view.tab === 'technical' || view.tab === 'freshness')) params.set('symbol', view.symbol)
  if (view.tab === 'holidays' && view.year !== thisYear) params.set('year', String(view.year))
  const text = params.toString()
  return text ? `?${text}` : ''
}

/** Whether `text` can be opened as a share symbol, whether or not the instrument list holds it (the
 * lake can hold bars for one the list does not). */
export function isSymbol(text: string): boolean {
  return SYMBOL.test(text.trim())
}
