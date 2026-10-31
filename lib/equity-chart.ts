import type { EquitySnapshot } from './api'

/**
 * Geometry and wording for the equity chart, kept free of React so a logic spec can check it.
 *
 * Two rules the chart is built on: a reading that could not be measured is a **gap** (the line breaks
 * and the span is shaded), never a zero or a joined-up line, and the kill switch's **trip level** is
 * drawn where it really is: the highest equity so far less the drawdown limit, so the distance
 * between the line and the trip level is the room left before the switch trips.
 */

export type Point = { x: number; y: number }

export interface ChartGeometry {
  width: number
  height: number
  /** One path per run of measured readings; a gap ends a run. */
  equityPaths: string[]
  /** A single reading on its own is drawn as a dot, since a path of one point shows nothing. */
  dots: Point[]
  /** The trip level over time (peak less the limit); empty before there is a peak. */
  tripPath: string
  /** x-ranges where the equity could not be measured. */
  gaps: Array<{ x1: number; x2: number }>
  yTicks: Array<{ y: number; value: number }>
  domain: { minValue: number; maxValue: number; startMs: number; endMs: number }
}

export function tripLevel(peakEquity: number, thresholdPct: number): number {
  return peakEquity * (1 - thresholdPct / 100)
}

const ms = (iso: string) => new Date(iso).getTime()

/** Runs of consecutive measured readings: a null equity ends a run. */
export function measuredRuns(snapshots: EquitySnapshot[]): EquitySnapshot[][] {
  const runs: EquitySnapshot[][] = []
  let current: EquitySnapshot[] = []
  for (const snapshot of snapshots) {
    if (snapshot.equity === null) {
      if (current.length) runs.push(current)
      current = []
    } else {
      current.push(snapshot)
    }
  }
  if (current.length) runs.push(current)
  return runs
}

export function buildGeometry(
  snapshots: EquitySnapshot[],
  thresholdPct: number,
  width: number,
  height: number,
  pad = { left: 8, right: 8, top: 8, bottom: 8 },
): ChartGeometry | null {
  const measured = snapshots.filter((s) => s.equity !== null)
  if (measured.length === 0) return null

  const times = snapshots.map((s) => ms(s.taken_at))
  const startMs = Math.min(...times)
  const endMs = Math.max(...times)
  const levels = measured.flatMap((s) => {
    const values = [s.equity as number]
    if (s.peak_equity !== null) values.push(s.peak_equity, tripLevel(s.peak_equity, thresholdPct))
    return values
  })
  const low = Math.min(...levels)
  const high = Math.max(...levels)
  const margin = (high - low || Math.abs(high) || 1) * 0.06
  const minValue = low - margin
  const maxValue = high + margin

  const innerW = width - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom
  const x = (t: number) => pad.left + (endMs === startMs ? innerW / 2 : ((t - startMs) / (endMs - startMs)) * innerW)
  const y = (v: number) => pad.top + (1 - (v - minValue) / (maxValue - minValue)) * innerH

  const equityPaths: string[] = []
  const dots: Point[] = []
  for (const run of measuredRuns(snapshots)) {
    if (run.length === 1) {
      dots.push({ x: x(ms(run[0].taken_at)), y: y(run[0].equity as number) })
      continue
    }
    equityPaths.push(
      run.map((s, i) => `${i === 0 ? 'M' : 'L'}${x(ms(s.taken_at)).toFixed(1)},${y(s.equity as number).toFixed(1)}`).join(' '),
    )
  }

  const withPeak = snapshots.filter((s) => s.peak_equity !== null)
  const tripPath = withPeak
    .map((s, i) => `${i === 0 ? 'M' : 'L'}${x(ms(s.taken_at)).toFixed(1)},${y(tripLevel(s.peak_equity as number, thresholdPct)).toFixed(1)}`)
    .join(' ')

  // A gap runs from the last reading before it to the next one after it (or to the edge).
  const gaps: Array<{ x1: number; x2: number }> = []
  for (let i = 0; i < snapshots.length; i += 1) {
    if (snapshots[i].equity !== null) continue
    let j = i
    while (j + 1 < snapshots.length && snapshots[j + 1].equity === null) j += 1
    const before = i > 0 ? ms(snapshots[i - 1].taken_at) : ms(snapshots[i].taken_at)
    const after = j + 1 < snapshots.length ? ms(snapshots[j + 1].taken_at) : ms(snapshots[j].taken_at)
    gaps.push({ x1: x(before), x2: Math.max(x(after), x(before) + 2) })
    i = j
  }

  const yTicks = [0, 0.5, 1].map((fraction) => {
    const value = minValue + (maxValue - minValue) * fraction
    return { y: y(value), value }
  })

  return { width, height, equityPaths, dots, tripPath, gaps, yTicks, domain: { minValue, maxValue, startMs, endMs } }
}

export interface EquitySummary {
  /** The newest reading, measured or not. */
  latest: EquitySnapshot | null
  /** The newest reading that was measured. */
  lastMeasured: EquitySnapshot | null
  /** Why the newest reading is a gap, when it is. */
  gapNote: string | null
  peak: number | null
  drawdownPct: number | null
  /** How many points of drawdown remain before the limit, never below zero. */
  headroomPct: number | null
  tripLevel: number | null
}

export function summarize(snapshots: EquitySnapshot[], thresholdPct: number): EquitySummary {
  const latest = snapshots.length ? snapshots[snapshots.length - 1] : null
  const lastMeasured = [...snapshots].reverse().find((s) => s.equity !== null) ?? null
  const peak = lastMeasured?.peak_equity ?? null
  const drawdownPct = lastMeasured?.drawdown_pct ?? null
  return {
    latest,
    lastMeasured,
    gapNote: latest && latest.equity === null ? (latest.note ?? 'The equity could not be measured') : null,
    peak,
    drawdownPct,
    headroomPct: drawdownPct === null ? null : Math.max(0, thresholdPct - drawdownPct),
    tripLevel: peak === null ? null : tripLevel(peak, thresholdPct),
  }
}
