import { expect, test } from '@playwright/test'
import type { PaperFill, PaperHistoryBackfill, PaperSignal, PaperSubscription, TodaysPaperPnl, UnrealizedPaperPnl } from '../lib/api'
import {
  type DeskReading,
  ORDERS_PER_STEP,
  csvCell,
  describeBackfill,
  fillsCsv,
  fillsFilename,
  isHistoryWait,
  ordersWindow,
  settleDesk,
  signalRows,
  signalStatus,
} from '../lib/paper-trading'

// What the Paper Desk says about price history, signals and fills, with no browser and no backend.

const backfill = (over: Partial<PaperHistoryBackfill> = {}): PaperHistoryBackfill => ({
  status: 'success',
  symbol: 'RELIANCE',
  rows: 1240,
  first_date: '2021-11-02',
  last_date: '2026-10-30',
  source: 'zerodha',
  unreal_rows_removed: 0,
  error: null,
  ...over,
})

const signal = (over: Partial<PaperSignal> = {}): PaperSignal => ({
  id: 'sg-1',
  subscription_id: 's1',
  symbol: 'RELIANCE',
  signal_type: 'BUY',
  reference_price: 2495,
  consumed: false,
  generated_at: '2026-11-02T03:45:00Z',
  consumed_at: null,
  ...over,
})

const fill = (over: Partial<PaperFill> = {}): PaperFill => ({
  id: 'f1',
  subscription_id: 's1',
  symbol: 'RELIANCE',
  side: 'buy',
  order_group_id: 'g1',
  leg_index: 0,
  requested_quantity: 10,
  filled_quantity: 10,
  avg_fill_price: 2500.5,
  fully_filled: true,
  realized_pnl: 0,
  costs_total: 12.5,
  costs_breakdown: { brokerage: 10, stt: 2.5 },
  data_source: 'real',
  administrative: false,
  created_at: '2026-11-02T03:45:00Z',
  ...over,
})

test.describe('fetching price history', () => {
  test('a success says how much, from where, over which dates', () => {
    expect(describeBackfill(backfill())).toBe('Fetched 1,240 daily prices for RELIANCE from zerodha (2021-11-02 to 2026-10-30).')
  })

  test('says what it replaced, in the singular and the plural', () => {
    expect(describeBackfill(backfill({ unreal_rows_removed: 12 }))).toMatch(/12 generated rows were replaced\.$/)
    expect(describeBackfill(backfill({ unreal_rows_removed: 1 }))).toMatch(/1 generated row was replaced\.$/)
  })

  test('leaves out what the server did not say', () => {
    expect(describeBackfill(backfill({ source: null, first_date: null, last_date: null, rows: 1 }))).toBe('Fetched 1 daily price for RELIANCE.')
  })

  test('a broker that returned nothing is said, not shown as a success of zero', () => {
    expect(describeBackfill(backfill({ rows: 0 }))).toBe('The broker returned no price history for RELIANCE.')
  })

  test('a failure carries the broker’s reason, or says there was none', () => {
    expect(describeBackfill(backfill({ status: 'failed', error: 'token expired' }))).toBe('Could not fetch the price history of RELIANCE: token expired.')
    expect(describeBackfill(backfill({ status: 'failed', error: null }))).toBe('Could not fetch the price history of RELIANCE: the broker did not say why.')
  })

  test('only a subscription waiting for history is offered the fetch', () => {
    expect(isHistoryWait({ id: 's1:history' })).toBe(true)
    expect(isHistoryWait({ id: 's1:halted' })).toBe(false)
  })
})

test.describe('daily signals', () => {
  test('come newest first', () => {
    const rows = signalRows([
      signal({ id: 'old', generated_at: '2026-10-30T03:45:00Z' }),
      signal({ id: 'new', generated_at: '2026-11-02T03:45:00Z' }),
      signal({ id: 'mid', generated_at: '2026-10-31T03:45:00Z' }),
    ])

    expect(rows.map((s) => s.id)).toEqual(['new', 'mid', 'old'])
  })

  test('ordering them does not change the list they came in', () => {
    const input = [signal({ id: 'a', generated_at: '2026-10-30T00:00:00Z' }), signal({ id: 'b', generated_at: '2026-11-02T00:00:00Z' })]
    signalRows(input)
    expect(input.map((s) => s.id)).toEqual(['a', 'b'])
  })

  test('say whether a position was taken on them, and when, in India time', () => {
    expect(signalStatus(signal())).toBe('Not acted on yet')
    expect(signalStatus(signal({ consumed: true, consumed_at: '2026-11-02T03:46:00Z' }))).toBe('Acted on 02 Nov 2026, 09:16:00')
    expect(signalStatus(signal({ consumed: true, consumed_at: null }))).toBe('Acted on')
  })
})

