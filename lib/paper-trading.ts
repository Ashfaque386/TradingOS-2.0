// Pure logic behind the paper-trading desk (app/paper-trading): naming a contract, the cost
// waterfall, the at-expiry payoff of an option structure, what needs an operator's attention, and
// the expiry countdown. Nothing here touches React, the network or the clock (`now` is passed
// in), so it is tested directly (e2e/paper-trading-logic.spec.ts).

import type {
  PaperCostBreakdown,
  PaperFill,
  PaperInstrumentKind,
  PaperOptionStrategy,
  PaperPosition,
  PaperSubscription,
} from './api'

// ---- Contracts ----

export type OptionContract = {
  underlying: string
  expiry: string // YYYY-MM-DD
  strike: number
  type: 'CE' | 'PE'
}

/** `NFO:NIFTY|261027|24500|CE` -> its parts; `null` for anything that is not an option's name.
 * The same shared name the backend builds (`brokers.base.option_symbol`). */
export function parseOptionSymbol(symbol: string): OptionContract | null {
  const match = /^NFO:([^|]+)\|(\d{2})(\d{2})(\d{2})\|(\d+(?:\.\d+)?)\|(CE|PE)$/.exec(symbol.trim())
  if (!match) return null
  const [, underlying, yy, mm, dd, strike, type] = match
  const month = Number(mm)
  const day = Number(dd)
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  return {
    underlying,
    expiry: `20${yy}-${mm}-${dd}`,
    strike: Number(strike),
    type: type as 'CE' | 'PE',
  }
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** `2026-10-27` -> `27 Oct` (with the year when it is not the current one). */
export function formatExpiry(expiry: string, now: Date = new Date()): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expiry)
  if (!match) return expiry
  const label = `${Number(match[3])} ${MONTHS[Number(match[2]) - 1]}`
  return Number(match[1]) === istDate(now).year ? label : `${label} ${match[1]}`
}

/** A held contract named for a person: `24500 CE · 27 Oct`; a future `Oct future`; else as is. */
export function describeContract(symbol: string, now: Date = new Date()): string {
  const option = parseOptionSymbol(symbol)
  if (option) return `${option.strike} ${option.type} · ${formatExpiry(option.expiry, now)}`
  const future = /^NFO:.+?(\d{2})([A-Z]{3})FUT$/.exec(symbol)
  if (future) return `${future[2][0]}${future[2].slice(1).toLowerCase()} future`
  return symbol
}

export const KIND_LABEL: Record<PaperInstrumentKind, string> = {
  equity: 'Shares',
  future: 'Futures',
  option: 'Options',
}

export const KIND_SHORT: Record<PaperInstrumentKind, string> = {
  equity: 'EQ',
  future: 'FUT',
  option: 'OPT',
}

export const OPTION_STRATEGY_LABEL: Record<PaperOptionStrategy, string> = {
  long_option: 'Long option',
  vertical_spread: 'Credit spread',
}

/** What a strategy does, in the words an operator would use. */
export function strategySummary(
  strategy: PaperOptionStrategy,
  legs: { type: 'CE' | 'PE'; long: boolean }[] = [],
): string {
  if (strategy === 'long_option') return 'Buys the at-the-money call on a buy signal, the put on a sell signal'
  const sold = legs.find((leg) => !leg.long)
  if (sold) return sold.type === 'PE' ? 'Bull put spread' : 'Bear call spread'
  return 'Sells the at-the-money put (buy signal) or call (sell signal) and buys a hedge further out'
}

// ---- Money ----

const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })
const inr2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** `₹1,24,000` (Indian digit grouping); `signed` adds an explicit `+`/`−`. `null` -> `—`. */
export function formatInr(value: number | null | undefined, options: { signed?: boolean; decimals?: 0 | 2 } = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const formatter = options.decimals === 2 ? inr2 : inr
  const magnitude = formatter.format(Math.abs(value))
  if (value < 0) return `−₹${magnitude}`
  return `${options.signed && value > 0 ? '+' : ''}₹${magnitude}`
}

