/**
 * What the live subscription's limits screen decides, kept free of React so a logic spec can drive it.
 * It mirrors the server's rules (`src.orchestration.live_subscription_limits`) so a person is told
 * before anything is sent, and the server still enforces them: a limit may only get stricter here,
 * a position that is held protects itself, and nothing here switches a subscription back on.
 */
import type { LiveTradingSubscriptionDto, PositionByStrategy, UpdateLiveSubscriptionInput } from './api'

export type LimitField = 'max_intents_per_window' | 'rate_limit_window_minutes' | 'max_notional_per_intent' | 'intent_expiry_seconds'

export const LIMIT_FIELDS: LimitField[] = ['max_intents_per_window', 'rate_limit_window_minutes', 'max_notional_per_intent', 'intent_expiry_seconds']

export const LIMITS: Record<LimitField, { label: string; unit: string; direction: 'lower' | 'raise'; hint: string; whole: boolean; max?: number }> = {
  max_intents_per_window: { label: 'Orders per window', unit: 'orders', direction: 'lower', hint: 'The most orders the strategy may send in one window.', whole: true },
  rate_limit_window_minutes: { label: 'Window (minutes)', unit: 'minutes', direction: 'raise', hint: 'How far back the orders are counted. A longer window counts more of them.', whole: true },
  max_notional_per_intent: { label: 'Largest order (₹)', unit: '₹', direction: 'lower', hint: 'The most one order may be worth.', whole: false },
  intent_expiry_seconds: { label: 'Seconds an order lives', unit: 'seconds', direction: 'lower', hint: 'An order not sent by then is dropped. At most 300.', whole: true, max: 300 },
}

export type LimitsForm = Record<LimitField, string>

export const limitsForm = (sub: LiveTradingSubscriptionDto): LimitsForm =>
  Object.fromEntries(LIMIT_FIELDS.map((field) => [field, String(sub[field])])) as LimitsForm

/** What a live strategy holds on its symbol, if anything: shares (negative when short) and what they cost. */
export type Held = { quantity: number; cost: number } | null

export function heldBy(positions: PositionByStrategy[] | null | undefined, sub: Pick<LiveTradingSubscriptionDto, 'strategy_id' | 'symbol'>): Held {
  const position = positions?.find((p) => p.mode === 'live' && p.strategy_id === sub.strategy_id && p.symbol === sub.symbol && p.quantity !== 0)
  return position ? { quantity: position.quantity, cost: Math.abs(position.quantity) * position.avg_cost } : null
}

