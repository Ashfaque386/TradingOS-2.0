import { expect, test } from '@playwright/test'
import type { LiveOrderDto, PositionByStrategy } from '../lib/api'
import {
  EXECUTION_STATUSES,
  INTENT_STATUSES,
  NO_ORDERS_FILTER,
  failureLine,
  formatPnl,
  formatRupees,
  ordersFiltered,
  ordersQuery,
  positionNote,
  sourceNote,
  statusLabel,
  statusTone,
} from '../lib/orders'
import { indiaStamp } from '../lib/time'

// What the Orders & Trades page decides, with no browser and no backend: each case states the answer worked out by hand.

const position = (over: Partial<PositionByStrategy> = {}): PositionByStrategy => ({
  mode: 'paper',
  strategy_id: 's-1',
  strategy_name: 'Alpha',
  symbol: 'TCS',
  quantity: 10,
  avg_cost: 3400,
  realized_pnl: 0,
  managed: true,
  ...over,
})

const order = (over: Partial<LiveOrderDto> = {}): LiveOrderDto => ({
  id: 'o-1',
  live_order_intent_id: 'i-1',
  strategy_id: 's-1',
  symbol: 'INFY',
  side: 'buy',
  quantity: 5,
  broker_name: 'zerodha',
  broker_order_id: 'OID-1',
  status: 'submitted',
  failure_reason: null,
  submitted_at: '2026-11-02T04:00:00Z',
  ...over,
})

test.describe('statuses', () => {
  test('every status an execution or an intent can have has a tone, so none is left unstyled', () => {
    for (const status of [...EXECUTION_STATUSES, ...INTENT_STATUSES]) {
      expect(['good', 'warn', 'bad'], status).toContain(statusTone(status))
    }
  })

  test('finished well is good, still open is a warning, not done is bad', () => {
    expect(statusTone('filled')).toBe('good')
    expect(statusTone('pending_confirmation')).toBe('warn')
    expect(statusTone('partial')).toBe('warn')
    expect(statusTone('failed')).toBe('bad')
    expect(statusTone('cancelled')).toBe('bad')
    expect(statusTone('something-new')).toBe('neutral')
  })

  test('are read with spaces, not underscores', () => {
    expect(statusLabel('pending_confirmation')).toBe('pending confirmation')
  })

  test('the ledger offers a live trade’s statuses and a paper fill’s, and none that never occurs', () => {
    expect([...EXECUTION_STATUSES]).toEqual(['filled', 'partial', 'pending_confirmation', 'rejected', 'cancelled', 'failed'])
    expect(EXECUTION_STATUSES).not.toContain('open' as never)
  })
})

test.describe('what a price rests on', () => {
  test('a real price is not a caution', () => {
    expect(sourceNote('real')).toMatchObject({ label: 'Real', caution: false })
  })

  test('anything else is a caution, and says why', () => {
    for (const source of ['synthetic', 'mixed', 'supplied', 'unknown', 'never-heard-of']) {
      const note = sourceNote(source)
      expect(note.caution, source).toBe(true)
      expect(note.explanation.length, source).toBeGreaterThan(10)
    }
    expect(sourceNote('synthetic').label).toBe('Simulated')
    expect(sourceNote('unknown').label).toBe('Not recorded')
  })
})

test.describe('the request for what is on screen', () => {
  test('the first page starts at the beginning, a later page skips the pages before it', () => {
    expect(ordersQuery(NO_ORDERS_FILTER, 1, 25)).toEqual({ mode: 'both', limit: 25, offset: 0 })
    expect(ordersQuery(NO_ORDERS_FILTER, 3, 25)).toEqual({ mode: 'both', limit: 25, offset: 50 })
  })

  test('a page before the first is the first', () => {
    expect(ordersQuery(NO_ORDERS_FILTER, 0, 25).offset).toBe(0)
  })

  test('carries each filter, trims the symbol and leaves out what is empty', () => {
    expect(ordersQuery({ mode: 'live', status: 'failed', symbol: '  infy ', strategyId: 's-1' }, 1, 50)).toEqual({
      mode: 'live',
      status: 'failed',
      symbol: 'infy',
      strategyId: 's-1',
      limit: 50,
      offset: 0,
    })
    expect(ordersQuery({ ...NO_ORDERS_FILTER, symbol: '   ' }, 1, 25)).toEqual({ mode: 'both', limit: 25, offset: 0 })
  })

  test('is filtered when anything differs from showing everything', () => {
    expect(ordersFiltered(NO_ORDERS_FILTER)).toBe(false)
    expect(ordersFiltered({ ...NO_ORDERS_FILTER, mode: 'paper' })).toBe(true)
    expect(ordersFiltered({ ...NO_ORDERS_FILTER, symbol: ' ' })).toBe(false)
    expect(ordersFiltered({ ...NO_ORDERS_FILTER, strategyId: 's-1' })).toBe(true)
  })
})

test.describe('positions', () => {
  test('one something is watching says nothing', () => {
    expect(positionNote(position())).toBeNull()
  })

  test('one on a switched-off subscription says nothing is watching it', () => {
    expect(positionNote(position({ managed: false }))).toMatch(/no stop-loss is looking at it/)
  })

  test('profit is signed, in rupees, with the Indian grouping', () => {
    expect(formatPnl(1250)).toBe('+₹1,250.00')
    expect(formatPnl(-300.5)).toBe('-₹300.50')
    expect(formatPnl(0)).toBe('₹0.00')
    expect(formatPnl(1234567.8)).toBe('+₹12,34,567.80')
  })

  test('a price is in rupees, and a missing one is a dash', () => {
    expect(formatRupees(1500)).toBe('₹1,500.00')
    expect(formatRupees(null)).toBe('—')
    expect(formatRupees(undefined)).toBe('—')
    expect(formatRupees(Number.NaN)).toBe('—')
  })
})

test.describe('a live order that did not go through', () => {
  test('says the broker’s reason', () => {
    expect(failureLine(order({ status: 'failed', failure_reason: ' Insufficient funds ' }))).toBe('Insufficient funds')
  })

  test('a failure with no reason says there was none, instead of saying nothing', () => {
    expect(failureLine(order({ status: 'failed', failure_reason: null }))).toBe('The broker refused it and gave no reason.')
    expect(failureLine(order({ status: 'failed', failure_reason: '  ' }))).toBe('The broker refused it and gave no reason.')
  })

  test('an order that went through has no failure to show', () => {
    expect(failureLine(order())).toBeNull()
  })
})

test.describe('times', () => {
  test('are India time whatever the browser’s zone is', () => {
    expect(indiaStamp('2026-11-02T04:00:00Z')).toBe('02 Nov 2026, 09:30:00')
  })
})
