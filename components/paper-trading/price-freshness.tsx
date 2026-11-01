'use client'

import type { PaperSubscription } from '@/lib/api'
import { circuitLine, exchangeFreshness, formatAge, formatInr } from '@/lib/paper-trading'

/**
 * The latest price of a subscription and how far to trust it: when it arrived, what the exchange's own
 * stamp says about it, which circuit band an order against it will be judged by, and, if the broker's
 * newest quotes are being refused, why. The stamp is the time of the last trade, so an old one is
 * reported as "a quiet instrument, or a feed that has stopped", not as a fault.
 */
export function PriceFreshness({ sub }: { sub: PaperSubscription }) {
  const tick = sub.last_tick
  const freshness = exchangeFreshness(tick)
  const circuit = circuitLine(tick, sub.circuit_fallback_band_pct)
  const refusal = sub.quote_refusal

  return (
    <section className="pt-price" aria-label="Latest price" data-testid="price-freshness">
      {tick ? (
        <p className="pt-price-now">
          Latest price {formatInr(tick.price, { decimals: 2 })}
          <span className="pt-muted" style={{ fontWeight: 400 }}> · arrived {formatAge(tick.age_seconds)} from {tick.source}</span>
        </p>
      ) : (
        <p className="pt-price-now">No price has arrived yet.</p>
      )}
      {tick && (
        <p className={`pt-price-${freshness.tone}`} data-testid="exchange-freshness" data-tone={freshness.tone}>
          {freshness.text}
        </p>
      )}
      {tick && (
        <p data-testid="circuit-line" data-source={circuit.source}>
          {circuit.text}
        </p>
      )}
      {refusal && (
        <p className="pt-price-refused" role="status" data-testid="quote-refusal">
          Its latest {refusal.broker} quote was refused: {refusal.reason}. Nothing trades on it until a current quote arrives.
        </p>
      )}
    </section>
  )
}