/** `gain`, `loss` or `flat` (a P&L's colour class). Under half a paisa counts as flat. */
export function tone(value: number | null | undefined): 'gain' | 'loss' | 'flat' {
  if (value === null || value === undefined || Math.abs(value) < 0.005) return 'flat'
  return value > 0 ? 'gain' : 'loss'
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'never'
  if (seconds < 1) return 'just now'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

// ---- The cost waterfall: gross - costs = net ----

export type WaterfallSegment = { kind: 'net' | 'costs' | 'loss'; share: number }

/** The bar that shows what a result is made of, as shares of one whole (they sum to 1; empty when
 * there is nothing to show):
 *   gain, costs within it  -> net (green) then costs (amber): what you kept and what it cost;
 *   gain, costs beyond it  -> costs (amber, all of the gain) then loss (red, the part beyond);
 *   loss                   -> loss (red) then costs (amber): costs deepen a loss.
 * `gross` is the price difference only; `costs` is what the fills cost (never negative). */
export function waterfall(gross: number, costs: number): WaterfallSegment[] {
  const spent = Math.max(0, costs)
  let parts: { kind: WaterfallSegment['kind']; amount: number }[]
  if (gross > 0 && spent <= gross) {
    parts = [{ kind: 'net', amount: gross - spent }, { kind: 'costs', amount: spent }]
  } else if (gross > 0) {
    parts = [{ kind: 'costs', amount: gross }, { kind: 'loss', amount: spent - gross }]
  } else {
    parts = [{ kind: 'loss', amount: -gross }, { kind: 'costs', amount: spent }]
  }
  const total = parts.reduce((sum, part) => sum + part.amount, 0)
  if (total <= 0) return []
  return parts.filter((part) => part.amount > 0).map((part) => ({ kind: part.kind, share: part.amount / total }))
}

export const COST_LINES: { key: keyof PaperCostBreakdown; label: string }[] = [
  { key: 'brokerage', label: 'Brokerage' },
  { key: 'stt', label: 'STT' },
  { key: 'exchange_txn_charges', label: 'Exchange charges' },
  { key: 'sebi_fee', label: 'SEBI fee' },
  { key: 'stamp_duty', label: 'Stamp duty' },
  { key: 'gst', label: 'GST' },
]

/** Each cost line summed over fills, largest first, zero lines left out. */
export function costLines(fills: Pick<PaperFill, 'costs_breakdown'>[]): { label: string; amount: number }[] {
  return COST_LINES.map(({ key, label }) => ({
    label,
    amount: fills.reduce((sum, fill) => sum + Number(fill.costs_breakdown?.[key] ?? 0), 0),
  }))
    .filter((line) => line.amount > 0)
    .sort((a, b) => b.amount - a.amount)
}

// ---- The payoff of held option legs at expiry ----

export type Leg = { symbol: string; quantity: number; avg_cost: number }
export type PayoffPoint = { spot: number; payoff: number }
export type PayoffSummary = {
  curve: PayoffPoint[]
  /** Best result at expiry, or `null` when it grows without limit. */
  maxProfit: number | null
  /** Worst result at expiry (a negative number), or `null` when it is unlimited. */
  maxLoss: number | null
  breakevens: number[]
  /** `null` when the legs are not all options of one underlying. */
  underlying: string | null
}

/** What the legs would be worth at expiry if the underlying finished at `spot`, before costs:
 * the sum over legs of quantity x (intrinsic value - what it cost). */
export function payoffAt(legs: Leg[], spot: number): number {
  let total = 0
  for (const leg of legs) {
    const option = parseOptionSymbol(leg.symbol)
    if (!option || leg.quantity === 0) continue
    const intrinsic = option.type === 'CE' ? Math.max(spot - option.strike, 0) : Math.max(option.strike - spot, 0)
    total += leg.quantity * (intrinsic - leg.avg_cost)
  }
  return total
}

/** The at-expiry payoff of the open option legs: a curve to draw, the best and worst result, and
 * where it crosses zero. The payoff is piecewise linear with kinks only at the strikes, so the
 * extremes and break-evens are exact (found between the kinks), not read off a sampled curve. */
export function payoffSummary(allLegs: Leg[], spotHint: number | null = null): PayoffSummary {
  const legs = allLegs.filter((leg) => leg.quantity !== 0 && parseOptionSymbol(leg.symbol) !== null)
  const parsed = legs.map((leg) => parseOptionSymbol(leg.symbol) as OptionContract)
  const underlyings = new Set(parsed.map((option) => option.underlying))
  if (legs.length === 0 || underlyings.size !== 1) {
    return { curve: [], maxProfit: null, maxLoss: null, breakevens: [], underlying: null }
  }
  const strikes = [...new Set(parsed.map((option) => option.strike))].sort((a, b) => a - b)
  const points = [0, ...strikes] // the payoff is linear between these, and beyond the last
  const values = points.map((spot) => payoffAt(legs, spot))

  // Beyond the highest strike every call is in the money and every put is out: the slope is the
  // net number of calls bought (negative: sold). Unlimited if it is not zero.
  const rightSlope = legs.reduce((sum, leg, index) => sum + (parsed[index].type === 'CE' ? leg.quantity : 0), 0)
  const candidates = [...values]
  const upside = rightSlope > 0 ? Infinity : rightSlope < 0 ? -Infinity : null
  const maxProfit = upside === Infinity ? null : Math.max(...candidates)
  const maxLoss = upside === -Infinity ? null : Math.min(...candidates)

  const breakevens: number[] = []
  for (let i = 0; i + 1 < points.length; i++) {
    const [a, b] = [values[i], values[i + 1]]
    if ((a < 0 && b > 0) || (a > 0 && b < 0)) {
      breakevens.push(points[i] + ((0 - a) / (b - a)) * (points[i + 1] - points[i]))
    } else if (a === 0 && i > 0) {
      breakevens.push(points[i])
    }
  }
  const last = values[values.length - 1]
  if (last === 0 && strikes.length > 0) breakevens.push(points[points.length - 1])
  if (rightSlope !== 0 && ((last < 0 && rightSlope > 0) || (last > 0 && rightSlope < 0))) {
    breakevens.push(points[points.length - 1] + -last / rightSlope)
  }
  const unique = [...new Set(breakevens.map((value) => Math.round(value * 100) / 100))].sort((a, b) => a - b)

  // The curve: from a little below the lowest strike to a little above the highest (or the spot,
  // if it lies outside), through every strike so the kinks are drawn exactly.
  const low = Math.min(strikes[0], spotHint ?? strikes[0])
  const high = Math.max(strikes[strikes.length - 1], spotHint ?? strikes[strikes.length - 1])
  const margin = Math.max((high - low) * 0.25, strikes[0] * 0.01, 1)
  const start = Math.max(0, low - margin)
  const end = high + margin
  const spots = [...new Set([start, ...strikes, ...unique, end].filter((spot) => spot >= start && spot <= end))]
    .sort((a, b) => a - b)
  return {
    curve: spots.map((spot) => ({ spot, payoff: payoffAt(legs, spot) })),
    maxProfit,
    maxLoss,
    breakevens: unique,
    underlying: parsed[0].underlying,
  }
}

// ---- Expiry ----

/** Today's calendar date in India (where contracts expire), whatever the browser's time zone. */
export function istDate(now: Date): { year: number; month: number; day: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const pick = (type: string) => Number(parts.find((part) => part.type === type)?.value)
  return { year: pick('year'), month: pick('month'), day: pick('day') }
}

/** Whole days from today (IST) to `expiry` (`YYYY-MM-DD`): 0 on the expiry day, negative after. */
export function daysToExpiry(expiry: string, now: Date): number {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expiry)
  if (!match) return Number.NaN
  const today = istDate(now)
  const a = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  const b = Date.UTC(today.year, today.month - 1, today.day)
  return Math.round((a - b) / 86_400_000)
}

