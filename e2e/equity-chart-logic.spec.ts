import { expect, test } from '@playwright/test'
import type { EquitySnapshot } from '../lib/api'
import { buildGeometry, measuredRuns, summarize, tripLevel } from '../lib/equity-chart'

// The equity chart's rules: a reading that could not be measured is a gap, not a zero, and the trip
// level is where the kill switch really trips (the peak less the limit).

function reading(minute: number, equity: number | null, peak: number | null = equity, drawdown: number | null = 0, note: string | null = null): EquitySnapshot {
  return {
    taken_at: new Date(Date.UTC(2026, 9, 30, 4, minute)).toISOString(),
    equity,
    capital_base: 100_000,
    realized_net: 0,
    unrealized: 0,
    open_positions: 0,
    unpriced_positions: equity === null ? 1 : 0,
    price_source: 'real',
    peak_equity: peak,
    drawdown_pct: drawdown,
    note,
  }
}

test('the trip level is the peak less the limit', () => {
  expect(tripLevel(100_000, 15)).toBe(85_000)
  expect(tripLevel(120_000, 10)).toBe(108_000)
})

test('a reading that could not be measured ends a run instead of joining the line', () => {
  const runs = measuredRuns([reading(0, 100), reading(1, 101), reading(2, null), reading(3, 99)])

  expect(runs.map((run) => run.map((s) => s.equity))).toEqual([[100, 101], [99]])
})

test('there is nothing to draw until something has been measured', () => {
  expect(buildGeometry([], 15, 300, 100)).toBeNull()
  expect(buildGeometry([reading(0, null, null, null, 'no price')], 15, 300, 100)).toBeNull()
})

test('a gap is shaded between the readings either side of it, and the line breaks there', () => {
  const geometry = buildGeometry(
    [reading(0, 100_000), reading(1, 101_000), reading(2, null, 101_000, null, 'unpriced'), reading(3, null, 101_000, null, 'unpriced'), reading(4, 99_000, 101_000, 1.98)],
    15,
    400,
    120,
  )!

  expect(geometry.equityPaths).toHaveLength(1) // the 99,000 reading is a run of one: a dot
  expect(geometry.dots).toHaveLength(1)
  expect(geometry.gaps).toHaveLength(1) // two readings in a row are one gap
  expect(geometry.gaps[0].x2).toBeGreaterThan(geometry.gaps[0].x1)
})

test('the scale reaches down to the trip level, so the line the switch trips at is always on the chart', () => {
  const geometry = buildGeometry([reading(0, 100_000), reading(1, 100_500)], 15, 400, 120)!

  expect(geometry.domain.minValue).toBeLessThan(85_000)
  expect(geometry.domain.maxValue).toBeGreaterThan(100_500)
  expect(geometry.tripPath.startsWith('M')).toBe(true)
})

test('a single reading is drawn as a dot, centred', () => {
  const geometry = buildGeometry([reading(0, 100_000)], 15, 400, 120)!

  expect(geometry.equityPaths).toEqual([])
  expect(geometry.dots).toHaveLength(1)
  expect(geometry.dots[0].x).toBe(200)
})

test('the summary reports the newest measured reading, the headroom, and why the newest is a gap', () => {
  const summary = summarize(
    [reading(0, 100_000, 100_000, 0), reading(1, 92_000, 100_000, 8), reading(2, null, 100_000, null, '2 open position(s) have no price')],
    15,
  )

  expect(summary.lastMeasured?.equity).toBe(92_000)
  expect(summary.peak).toBe(100_000)
  expect(summary.drawdownPct).toBe(8)
  expect(summary.headroomPct).toBe(7)
  expect(summary.tripLevel).toBe(85_000)
  expect(summary.gapNote).toBe('2 open position(s) have no price')
})

test('headroom never goes below zero and an empty history says nothing', () => {
  expect(summarize([reading(0, 80_000, 100_000, 20)], 15).headroomPct).toBe(0)
  const empty = summarize([], 15)
  expect(empty.latest).toBeNull()
  expect(empty.peak).toBeNull()
  expect(empty.headroomPct).toBeNull()
  expect(empty.gapNote).toBeNull()
})
