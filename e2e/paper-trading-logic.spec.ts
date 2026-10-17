import { expect, test } from '@playwright/test'
import {
  attentionItems,
  costLines,
  daysToExpiry,
  earliestLegExpiry,
  describeContract,
  expiryCountdown,
  formatInr,
  groupFills,
  legsUnrealized,
  parseOptionSymbol,
  payoffAt,
  payoffSummary,
  tone,
  waterfall,
} from '../lib/paper-trading'
import type { PaperFill, PaperPosition, PaperSubscription } from '../lib/api'

// The desk's pure logic -- no browser, no backend. Each case states the answer worked out by hand.

const PUT_HIGH = 'NFO:NIFTY|261027|24500|PE'
const PUT_LOW = 'NFO:NIFTY|261027|24400|PE'
const CALL = 'NFO:NIFTY|261027|24500|CE'

function sub(overrides: Partial<PaperSubscription> = {}): PaperSubscription {
  return {
    id: 's1', strategy_version_id: 'v1', symbol: 'NIFTY', builtin_strategy: 'sma_crossover', sma_window: 15,
    initial_capital: 100000, stop_loss_pct: 3, position_size_pct: 5, is_active: true, instrument_kind: 'equity',
    contract_symbol: null, contract_expiry: null, lot_size: null, lots: 1, option_strategy: null, spread_width: 2,
    open_legs: 0, state: 'active', state_reason: null, created_at: '2026-10-01T00:00:00Z', created_by: null,
    deactivated_at: null, deactivated_by: null, deactivation_reason: null, quantity: 0, avg_cost: 0,
    realized_pnl: 0, costs_total: 0, net_realized_pnl: 0, managed: true, last_tick: null, unrealized_pnl: null,
    ...overrides,
  }
}

function fill(overrides: Partial<PaperFill>): PaperFill {
  return {
    id: 'f', subscription_id: 's1', symbol: PUT_HIGH, side: 'sell', order_group_id: 'g1', leg_index: 0,
    requested_quantity: 50, filled_quantity: 50, avg_fill_price: 150, fully_filled: true, realized_pnl: 0,
    costs_total: 0, costs_breakdown: null, data_source: 'real', created_at: '2026-10-16T04:00:00Z', ...overrides,
  }
}

test.describe('contract names', () => {
  test('an option name is read into its parts and anything else is not', () => {
    expect(parseOptionSymbol(CALL)).toEqual({ underlying: 'NIFTY', expiry: '2026-10-27', strike: 24500, type: 'CE' })
    expect(parseOptionSymbol('NFO:NIFTY26OCTFUT')).toBeNull()
    expect(parseOptionSymbol('RELIANCE')).toBeNull()
    expect(parseOptionSymbol('NFO:NIFTY|261327|24500|CE')).toBeNull() // month 13
  })

  test('contracts are named for a person', () => {
    const now = new Date('2026-10-16T05:00:00Z')
    expect(describeContract(CALL, now)).toBe('24500 CE · 27 Oct')
    expect(describeContract('NFO:NIFTY26OCTFUT', now)).toBe('Oct future')
    expect(describeContract('RELIANCE', now)).toBe('RELIANCE')
  })
})

test.describe('money', () => {
  test('uses Indian digit grouping and a real minus sign', () => {
    expect(formatInr(1240000)).toBe('₹12,40,000')
    expect(formatInr(-2500.5, { decimals: 2 })).toBe('−₹2,500.50')
    expect(formatInr(125, { signed: true })).toBe('+₹125')
    expect(formatInr(null)).toBe('—')
  })

  test('a result under half a paisa is flat', () => {
    expect(tone(0.004)).toBe('flat')
    expect(tone(-1)).toBe('loss')
    expect(tone(null)).toBe('flat')
  })
})

