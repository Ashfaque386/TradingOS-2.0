'use client'

import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { useConsoleState } from '@/components/shell/console-state'
import { ErrorNotice } from '@/components/ui/error-notice'
import { type KillSwitchMode, evaluateKillSwitch, getEquityHistory } from '@/lib/api'
import { buildGeometry, summarize } from '@/lib/equity-chart'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'

const W = 640
const H = 200

const inr = (value: number | null) =>
  value === null ? 'unknown' : `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
const pct = (value: number | null) => (value === null ? 'unknown' : `${value.toFixed(1)}%`)

const OUTCOME_TEXT: Record<string, string> = {
  ok: 'Measured now: within the limit.',
  tripped: 'Measured now: the limit was reached and the kill switch tripped.',
  already_tripped: 'Measured now. The kill switch was already tripped and stays tripped until it is reset.',
  cannot_evaluate: 'Could not be measured, so nothing was judged.',
}

/**
 * Equity and drawdown for one mode: the line, the level the kill switch trips at, and every stretch the
 * equity could not be measured shown as a gap. Reads what the drawdown monitor has stored; "Measure now"
 * (risk roles) takes a reading and runs the same check the monitor runs.
 */
export function EquityPanel({ mode, compact = false }: { mode: KillSwitchMode; compact?: boolean }) {
  const { role } = useAuth()
  const { refresh: refreshConsole } = useConsoleState()
  const canMeasure = can(role, 'POST /api/v1/kill-switch/{mode}/evaluate')
  const { data, error, refresh } = usePolling(() => getEquityHistory(mode, 400), { intervalMs: 60_000 })
  const [measuring, setMeasuring] = useState(false)
  const [measureMessage, setMeasureMessage] = useState<string | null>(null)
  const [measureError, setMeasureError] = useState<string | null>(null)

  async function measureNow() {
    setMeasuring(true)
    setMeasureError(null)
    try {
      const result = await evaluateKillSwitch(mode)
      setMeasureMessage(OUTCOME_TEXT[result.outcome] ?? result.outcome)
      await Promise.all([refresh(), refreshConsole()])
    } catch (err) {
      setMeasureError(err instanceof Error && err.message ? err.message : 'Could not measure the equity')
    } finally {
      setMeasuring(false)
    }
  }

  const snapshots = data?.snapshots ?? []
  const threshold = data?.threshold_pct ?? 15
  const summary = summarize(snapshots, threshold)
  const geometry = buildGeometry(snapshots, threshold, W, compact ? 140 : H)
  const description = geometry
    ? `${mode} equity ${inr(summary.lastMeasured?.equity ?? null)}, peak ${inr(summary.peak)}, drawdown ${pct(summary.drawdownPct)} of a ${threshold}% limit`
    : `No ${mode} equity has been measured yet`

  return (
    <section className={`eq-panel${compact ? ' eq-compact' : ''}`} aria-label={`${mode} equity and drawdown`}>
      <header className="eq-head">
        <div>
          <h2>Equity and drawdown · {mode}</h2>
          <p>The kill switch trips when equity falls {threshold}% below its highest point since the last reset.</p>
        </div>
        {canMeasure && (
          <button type="button" className="button-secondary" onClick={() => void measureNow()} disabled={measuring}>
            <RefreshCw className={`size-3 ${measuring ? 'animate-spin' : ''}`} aria-hidden="true" />
            Measure now
          </button>
        )}
      </header>

      {error && <ErrorNotice message={`Could not load the equity history: ${error}`} onRetry={() => void refresh()} />}
      {measureError && <ErrorNotice message={measureError} />}
      {measureMessage && !measureError && <p className="eq-note" role="status">{measureMessage}</p>}

      <dl className="eq-stats">
        <div><dt>Equity</dt><dd>{inr(summary.lastMeasured?.equity ?? null)}</dd></div>
        <div><dt>Highest</dt><dd>{inr(summary.peak)}</dd></div>
        <div>
          <dt>Drawdown</dt>
          <dd className={summary.headroomPct !== null && summary.headroomPct <= threshold / 3 ? 'eq-warn' : ''}>
            {pct(summary.drawdownPct)} <small>of {threshold}%</small>
          </dd>
        </div>
        <div><dt>Trips at</dt><dd>{inr(summary.tripLevel)}</dd></div>
      </dl>

      {summary.gapNote && (
        <p className="eq-gap" role="status">
          Not measured at the latest reading: {summary.gapNote}. The kill switch does not judge a reading it could not measure.
        </p>
      )}

      {geometry ? (
        <>
          <svg viewBox={`0 0 ${geometry.width} ${geometry.height}`} role="img" aria-label={description} className="eq-chart">
            {geometry.gaps.map((gap, i) => (
              <rect key={i} x={gap.x1} y={0} width={gap.x2 - gap.x1} height={geometry.height} className="eq-gap-band" />
            ))}
            {geometry.yTicks.map((tick) => (
              <g key={tick.value}>
                <line x1={0} x2={geometry.width} y1={tick.y} y2={tick.y} className="eq-grid" />
                <text x={geometry.width - 4} y={tick.y - 3} textAnchor="end" className="eq-tick">{inr(tick.value)}</text>
              </g>
            ))}
            {geometry.tripPath && <path d={geometry.tripPath} className="eq-trip" fill="none" />}
            {geometry.equityPaths.map((path, i) => <path key={i} d={path} className="eq-line" fill="none" />)}
            {geometry.dots.map((dot, i) => <circle key={i} cx={dot.x} cy={dot.y} r={3.5} className="eq-dot" />)}
          </svg>
          <p className="eq-legend">
            <span className="eq-key eq-key-line" />Equity <span className="eq-key eq-key-trip" />Where the kill switch trips{' '}
            <span className="eq-key eq-key-gap" />Could not be measured
          </p>
        </>
      ) : (
        <p className="eq-empty">
          {snapshots.length === 0
            ? `No equity has been measured yet. The monitor reads it every minute while the market is open and once at the close.${canMeasure ? ' You can also measure now.' : ''}`
            : 'Every reading so far could not be measured; the reason is above.'}
        </p>
      )}

      {!compact && snapshots.length > 0 && (
        <details className="eq-readings">
          <summary>Latest readings</summary>
          <table>
            <thead><tr><th>Time</th><th>Equity</th><th>Highest</th><th>Drawdown</th><th>Prices</th></tr></thead>
            <tbody>
              {[...snapshots].reverse().slice(0, 10).map((s) => (
                <tr key={s.taken_at}>
                  <td>{new Date(s.taken_at).toLocaleString()}</td>
                  <td>{s.equity === null ? `not measured (${s.note ?? 'unknown'})` : inr(s.equity)}</td>
                  <td>{inr(s.peak_equity)}</td>
                  <td>{pct(s.drawdown_pct)}</td>
                  <td>{s.price_source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  )
}