const rupees = (value: number): string => `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

const isNumber = (text: string): boolean => text.trim() !== '' && Number.isFinite(Number(text))

/** The limits whose value on screen differs from the subscription's. */
export function changedLimits(sub: LiveTradingSubscriptionDto, form: LimitsForm): LimitField[] {
  return LIMIT_FIELDS.filter((field) => isNumber(form[field]) && Number(form[field]) !== sub[field])
}

/** What is wrong with the form, by field: a value that is not a number it may be, one that loosens a limit,
 * an order cap below what is held. A limit left as it is has no problem. */
export function limitProblems(sub: LiveTradingSubscriptionDto, form: LimitsForm, held: Held): Partial<Record<LimitField, string>> {
  const problems: Partial<Record<LimitField, string>> = {}
  for (const field of LIMIT_FIELDS) {
    const rule = LIMITS[field]
    const text = form[field]
    if (!isNumber(text)) {
      problems[field] = 'Give a number.'
      continue
    }
    const value = Number(text)
    if (!(value > 0)) problems[field] = 'It must be more than zero.'
    else if (rule.whole && !Number.isInteger(value)) problems[field] = 'It is a whole number.'
    else if (rule.max !== undefined && value > rule.max) problems[field] = `At most ${rule.max}.`
    else if (value === sub[field]) continue
    else if (rule.direction === 'lower' && value > sub[field]) problems[field] = `It can only be lowered here (it is ${sub[field]}). To raise it, enrol the strategy again.`
    else if (rule.direction === 'raise' && value < sub[field]) problems[field] = `It can only be raised here (it is ${sub[field]}). To shorten it, enrol the strategy again.`
    else if (field === 'max_notional_per_intent' && held && value < held.cost)
      problems[field] = `The strategy holds ${Math.abs(held.quantity)} shares (about ${rupees(held.cost)} at cost): a cap below that would block the order that closes them.`
  }
  return problems
}

/** The request for the changes on screen: only the limits that differ, and the reason. */
export function limitsRequest(sub: LiveTradingSubscriptionDto, form: LimitsForm, reason: string): UpdateLiveSubscriptionInput {
  const request: UpdateLiveSubscriptionInput = { reason: reason.trim() }
  for (const field of changedLimits(sub, form)) request[field] = Number(form[field])
  return request
}

/** `Orders per window 5 → 2`, for the summary under the form and for the notice after saving. */
export function describeLimitChange(field: LimitField, before: number, after: number): string {
  const format = (n: number) => (field === 'max_notional_per_intent' ? rupees(n) : String(n))
  return `${LIMITS[field].label} ${format(before)} → ${format(after)}`
}

/** The subscription's limits, one line each, as the panel shows them. */
export function limitLines(sub: LiveTradingSubscriptionDto): string[] {
  return [
    `${sub.max_intents_per_window} orders every ${sub.rate_limit_window_minutes} minutes`,
    `${rupees(sub.max_notional_per_intent)} at most in one order`,
    `an order lives ${sub.intent_expiry_seconds} seconds`,
  ]
}

/** Why a subscription cannot be switched off right now, or null when it can. */
export function switchOffBlocked(sub: Pick<LiveTradingSubscriptionDto, 'is_active' | 'symbol'>, held: Held): string | null {
  if (!sub.is_active) return 'It is already switched off.'
  if (held) return `It holds ${Math.abs(held.quantity)} shares of ${sub.symbol}. A switched-off subscription sends no exit or stop-loss order, so they would be left unwatched. Turn autonomous trading off instead, and switch the subscription off once the position is closed.`
  return null
}

export type LiveSubscriptionState = 'autonomous' | 'autonomy-off' | 'switched-off'

export type LiveSubscriptionRow = {
  id: string
  strategyId: string
  strategyName: string
  symbol: string
  broker: string
  state: LiveSubscriptionState
  stateLabel: string
  limits: string
  autonomyNote: string | null
}

const STATE_ORDER: Record<LiveSubscriptionState, number> = { autonomous: 0, 'autonomy-off': 1, 'switched-off': 2 }

/** Every live subscription as a row for the board: whether real money is being risked on its own, its
 * limits in a line, and who turned autonomy on. Those trading on their own come first, switched-off last. */
export function liveSubscriptionRows(subs: LiveTradingSubscriptionDto[], strategies: { id: string; name: string }[]): LiveSubscriptionRow[] {
  const names = new Map(strategies.map((s) => [s.id, s.name]))
  return subs
    .map((sub): LiveSubscriptionRow => {
      const state: LiveSubscriptionState = !sub.is_active ? 'switched-off' : sub.autonomous_trading_enabled ? 'autonomous' : 'autonomy-off'
      return {
        id: sub.id,
        strategyId: sub.strategy_id,
        strategyName: names.get(sub.strategy_id) ?? sub.strategy_id.slice(0, 8),
        symbol: sub.symbol,
        broker: sub.broker_name,
        state,
        stateLabel: state === 'autonomous' ? 'Trading on its own' : state === 'autonomy-off' ? 'Autonomy off' : 'Switched off',
        limits: `${sub.max_intents_per_window} orders / ${sub.rate_limit_window_minutes} min · up to ${rupees(sub.max_notional_per_intent)} an order`,
        autonomyNote: state === 'autonomous' && sub.autonomy_enabled_by ? `turned on by ${sub.autonomy_enabled_by}` : null,
      }
    })
    .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.strategyName.localeCompare(b.strategyName) || a.symbol.localeCompare(b.symbol))
}