test.describe('cost waterfall', () => {
  const shares = (gross: number, costs: number) => waterfall(gross, costs).map((s) => [s.kind, Math.round(s.share * 100) / 100])

  test('a gain that covers its costs splits into kept and costs', () => {
    expect(shares(1000, 250)).toEqual([['net', 0.75], ['costs', 0.25]])
  })
  test('costs larger than the gain eat all of it and leave a loss', () => {
    expect(shares(100, 150)).toEqual([['costs', 0.67], ['loss', 0.33]])
  })
  test('costs deepen a loss', () => {
    expect(shares(-300, 100)).toEqual([['loss', 0.75], ['costs', 0.25]])
  })
  test('costs alone, and nothing at all', () => {
    expect(shares(0, 80)).toEqual([['costs', 1]])
    expect(waterfall(0, 0)).toEqual([])
  })
  test('the shares always make one whole', () => {
    for (const [gross, costs] of [[1000, 1], [5, 900], [-50, 50], [0.5, 0.25]]) {
      const total = waterfall(gross, costs).reduce((sum, s) => sum + s.share, 0)
      expect(total).toBeCloseTo(1, 10)
    }
  })
})

test('cost lines sum over fills, largest first, zero lines left out', () => {
  const lines = costLines([
    fill({ costs_breakdown: { brokerage: 20, stt: 5, gst: 3.6 } }),
    fill({ costs_breakdown: { brokerage: 20, stt: 7.5, gst: 3.6, stamp_duty: 0 } }),
  ])
  expect(lines.map((l) => l.label)).toEqual(['Brokerage', 'STT', 'GST'])
  expect(lines[0].amount).toBe(40)
  expect(lines[1].amount).toBe(12.5)
})

test.describe('option payoff at expiry', () => {
  // Bull put spread: sold 24500 PE at 150, bought 24400 PE at 100, 50 units.
  const spread = [
    { symbol: PUT_HIGH, quantity: -50, avg_cost: 150 },
    { symbol: PUT_LOW, quantity: 50, avg_cost: 100 },
  ]

  test('a credit spread keeps the net credit above the sold strike and loses the width less the credit below the bought', () => {
    expect(payoffAt(spread, 25000)).toBe(2500) // 50 x (150 - 100)
    expect(payoffAt(spread, 24000)).toBe(-2500) // 50 x ((24400-24500) + 50 credit) = 50 x -50
  })

  test('it is capped on both sides, with its break-even between the strikes', () => {
    const summary = payoffSummary(spread)
    expect(summary.maxProfit).toBe(2500)
    expect(summary.maxLoss).toBe(-2500)
    expect(summary.breakevens).toEqual([24450])
    expect(summary.underlying).toBe('NIFTY')
  })

  test('a long call loses its premium and has no upper limit', () => {
    const summary = payoffSummary([{ symbol: CALL, quantity: 50, avg_cost: 120 }])
    expect(summary.maxLoss).toBe(-6000)
    expect(summary.maxProfit).toBeNull()
    expect(summary.breakevens).toEqual([24620])
  })

  test('a naked sold call is reported as an unlimited loss', () => {
    const summary = payoffSummary([{ symbol: CALL, quantity: -50, avg_cost: 120 }])
    expect(summary.maxLoss).toBeNull()
    expect(summary.maxProfit).toBe(6000)
  })

  test('the curve runs through every strike and covers the spot', () => {
    const summary = payoffSummary(spread, 26000)
    const spots = summary.curve.map((p) => p.spot)
    expect(spots).toContain(24400)
    expect(spots).toContain(24500)
    expect(Math.max(...spots)).toBeGreaterThan(26000)
    expect([...spots].sort((a, b) => a - b)).toEqual(spots)
  })

  test('legs that are not options, or flat, draw nothing', () => {
    expect(payoffSummary([{ symbol: 'RELIANCE', quantity: 10, avg_cost: 100 }]).curve).toEqual([])
    expect(payoffSummary([{ symbol: CALL, quantity: 0, avg_cost: 120 }]).curve).toEqual([])
  })
})

test.describe('expiry', () => {
  test('counts days in India, not in the browser zone', () => {
    // 19:00 UTC on the 26th is 00:30 IST on the 27th: expiry day, whatever zone the browser is in.
    expect(daysToExpiry('2026-10-27', new Date('2026-10-26T19:00:00Z'))).toBe(0)
    expect(daysToExpiry('2026-10-27', new Date('2026-10-26T17:00:00Z'))).toBe(1)
    expect(daysToExpiry('2026-10-27', new Date('2026-10-28T05:00:00Z'))).toBe(-1)
  })

  test('a future is rolling inside three days and an option squares off on the last day', () => {
    const now = new Date('2026-10-26T05:00:00Z')
    expect(expiryCountdown('future', '2026-10-27', now)).toMatchObject({ days: 1, phase: 'near' })
    expect(expiryCountdown('future', '2026-11-27', now)?.phase).toBe('ok')
    expect(expiryCountdown('option', '2026-10-28', now)).toMatchObject({ days: 2, phase: 'ok' })
    expect(expiryCountdown('option', '2026-10-27', now)).toMatchObject({ days: 1, phase: 'near' })
    expect(expiryCountdown('option', '2026-10-20', now)?.phase).toBe('expired')
    expect(expiryCountdown('equity', null, now)).toBeNull()
  })
})