test.describe('the fills as a file', () => {
  test('a cell is quoted only when it has to be', () => {
    expect(csvCell('RELIANCE')).toBe('RELIANCE')
    expect(csvCell('a,b')).toBe('"a,b"')
    expect(csvCell('say "hi"')).toBe('"say ""hi"""')
    expect(csvCell('two\nlines')).toBe('"two\nlines"')
  })

  test('nothing, a number and a flag are plain', () => {
    expect(csvCell(null)).toBe('')
    expect(csvCell(undefined)).toBe('')
    expect(csvCell(2500.5)).toBe('2500.5')
    expect(csvCell(false)).toBe('false')
    expect(csvCell(-12.5)).toBe('-12.5')
  })

  test('a text a spreadsheet would run as a formula is made plain text', () => {
    expect(csvCell('=SUM(A1:A9)')).toBe("'=SUM(A1:A9)")
    expect(csvCell('@cmd')).toBe("'@cmd")
    expect(csvCell('+1')).toBe("'+1")
  })

  test('has one header and one row per fill, oldest first, with each cost line', () => {
    const csv = fillsCsv([
      fill({ id: 'late', created_at: '2026-11-03T03:45:00Z', side: 'sell', administrative: true, realized_pnl: 80 }),
      fill({ id: 'early' }),
    ])

    const lines = csv.trimEnd().split('\r\n')
    expect(lines).toHaveLength(3)
    expect(lines[0]).toBe(
      'created_at_utc,time_india,symbol,side,requested_quantity,filled_quantity,avg_fill_price,fully_filled,data_source,gross_realized_pnl,costs_total,brokerage,stt,exchange_txn_charges,sebi_fee,stamp_duty,gst,manual_close,order_group_id,leg_index',
    )
    expect(lines[1]).toBe('2026-11-02T03:45:00Z,"02 Nov 2026, 09:15:00",RELIANCE,buy,10,10,2500.5,true,real,0,12.5,10,2.5,0,0,0,0,false,g1,0')
    expect(lines[2]).toContain(',sell,')
    expect(lines[2]).toContain(',80,12.5,')
    expect(lines[2]).toContain(',true,g1,0') // the manual close
  })

  test('a price that was never filled is an empty cell, not the word null', () => {
    const [, row] = fillsCsv([fill({ avg_fill_price: null, fully_filled: false, filled_quantity: 0 })]).trimEnd().split('\r\n')
    expect(row).not.toContain('null')
    // the India-time cell holds a comma, so on a plain split the price is the eighth cell, not the seventh
    expect(row.split(',')[7]).toBe('')
  })

  test('is named for the symbol and the day in India', () => {
    expect(fillsFilename('NFO:NIFTY|261027|24500|CE', new Date('2026-11-02T20:00:00Z'))).toBe('paper-fills-NFO_NIFTY_261027_24500_CE-2026-11-03.csv')
    expect(fillsFilename('RELIANCE', new Date('2026-11-02T03:45:00Z'))).toBe('paper-fills-RELIANCE-2026-11-02.csv')
  })
})

test.describe('how many orders are shown', () => {
  test('a list opens on the newest few and grows a step at a time', () => {
    expect(ordersWindow(41, ORDERS_PER_STEP)).toEqual({ visible: 8, remaining: 33, next: 16 })
    expect(ordersWindow(41, 16)).toEqual({ visible: 16, remaining: 25, next: 24 })
  })

  test('the last step shows what is left and no more', () => {
    expect(ordersWindow(41, 40)).toEqual({ visible: 40, remaining: 1, next: 41 })
    expect(ordersWindow(41, 41)).toEqual({ visible: 41, remaining: 0, next: 41 })
  })

  test('a short list shows all of itself, and an empty one nothing', () => {
    expect(ordersWindow(3, 8)).toEqual({ visible: 3, remaining: 0, next: 3 })
    expect(ordersWindow(0, 8)).toEqual({ visible: 0, remaining: 0, next: 0 })
  })
})

test.describe('reading the desk when part of it cannot be read', () => {
  const subs = [{ id: 's1' }] as PaperSubscription[]
  const today = { net_realized_pnl: 100 } as TodaysPaperPnl
  const unrealized = { unrealized_pnl: 50 } as UnrealizedPaperPnl
  const ok = <T>(value: T): PromiseFulfilledResult<T> => ({ status: 'fulfilled', value })
  const failed = (reason: string): PromiseRejectedResult => ({ status: 'rejected', reason: new Error(reason) })
  const say = (reason: unknown) => (reason instanceof Error ? reason.message : String(reason))

  test('everything read is everything shown, with no problem', () => {
    expect(settleDesk(null, [ok(subs), ok(today), ok(unrealized)], say)).toEqual({ subs, today, unrealized, problems: [] })
  })

  test('without the subscriptions there is no desk: the reading fails, so the page keeps the last one', () => {
    expect(() => settleDesk(null, [failed('db down'), ok(today), ok(unrealized)], say)).toThrow('db down')
  })

  test('a figure that cannot be read keeps its last value and says so; the rest is still shown', () => {
    const previous: DeskReading = { subs: [], today, unrealized, problems: [] }

    const reading = settleDesk(previous, [ok(subs), failed('timeout'), ok({ unrealized_pnl: 75 } as UnrealizedPaperPnl)], say)

    expect(reading.subs).toBe(subs)
    expect(reading.today).toBe(today)
    expect(reading.unrealized).toEqual({ unrealized_pnl: 75 })
    expect(reading.problems).toEqual(['Today’s result could not be read (timeout); the figure shown is the last one read.'])
  })

  test('a figure never read has nothing to fall back on, and says so', () => {
    const reading = settleDesk(null, [ok(subs), ok(today), failed('no marks')], say)

    expect(reading.unrealized).toBeNull()
    expect(reading.problems).toEqual(['Open profit and loss could not be read (no marks); the figure shown is the last one read.'])
  })

  test('both figures failing are two problems', () => {
    expect(settleDesk(null, [ok(subs), failed('a'), failed('b')], say).problems).toHaveLength(2)
  })
})
