// Pure logic behind the paper-trading desk (app/(console)/paper-trading): naming a contract, the cost
// waterfall, the at-expiry payoff of an option structure, what needs an operator's attention, and
// the expiry countdown. Nothing here touches React, the network or the clock (`now` is passed
// in), so it is tested directly (e2e/paper-trading-logic.spec.ts).

import type {
  PaperCostBreakdown,
  PaperFill,
  PaperHistoryBackfill,
  PaperInstrumentKind,
  PaperLastTick,
  PaperOptionStrategy,
  PaperPosition,
  PaperSignal,
  PaperSubscription,
  TodaysPaperPnl,
  UnrealizedPaperPnl,
} from './api'
import { indiaStamp } from './time'

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

/** The backend's PAPER_STALE_PRICE_SECONDS default; the shared fixture pins the two together. */
export const STALE_PRICE_SECONDS = 120
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
    if (sub.is_active && sub.state === 'active' && sub.quote_refusal) {
      items.push({
        id: `${sub.id}:refused`,
        severity: 'warning',
        title: `${name} has no usable price`,
        detail: `Its latest ${sub.quote_refusal.broker} quote was refused: ${sub.quote_refusal.reason}. Nothing trades on it until a current quote arrives.`,
        subscriptionId: sub.id,
        action: null,
      })
    } else if (
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

// ---- How fresh a price is by the exchange's own clock, and the band it may trade in ----

const IST_CLOCK = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

/** `2026-11-02T04:01:02Z` -> `09:31:02 IST`: the exchange's clock, whatever the browser's zone. */
export function formatIstClock(iso: string): string {
  return `${IST_CLOCK.format(new Date(iso))} IST`
}

/** A price whose exchange stamp is this fresh (seconds) is "live"; an older one is a quiet instrument
 * or a feed that has stopped, and the desk says it cannot tell which. */
export const EXCHANGE_FRESH_SECONDS = 60

export type ExchangeFreshness = { tone: 'fresh' | 'quiet' | 'unknown'; text: string }

/** What the exchange's stamp on the latest price says about it. A stamp is the time of the last
 * *trade*, so an old one is not by itself a fault; no stamp at all is "not known", never "fresh". */
export function exchangeFreshness(
  tick: Pick<PaperLastTick, 'exchange_time' | 'exchange_age_seconds'> | null,
): ExchangeFreshness {
  if (tick === null) return { tone: 'unknown', text: 'No price has arrived yet.' }
  if (!tick.exchange_time) {
    return {
      tone: 'unknown',
      text: 'This broker sends no exchange timestamp we can read, so the price cannot be dated by the exchange.',
    }
  }
  const age = tick.exchange_age_seconds ?? 0
  const traded = `Last traded at ${formatIstClock(tick.exchange_time)}, ${formatAge(age)}.`
  return age <= EXCHANGE_FRESH_SECONDS
    ? { tone: 'fresh', text: traded }
    : { tone: 'quiet', text: `${traded} A quiet instrument, or a feed that has stopped.` }
}

export type CircuitLine = { source: 'exchange' | 'reference_table' | 'none'; text: string }

/** Which band the risk gate will judge an order against: the exchange's own limits from the quote,
 * or, failing those, the reference table's percentage of the previous close (said to be exactly that). */
export function circuitLine(
  tick: Pick<PaperLastTick, 'lower_circuit' | 'upper_circuit'> | null,
  fallbackPct: number | null | undefined,
): CircuitLine {
  if (tick && tick.lower_circuit != null && tick.upper_circuit != null) {
    return {
      source: 'exchange',
      text: `Exchange circuit limits ${formatInr(tick.lower_circuit, { decimals: 2 })} to ${formatInr(tick.upper_circuit, { decimals: 2 })}.`,
    }
  }
  if (fallbackPct != null) {
    return {
      source: 'reference_table',
      text: `The quote carried no exchange circuit limits, so the risk gate uses the reference table's ${fallbackPct}% band from the previous close (not an exchange figure).`,
    }
  }
  return { source: 'none', text: 'No circuit band is known for this price.' }
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

// ---- Closing: the confirmation an operator types ----

/** What must be typed to confirm a close: the subscription's symbol, and for an administrative
 * close (no risk gate) a longer phrase, so it cannot be done on autopilot. */
export function closeConfirmationPhrase(symbol: string, administrative: boolean): string {
  const name = symbol.trim().toUpperCase()
  return administrative ? `ADMIN ${name}` : name
}

export function confirmationMatches(typed: string, phrase: string): boolean {
  return phrase.length > 0 && typed.trim().toUpperCase() === phrase.toUpperCase()
}

// ---- The underlying's price, for the payoff chart ----

export const SPOT_STALE_SECONDS = 300

/** An option subscription's own tick is its underlying's (its legs are quoted under their own
 * names), so the list's `last_tick` is where the underlying is now. `stale` once it is older than
 * `SPOT_STALE_SECONDS`: shown, but as an old price, not a live one. */
export function underlyingSpot(
  sub: Pick<PaperSubscription, 'instrument_kind' | 'last_tick'>,
): { price: number; ageSeconds: number; stale: boolean } | null {
  if (sub.instrument_kind !== 'option' || sub.last_tick === null) return null
  const { price, age_seconds: ageSeconds } = sub.last_tick
  return { price, ageSeconds, stale: ageSeconds > SPOT_STALE_SECONDS }
}

// ---- Editing a subscription's settings ----

export type SettingField =
  | 'stop_loss_pct'
  | 'position_size_pct'
  | 'lots'
  | 'spread_width'
  | 'sma_window'
  | 'builtin_strategy'
  | 'initial_capital'

export const SETTING_LABEL: Record<SettingField, string> = {
  stop_loss_pct: 'Stop-loss',
  position_size_pct: 'Size per entry',
  lots: 'Lots',
  spread_width: 'Hedge distance',
  sma_window: 'Average length',
  builtin_strategy: 'Signal',
  initial_capital: 'Capital',
}

type SettingsSource = Pick<
  PaperSubscription,
  | 'instrument_kind'
  | 'option_strategy'
  | 'stop_loss_pct'
  | 'position_size_pct'
  | 'lots'
  | 'spread_width'
  | 'sma_window'
  | 'builtin_strategy'
  | 'initial_capital'
>

export type SettingsForm = Record<Exclude<SettingField, 'builtin_strategy'>, string> & {
  builtin_strategy: 'always_long' | 'sma_crossover'
}

/** A subscription that has never been in the market: nothing open, no result, no costs. Capital
 * may change until then; afterwards it is the basis its returns are measured on. (A hint: the
 * backend decides from the fills themselves.) */
export function hasTraded(
  sub: Pick<PaperSubscription, 'open_legs' | 'realized_pnl' | 'costs_total' | 'quantity'>,
): boolean {
  return sub.open_legs > 0 || sub.quantity !== 0 || sub.realized_pnl !== 0 || sub.costs_total !== 0
}

/** The settings that apply to this kind of subscription, in the order they are shown. */
export function editableSettings(sub: SettingsSource): SettingField[] {
  const fields: SettingField[] = ['stop_loss_pct']
  if (sub.instrument_kind === 'equity') fields.push('position_size_pct')
  else fields.push('lots')
  if (sub.option_strategy === 'vertical_spread') fields.push('spread_width')
  fields.push('builtin_strategy', 'sma_window', 'initial_capital')
  return fields
}

export function settingsForm(sub: SettingsSource): SettingsForm {
  return {
    stop_loss_pct: String(sub.stop_loss_pct),
    position_size_pct: String(sub.position_size_pct),
    lots: String(sub.lots),
    spread_width: String(sub.spread_width),
    sma_window: String(sub.sma_window),
    builtin_strategy: sub.builtin_strategy === 'always_long' ? 'always_long' : 'sma_crossover',
    initial_capital: String(sub.initial_capital),
  }
}

export function formatSettingValue(field: SettingField, value: number | string): string {
  switch (field) {
    case 'stop_loss_pct':
    case 'position_size_pct':
      return `${value}%`
    case 'spread_width':
      return `${value} ${Number(value) === 1 ? 'strike' : 'strikes'}`
    case 'sma_window':
      return `${value} points`
    case 'builtin_strategy':
      return value === 'always_long' ? 'Always long' : 'Moving average'
    case 'initial_capital':
      return formatInr(Number(value))
    default:
      return String(value)
  }
}

/** When a change takes effect, in the words an operator needs before saving it. */
export function settingEffect(field: SettingField, sub: Pick<PaperSubscription, 'instrument_kind' | 'open_legs'>): string {
  const holding = sub.open_legs > 0
  switch (field) {
    case 'stop_loss_pct':
      return holding ? 'Applies to the open position from the next price tick.' : 'Applies from its next entry.'
    case 'position_size_pct':
      return 'Applies to the next entry. An open position is not resized.'
    case 'lots':
      return sub.instrument_kind === 'option'
        ? 'Applies to the next entry; a structure already planned is dropped and chosen again.'
        : 'Applies to the next entry. An open position is not resized.'
    case 'spread_width':
      return 'Applies to the next entry; a structure already planned is dropped and chosen again.'
    case 'sma_window':
    case 'builtin_strategy':
      return holding
        ? 'Takes effect with the next daily signal (08:00 IST). Today\'s signal stands. If the new one points the other way, the position is closed on the tick after it.'
        : 'Takes effect with the next daily signal (08:00 IST). Today\'s signal stands.'
    case 'initial_capital':
      return 'Sizes every later entry and its margin check.'
    default:
      return ''
  }
}

export type PlannedChange = {
  field: SettingField
  label: string
  before: string
  after: string
  effect: string
  /** What is sent: a number, or the signal's name. */
  value: number | string
}

const INTEGER_FIELDS: SettingField[] = ['lots', 'spread_width', 'sma_window']

/** The changes a form would make, and what is wrong with it. Mirrors the backend's own limits
 * (which stay the authority): stop-loss and capital above zero, size above zero up to 100, lots 1
 * to 1000, hedge distance 1 to 20, average length a whole number above zero. */
export function planSettingsChange(
  sub: SettingsSource & Pick<PaperSubscription, 'open_legs'>,
  form: SettingsForm,
): { changes: PlannedChange[]; errors: Partial<Record<SettingField, string>> } {
  const changes: PlannedChange[] = []
  const errors: Partial<Record<SettingField, string>> = {}
  const current = settingsForm(sub)
  for (const field of editableSettings(sub)) {
    if (form[field] === current[field]) continue
    let value: number | string
    if (field === 'builtin_strategy') {
      value = form.builtin_strategy
    } else {
      const text = form[field].trim()
      const parsed = Number(text)
      if (text === '' || Number.isNaN(parsed)) {
        errors[field] = 'Enter a number.'
        continue
      }
      if (INTEGER_FIELDS.includes(field) && !Number.isInteger(parsed)) {
        errors[field] = 'Use a whole number.'
        continue
      }
      const limits: Partial<Record<SettingField, [number, number]>> = {
        position_size_pct: [Number.MIN_VALUE, 100],
        lots: [1, 1000],
        spread_width: [1, 20],
        sma_window: [1, Number.MAX_SAFE_INTEGER],
      }
      const [low, high] = limits[field] ?? [Number.MIN_VALUE, Number.MAX_SAFE_INTEGER]
      if (parsed < low || parsed > high) {
        errors[field] =
          field === 'position_size_pct'
            ? 'Use more than 0 and at most 100.'
            : field === 'lots'
              ? 'Use 1 to 1000.'
              : field === 'spread_width'
                ? 'Use 1 to 20.'
                : 'Use more than 0.'
        continue
      }
      value = parsed
    }
    // A number typed as `5.0` is the same setting as `5`: compare what is sent, not the text.
    const before = field === 'builtin_strategy' ? current[field] : Number(current[field])
    if (value === before) continue
    changes.push({
      field,
      label: SETTING_LABEL[field],
      before: formatSettingValue(field, before),
      after: formatSettingValue(field, value),
      effect: settingEffect(field, sub),
      value,
    })
  }
  return { changes, errors }
}

// ---- Fetching a subscription's price history ----

/** Whether an attention item is a subscription waiting for price history: the one the operator can
 * answer by fetching it. (The item itself is shared with the server's alerts, so the kind is read from its id.) */
export const isHistoryWait = (item: Pick<AttentionItem, 'id'>): boolean => item.id.endsWith(':history')

/** What fetching the history did, in a sentence: how much, from which broker, over which dates, and
 * what it replaced; or why it failed. */
export function describeBackfill(result: PaperHistoryBackfill): string {
  if (result.status === 'failed') {
    return `Could not fetch the price history of ${result.symbol}: ${result.error?.trim() || 'the broker did not say why'}.`
  }
  if (result.rows === 0) return `The broker returned no price history for ${result.symbol}.`
  const range = result.first_date && result.last_date ? ` (${result.first_date} to ${result.last_date})` : ''
  const from = result.source ? ` from ${result.source}` : ''
  const replaced =
    result.unreal_rows_removed > 0
      ? ` ${result.unreal_rows_removed.toLocaleString('en-IN')} generated ${result.unreal_rows_removed === 1 ? 'row was' : 'rows were'} replaced.`
      : ''
  return `Fetched ${result.rows.toLocaleString('en-IN')} daily ${result.rows === 1 ? 'price' : 'prices'} for ${result.symbol}${from}${range}.${replaced}`
}

// ---- Daily signals ----

/** Signals newest first: the latest decision is the one the strategy is waiting to act on. */
export function signalRows(signals: PaperSignal[]): PaperSignal[] {
  return [...signals].sort((a, b) => (a.generated_at < b.generated_at ? 1 : a.generated_at > b.generated_at ? -1 : 0))
}

/** Whether a position was entered or exited on a signal, and when. */
export function signalStatus(signal: Pick<PaperSignal, 'consumed' | 'consumed_at'>): string {
  if (!signal.consumed) return 'Not acted on yet'
  return signal.consumed_at ? `Acted on ${indiaStamp(signal.consumed_at)}` : 'Acted on'
}

// ---- The fills as a file ----

/** One cell of a CSV: quoted when it holds a comma, a quote or a line break, and a text that a
 * spreadsheet would read as a formula is made plain text. */
export function csvCell(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined) return ''
  if (typeof value !== 'string') return String(value)
  const text = /^[=+@\t\r]/.test(value) ? `'${value}` : value
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

const COST_COLUMNS = ['brokerage', 'stt', 'exchange_txn_charges', 'sebi_fee', 'stamp_duty', 'gst'] as const

/** Every fill, oldest first, one row each: what was bought or sold and at what price, what it rested
 * on, the price result before costs, and each line of what it cost. */
export function fillsCsv(fills: PaperFill[]): string {
  const header = [
    'created_at_utc',
    'time_india',
    'symbol',
    'side',
    'requested_quantity',
    'filled_quantity',
    'avg_fill_price',
    'fully_filled',
    'data_source',
    'gross_realized_pnl',
    'costs_total',
    ...COST_COLUMNS,
    'manual_close',
    'order_group_id',
    'leg_index',
  ]
  const rows = [...fills]
    .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.leg_index - b.leg_index))
    .map((fill) => [
      fill.created_at,
      indiaStamp(fill.created_at),
      fill.symbol,
      fill.side,
      fill.requested_quantity,
      fill.filled_quantity,
      fill.avg_fill_price,
      fill.fully_filled,
      fill.data_source,
      fill.realized_pnl,
      fill.costs_total,
      ...COST_COLUMNS.map((column) => fill.costs_breakdown?.[column] ?? 0),
      fill.administrative,
      fill.order_group_id,
      fill.leg_index,
    ])
  return [header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n') + '\r\n'
}

/** The file's name: what it holds and when it was made, in India's date. */
export function fillsFilename(symbol: string, now: Date): string {
  const day = now.toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
  return `paper-fills-${symbol.replace(/[^A-Za-z0-9_-]+/g, '_')}-${day}.csv`
}

export const ORDERS_PER_STEP = 8

/** How many orders to show of `total`, having shown `shown`: the list opens on the newest few and grows by a step. */
export function ordersWindow(total: number, shown: number) {
  const visible = Math.min(total, Math.max(ORDERS_PER_STEP, shown))
  return { visible, remaining: total - visible, next: Math.min(total, visible + ORDERS_PER_STEP) }
}

// ---- Reading the desk when part of it cannot be read ----

export type DeskReading = {
  subs: PaperSubscription[]
  today: TodaysPaperPnl | null
  unrealized: UnrealizedPaperPnl | null
  /** What could not be read this time, in words; the figures concerned are the last ones read. */
  problems: string[]
}

/**
 * The desk is three reads. The subscriptions are the page itself, so if they cannot be read the whole
 * reading fails (and the page keeps the last one). Today's result and the open P&L are figures: if one
 * fails the other parts are still shown, the failed figure keeps its last value, and the problem is
 * said instead of the page going blank.
 */
export function settleDesk(
  previous: DeskReading | null,
  results: [PromiseSettledResult<PaperSubscription[]>, PromiseSettledResult<TodaysPaperPnl>, PromiseSettledResult<UnrealizedPaperPnl>],
  describe: (reason: unknown) => string,
): DeskReading {
  const [subs, today, unrealized] = results
  if (subs.status === 'rejected') throw subs.reason
  const problems: string[] = []
  if (today.status === 'rejected') problems.push(`Today’s result could not be read (${describe(today.reason)}); the figure shown is the last one read.`)
  if (unrealized.status === 'rejected') problems.push(`Open profit and loss could not be read (${describe(unrealized.reason)}); the figure shown is the last one read.`)
  return {
    subs: subs.value,
    today: today.status === 'fulfilled' ? today.value : (previous?.today ?? null),
    unrealized: unrealized.status === 'fulfilled' ? unrealized.value : (previous?.unrealized ?? null),
    problems,
  }
}
