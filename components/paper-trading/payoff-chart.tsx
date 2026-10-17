'use client'

import type { PayoffSummary } from '@/lib/paper-trading'
import { formatInr, tone } from '@/lib/paper-trading'

const W = 640
const H = 220
const PAD = { left: 8, right: 8, top: 12, bottom: 24 }

/** The at-expiry payoff of the held option legs (before costs): the line, the zero line, the
 * break-evens and where the underlying is now. Drawn by hand: the curve is a few straight
 * segments, and a chart library would hide the kinks that matter. */
export function PayoffChart({ summary, spot = null }: { summary: PayoffSummary; spot?: number | null }) {
  const { curve } = summary
  if (curve.length < 2) return null
  const xs = curve.map((p) => p.spot)
  const ys = curve.map((p) => p.payoff)
  const [xMin, xMax] = [Math.min(...xs), Math.max(...xs)]
  const [yLow, yHigh] = [Math.min(0, ...ys), Math.max(0, ...ys)]
  const ySpan = yHigh - yLow || 1
  const x = (value: number) => PAD.left + ((value - xMin) / (xMax - xMin || 1)) * (W - PAD.left - PAD.right)
  const y = (value: number) => PAD.top + (1 - (value - yLow) / ySpan) * (H - PAD.top - PAD.bottom)
  const zero = y(0)
  const line = curve.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.spot).toFixed(1)},${y(p.payoff).toFixed(1)}`).join(' ')
  const area = `${line} L${x(xMax).toFixed(1)},${zero.toFixed(1)} L${x(xMin).toFixed(1)},${zero.toFixed(1)} Z`
  const spotVisible = spot !== null && spot >= xMin && spot <= xMax
  const description =
    `Payoff at expiry: best ${summary.maxProfit === null ? 'unlimited' : formatInr(summary.maxProfit, { signed: true })}, ` +
    `worst ${summary.maxLoss === null ? 'unlimited' : formatInr(summary.maxLoss, { signed: true })}` +
    (summary.breakevens.length ? `, break-even ${summary.breakevens.map((b) => Math.round(b)).join(' and ')}` : '')

  return (
    <div className="pt-payoff">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={description}>
        <defs>
          <clipPath id="pt-above"><rect x="0" y="0" width={W} height={zero} /></clipPath>
          <clipPath id="pt-below"><rect x="0" y={zero} width={W} height={H - zero} /></clipPath>
        </defs>
        <path d={area} fill="rgba(52,211,153,.16)" clipPath="url(#pt-above)" />
        <path d={area} fill="rgba(251,113,133,.16)" clipPath="url(#pt-below)" />
        <line x1={PAD.left} x2={W - PAD.right} y1={zero} y2={zero} stroke="rgba(255,255,255,.22)" strokeDasharray="3 4" />
        <path d={line} fill="none" stroke="#e2e8f0" strokeWidth="2" strokeLinejoin="round" />
        {summary.breakevens.map((be) => (
          <g key={be}>
            <circle cx={x(be)} cy={zero} r="3.5" fill="#0a111c" stroke="#fbbf24" strokeWidth="2" />
            <text x={x(be)} y={H - 6} textAnchor="middle" fill="#fbbf24" fontSize="10" fontFamily="JetBrains Mono, monospace">
              {Math.round(be)}
            </text>
          </g>
        ))}
        {spotVisible && (
          <g>
            <line x1={x(spot)} x2={x(spot)} y1={PAD.top} y2={H - PAD.bottom} stroke="#22d3ee" strokeWidth="1.5" />
            <text x={x(spot)} y={PAD.top - 2} textAnchor="middle" fill="#22d3ee" fontSize="10" fontFamily="JetBrains Mono, monospace">
              now {Math.round(spot)}
            </text>
          </g>
        )}
      </svg>
      <div className="pt-payoff-stats">
        <div className="pt-fig"><span>BEST AT EXPIRY</span><span className="pt-gain">{summary.maxProfit === null ? 'unlimited' : formatInr(summary.maxProfit, { signed: true })}</span></div>
        <div className="pt-fig"><span>WORST AT EXPIRY</span><span className={summary.maxLoss === null ? 'pt-loss' : `pt-${tone(summary.maxLoss)}`}>{summary.maxLoss === null ? 'unlimited' : formatInr(summary.maxLoss, { signed: true })}</span></div>
        <div className="pt-fig"><span>BREAK-EVEN</span><span>{summary.breakevens.length ? summary.breakevens.map((b) => Math.round(b)).join(' · ') : '—'}</span></div>
      </div>
    </div>
  )
}