test.describe('what needs attention', () => {
  test('an unwatched position is critical and sorts first', () => {
    const items = attentionItems(
      [
        sub({ id: 'a', symbol: 'AAA', state: 'halted_no_data', state_reason: 'no feed' }),
        sub({ id: 'b', symbol: 'BBB', is_active: false, state: 'inactive', managed: false, open_legs: 1 }),
      ],
      true,
    )
    expect(items.map((i) => [i.subscriptionId, i.severity, i.action])).toEqual([
      ['b', 'critical', 'close'],
      ['a', 'warning', null],
    ])
  })

  test('a stale price counts only while the market is open', () => {
    const stale = sub({ last_tick: { price: 100, age_seconds: 900, source: 'zerodha' } })
    expect(attentionItems([stale], true).map((i) => i.id)).toEqual(['s1:stale'])
    expect(attentionItems([stale], false)).toEqual([])
  })

  test('an open position that has never been priced is flagged, a priced one is not', () => {
    expect(attentionItems([sub({ open_legs: 2, unrealized_pnl: null })], false).map((i) => i.id)).toEqual(['s1:unpriced'])
    expect(attentionItems([sub({ open_legs: 2, unrealized_pnl: 12 })], false)).toEqual([])
  })

  test('a healthy desk has nothing to show', () => {
    expect(attentionItems([sub()], true)).toEqual([])
  })
})

test.describe('fills and legs', () => {
  test('fills group into orders, newest first, legs in order', () => {
    const groups = groupFills([
      fill({ id: 'old', order_group_id: 'g1', created_at: '2026-10-15T04:00:00Z', costs_total: 10 }),
      fill({ id: 'b', order_group_id: 'g2', leg_index: 1, created_at: '2026-10-16T04:00:00Z', costs_total: 5 }),
      fill({ id: 'a', order_group_id: 'g2', leg_index: 0, created_at: '2026-10-16T04:00:01Z', costs_total: 7 }),
    ])
    expect(groups.map((g) => g.orderGroupId)).toEqual(['g2', 'g1'])
    expect(groups[0].fills.map((f) => f.id)).toEqual(['a', 'b'])
    expect(groups[0].costsTotal).toBe(12)
  })

  test('legs total their open P&L, and any unpriced leg makes the total unknown', () => {
    const leg = (symbol: string, quantity: number, unrealized_pnl: number | null): PaperPosition => ({
      subscription_id: 's1', symbol, quantity, avg_cost: 100, realized_pnl: 0, costs_total: 0, net_realized_pnl: 0,
      mark_price: null, mark_age_seconds: null, unrealized_pnl,
    })
    expect(legsUnrealized([leg(PUT_HIGH, -50, 300), leg(PUT_LOW, 50, -120), leg(CALL, 0, null)])).toBe(180)
    expect(legsUnrealized([leg(PUT_HIGH, -50, 300), leg(PUT_LOW, 50, null)])).toBeNull()
  })

  test('an option subscription expires when its soonest open leg does', () => {
    const leg = (symbol: string, quantity: number): PaperPosition => ({
      subscription_id: 's1', symbol, quantity, avg_cost: 100, realized_pnl: 0, costs_total: 0, net_realized_pnl: 0,
      mark_price: null, mark_age_seconds: null, unrealized_pnl: null,
    })
    const nearer = 'NFO:NIFTY|261020|24600|PE'
    expect(earliestLegExpiry([leg(PUT_HIGH, -50), leg(nearer, 50)])).toBe('2026-10-20')
    expect(earliestLegExpiry([leg(PUT_HIGH, -50), leg(nearer, 0)])).toBe('2026-10-27') // a flat leg does not count
    expect(earliestLegExpiry([])).toBeNull()
  })
})