export type ExpiryPhase = 'ok' | 'near' | 'expired'

/** A countdown for a contract: how long, and whether it is inside its window. Futures are rolled
 * `ROLL_DAYS` before expiry and options squared off `SQUAREOFF_DAYS` before (the backend's
 * defaults, PAPER_FUTURES_ROLL_DAYS and PAPER_OPTIONS_SQUAREOFF_DAYS): shown as information, the
 * backend decides. */
export const ROLL_DAYS = 3
export const SQUAREOFF_DAYS = 1

export function expiryCountdown(
  kind: PaperInstrumentKind,
  expiry: string | null,
  now: Date,
): { days: number; label: string; phase: ExpiryPhase; note: string } | null {
  if (!expiry || kind === 'equity') return null
  const days = daysToExpiry(expiry, now)
  if (Number.isNaN(days)) return null
  const window = kind === 'future' ? ROLL_DAYS : SQUAREOFF_DAYS
  if (days < 0) return { days, label: 'expired', phase: 'expired', note: 'Past expiry' }
  const label = days === 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`
  if (days <= window) {
    return {
      days,
      label,
      phase: 'near',
      note: kind === 'future' ? 'Rolling to the next contract' : 'Squaring off before expiry',
    }
  }
  return { days, label, phase: 'ok', note: kind === 'future' ? `Rolls ${ROLL_DAYS} days before expiry` : `Squares off ${SQUAREOFF_DAYS} day before expiry` }
}

// ---- What needs attention ----

export type AttentionItem = {
  id: string
  severity: 'critical' | 'warning' | 'info'
  title: string
  detail: string
  subscriptionId: string | null
  /** What the operator can do about it from here. */
  action: 'close' | 'activate' | 'deactivate' | null
}

const STALE_PRICE_SECONDS = 120
const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 } as const

export function subscriptionName(sub: Pick<PaperSubscription, 'symbol' | 'instrument_kind' | 'option_strategy'>): string {
  if (sub.instrument_kind === 'option' && sub.option_strategy) {
    return `${sub.symbol} ${OPTION_STRATEGY_LABEL[sub.option_strategy].toLowerCase()}`
  }
  return sub.instrument_kind === 'future' ? `${sub.symbol} future` : sub.symbol
}

/** Everything an operator should look at, most urgent first. `marketOpen` decides whether a price
 * that has stopped moving is a problem (a shut market is not). */
export function attentionItems(subs: PaperSubscription[], marketOpen: boolean): AttentionItem[] {
  const items: AttentionItem[] = []
  for (const sub of subs) {
    const name = subscriptionName(sub)
    if (!sub.managed) {
      items.push({
        id: `${sub.id}:unmanaged`,
        severity: 'critical',
        title: `${name} is holding a position nobody is watching`,
        detail: 'The subscription is switched off, so its stop-loss no longer runs. Close the position, or switch the subscription back on.',
        subscriptionId: sub.id,
        action: 'close',
      })
    }
    if (sub.is_active && sub.state === 'halted_no_data') {
      items.push({
        id: `${sub.id}:halted`,
        severity: 'warning',
        title: `${name} is not trading`,
        detail: sub.state_reason ?? 'There is no real price feed for it.',
        subscriptionId: sub.id,
        action: null,
      })
    }
    if (sub.is_active && sub.state === 'awaiting_history') {
      items.push({
        id: `${sub.id}:history`,
        severity: 'info',
        title: `${name} is waiting for price history`,
        detail: sub.state_reason ?? 'It needs more real price history before it can signal.',
        subscriptionId: sub.id,
        action: null,
      })
    }
    if (sub.is_active && sub.state === 'active' && sub.open_legs > 0 && sub.unrealized_pnl === null) {
      items.push({
        id: `${sub.id}:unpriced`,
        severity: 'warning',
        title: `${name} has a position with no price`,
        detail: 'At least one open leg has never been priced, so its value and stop-loss are unknown.',
        subscriptionId: sub.id,
        action: null,
      })
    }
    if (
      marketOpen &&
      sub.is_active &&
      sub.state === 'active' &&
      sub.last_tick !== null &&
      sub.last_tick.age_seconds > STALE_PRICE_SECONDS
    ) {
      items.push({
        id: `${sub.id}:stale`,
        severity: 'warning',
        title: `${name} has a stale price`,
        detail: `The last price arrived ${formatAge(sub.last_tick.age_seconds)} while the market is open.`,
        subscriptionId: sub.id,
        action: null,
      })
    }
  }
  return items.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || a.title.localeCompare(b.title))
}

// ---- Grouping ----

export type OrderGroup = {
  orderGroupId: string
  fills: PaperFill[]
  createdAt: string
  costsTotal: number
  realizedPnl: number
}

/** Fills grouped into the orders they belong to (a spread's legs share one), newest order first,
 * legs in leg order. */
export function groupFills(fills: PaperFill[]): OrderGroup[] {
  const groups = new Map<string, PaperFill[]>()
  for (const fill of fills) {
    const group = groups.get(fill.order_group_id)
    if (group) group.push(fill)
    else groups.set(fill.order_group_id, [fill])
  }
  return [...groups.entries()]
    .map(([orderGroupId, members]) => {
      const sorted = [...members].sort((a, b) => a.leg_index - b.leg_index)
      return {
        orderGroupId,
        fills: sorted,
        createdAt: sorted.reduce((first, fill) => (fill.created_at < first ? fill.created_at : first), sorted[0].created_at),
        costsTotal: sorted.reduce((sum, fill) => sum + fill.costs_total, 0),
        realizedPnl: sorted.reduce((sum, fill) => sum + fill.realized_pnl, 0),
      }
    })
    .sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0))
}

/** The open legs of a subscription, sold legs first (the order they are closed in). */
export function openLegs(positions: PaperPosition[]): PaperPosition[] {
  return positions.filter((position) => position.quantity !== 0).sort((a, b) => a.quantity - b.quantity)
}

/** The soonest expiry among the open option legs (`YYYY-MM-DD`), or `null`. An option
 * subscription has no contract of its own: its expiry is whatever its legs hold. */
export function earliestLegExpiry(positions: PaperPosition[]): string | null {
  const expiries = openLegs(positions)
    .map((position) => parseOptionSymbol(position.symbol)?.expiry)
    .filter((expiry): expiry is string => Boolean(expiry))
    .sort()
  return expiries[0] ?? null
}

/** The sum of the legs' unrealized P&L, or `null` if any open leg has never been priced. */
export function legsUnrealized(positions: PaperPosition[]): number | null {
  const open = openLegs(positions)
  if (open.some((position) => position.unrealized_pnl === null)) return null
  return open.reduce((sum, position) => sum + (position.unrealized_pnl ?? 0), 0)
}
