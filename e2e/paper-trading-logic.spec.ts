import { expect, test } from '@playwright/test'
import {
  attentionItems,
  closeConfirmationPhrase,
  confirmationMatches,
  costLines,
  daysToExpiry,
  earliestLegExpiry,
  describeContract,
  editableSettings,
  expiryCountdown,
  formatInr,
  formatSettingValue,
  groupFills,
  hasTraded,
  legsUnrealized,
  parseOptionSymbol,
  payoffAt,
  payoffSummary,
  planSettingsChange,
  settingsForm,
  STALE_PRICE_SECONDS,
  tone,
  underlyingSpot,
  waterfall,
} from '../lib/paper-trading'
import type { PaperFill, PaperPosition, PaperSubscription } from '../lib/api'
import attentionFixture from './fixtures/paper-attention.json'

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
    costs_total: 0, costs_breakdown: null, data_source: 'real', administrative: false, created_at: '2026-10-16T04:00:00Z', ...overrides,
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

  // The same cases the server runs (backend/tests/test_paper_attention.py): the engine alerts that
  // message a person must say exactly what this screen says, so neither side may change a rule alone.
  test.describe('agrees with the server on the shared cases', () => {
    test('with the same stale-price limit', () => {
      expect(STALE_PRICE_SECONDS).toBe(attentionFixture.stale_price_seconds)
    })

    type FixtureSub = {
      id: string
      symbol: string
      instrument_kind: PaperSubscription['instrument_kind']
      option_strategy: PaperSubscription['option_strategy']
      is_active: boolean
      managed: boolean
      state: PaperSubscription['state']
      state_reason: string | null
      open_legs: number
      unrealized_pnl: number | null
      last_tick: { age_seconds: number } | null
    }
    for (const fixtureCase of attentionFixture.cases) {
      test(fixtureCase.name, () => {
        const subs = (fixtureCase.subscriptions as FixtureSub[]).map((s) =>
          sub({ ...s, last_tick: s.last_tick ? { price: 100, source: 'zerodha', ...s.last_tick } : null }),
        )
        const items = attentionItems(subs, fixtureCase.market_open).map((i) => ({
          id: i.id,
          severity: i.severity,
          title: i.title,
          detail: i.detail,
          action: i.action,
        }))
        expect(items).toEqual(fixtureCase.expected)
      })
    }
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

test.describe('confirming a close', () => {
  test('the phrase is the symbol, and a longer one for an administrative close', () => {
    expect(closeConfirmationPhrase(' nifty ', false)).toBe('NIFTY')
    expect(closeConfirmationPhrase('nifty', true)).toBe('ADMIN NIFTY')
  })

  test('typing it matches ignoring case and spaces, and nothing else does', () => {
    expect(confirmationMatches('  nifty ', 'NIFTY')).toBe(true)
    expect(confirmationMatches('NIFT', 'NIFTY')).toBe(false)
    expect(confirmationMatches('NIFTY', 'ADMIN NIFTY')).toBe(false)
    expect(confirmationMatches('', '')).toBe(false) // an empty phrase never confirms anything
  })
})

test.describe('the underlying on the payoff chart', () => {
  const tick = (age: number) => ({ price: 24520, age_seconds: age, source: 'zerodha' })

  test('an option subscription reads its underlying from its own tick', () => {
    expect(underlyingSpot(sub({ instrument_kind: 'option', last_tick: tick(3) }))).toEqual({
      price: 24520,
      ageSeconds: 3,
      stale: false,
    })
  })

  test('an old price is still shown, but as old', () => {
    expect(underlyingSpot(sub({ instrument_kind: 'option', last_tick: tick(301) }))?.stale).toBe(true)
    expect(underlyingSpot(sub({ instrument_kind: 'option', last_tick: tick(300) }))?.stale).toBe(false)
  })

  test('another kind of subscription, or one never priced, has none', () => {
    expect(underlyingSpot(sub({ instrument_kind: 'future', last_tick: tick(1) }))).toBeNull()
    expect(underlyingSpot(sub({ instrument_kind: 'option', last_tick: null }))).toBeNull()
  })
})

test.describe('editing settings', () => {
  const equity = sub({ instrument_kind: 'equity' })
  const spread = sub({ instrument_kind: 'option', option_strategy: 'vertical_spread', stop_loss_pct: 50, spread_width: 2, lots: 1 })

  test('each kind is offered the settings that apply to it', () => {
    expect(editableSettings(equity)).toEqual(['stop_loss_pct', 'position_size_pct', 'builtin_strategy', 'sma_window', 'initial_capital'])
    expect(editableSettings(sub({ instrument_kind: 'future' }))).toEqual(['stop_loss_pct', 'lots', 'builtin_strategy', 'sma_window', 'initial_capital'])
    expect(editableSettings(spread)).toContain('spread_width')
    expect(editableSettings(sub({ instrument_kind: 'option', option_strategy: 'long_option' }))).not.toContain('spread_width')
  })

  test('only values that differ are changes, and 5.0 is the same as 5', () => {
    const form = { ...settingsForm(equity), stop_loss_pct: '6', position_size_pct: '5.0' }

    const { changes, errors } = planSettingsChange(equity, form)

    expect(errors).toEqual({})
    expect(changes.map((c) => [c.field, c.before, c.after, c.value])).toEqual([['stop_loss_pct', '3%', '6%', 6]])
  })

  test('nothing changed is no changes', () => {
    expect(planSettingsChange(equity, settingsForm(equity)).changes).toEqual([])
  })

  test('values outside the backend limits are errors, not changes', () => {
    const bad = (field: string, value: string) =>
      planSettingsChange(spread, { ...settingsForm(spread), [field]: value } as ReturnType<typeof settingsForm>)

    expect(bad('stop_loss_pct', '0').errors.stop_loss_pct).toBeDefined()
    expect(bad('stop_loss_pct', 'abc').errors.stop_loss_pct).toBe('Enter a number.')
    expect(bad('spread_width', '21').errors.spread_width).toBe('Use 1 to 20.')
    expect(bad('lots', '1.5').errors.lots).toBe('Use a whole number.')
    expect(bad('lots', '1001').errors.lots).toBe('Use 1 to 1000.')
    expect(planSettingsChange(equity, { ...settingsForm(equity), position_size_pct: '101' }).errors.position_size_pct).toBeDefined()
    expect(planSettingsChange(equity, { ...settingsForm(equity), position_size_pct: '100' }).errors).toEqual({})
    expect(planSettingsChange(equity, { ...settingsForm(equity), sma_window: '0' }).errors.sma_window).toBeDefined()
  })

  test('switching the signal is a change that is sent by name', () => {
    const { changes } = planSettingsChange(equity, { ...settingsForm(equity), builtin_strategy: 'always_long' })

    expect(changes).toHaveLength(1)
    expect(changes[0]).toMatchObject({ field: 'builtin_strategy', before: 'Moving average', after: 'Always long', value: 'always_long' })
  })

  test('each change says when it takes effect, and what holding a position adds', () => {
    const holding = sub({ instrument_kind: 'equity', open_legs: 1 })
    const form = { ...settingsForm(holding), stop_loss_pct: '5', sma_window: '20' }

    const effects = Object.fromEntries(planSettingsChange(holding, form).changes.map((c) => [c.field, c.effect]))

    expect(effects.stop_loss_pct).toContain('open position from the next price tick')
    expect(effects.sma_window).toContain('next daily signal (08:00 IST)')
    expect(effects.sma_window).toContain('closed on the tick after it')
    const flat = planSettingsChange(equity, { ...settingsForm(equity), sma_window: '20' }).changes[0]
    expect(flat.effect).not.toContain('closed on the tick')
  })

  test('capital is fixed once a subscription has been in the market', () => {
    expect(hasTraded(sub())).toBe(false)
    expect(hasTraded(sub({ open_legs: 1 }))).toBe(true)
    expect(hasTraded(sub({ realized_pnl: -5 }))).toBe(true)
    expect(hasTraded(sub({ costs_total: 12 }))).toBe(true)
  })

  test('values are written the way an operator reads them', () => {
    expect(formatSettingValue('stop_loss_pct', 6)).toBe('6%')
    expect(formatSettingValue('spread_width', 1)).toBe('1 strike')
    expect(formatSettingValue('spread_width', 3)).toBe('3 strikes')
    expect(formatSettingValue('initial_capital', 250000)).toBe('₹2,50,000')
    expect(formatSettingValue('builtin_strategy', 'always_long')).toBe('Always long')
  })
})
