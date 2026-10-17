'use client'

import type { TodaysPaperPnl, UnrealizedPaperPnl } from '@/lib/api'
import { formatInr, tone, waterfall } from '@/lib/paper-trading'

const SEGMENT_LABEL = { net: 'Kept', costs: 'Costs', loss: 'Lost' } as const

/** The bar under the day's result: how gross becomes net. */
export function Waterfall({ gross, costs }: { gross: number; costs: number }) {
  const segments = waterfall(gross, costs)
  if (segments.length === 0) return null
  const summary = segments.map((s) => `${SEGMENT_LABEL[s.kind]} ${Math.round(s.share * 100)}%`).join(', ')
  return (
    <>
      <div className="pt-waterfall" role="img" aria-label={`Where today's result went: ${summary}`}>
        {segments.map((segment) => (
          <span key={segment.kind} className={`pt-waterfall-seg pt-seg-${segment.kind}`} style={{ width: `${segment.share * 100}%` }} />
        ))}
      </div>
      <div className="pt-waterfall-legend" aria-hidden="true">
        {segments.map((segment) => (
          <span key={segment.kind}>
            <i className={`pt-seg-${segment.kind}`} />
            {SEGMENT_LABEL[segment.kind]} {Math.round(segment.share * 100)}%
          </span>
        ))}
      </div>
    </>
  )
}

/** The desk's header: today's realized result as `gross − costs = net`, and what is still open. */
export function Ledger({
  today,
  unrealized,
  openCount,
}: {
  today: TodaysPaperPnl | null
  unrealized: UnrealizedPaperPnl | null
  openCount: number
}) {
  const net = today?.net_realized_pnl ?? null
  const unrealizedTone = tone(unrealized?.unrealized_pnl)
  return (
    <section className="pt-ledger" aria-label="Today's result">
      <div className="pt-ledger-top">
        <div className="pt-equation" role="group" aria-label="Realized today: price result, minus costs, equals net">
          <div className="pt-term">
            <span className="pt-term-label">Price result</span>
            <span className={`pt-term-value pt-${tone(today?.realized_pnl)}`}>{formatInr(today?.realized_pnl, { signed: true, decimals: 2 })}</span>
          </div>
          <span className="pt-op" aria-hidden="true">−</span>
          <div className="pt-term">
            <span className="pt-term-label">Costs</span>
            <span className="pt-term-value" style={{ color: '#fbbf24' }}>{formatInr(today?.costs_total, { decimals: 2 })}</span>
          </div>
          <span className="pt-op" aria-hidden="true">=</span>
          <div className="pt-term pt-term-net">
            <span className="pt-term-label">Net realized today</span>
            <span className={`pt-term-value pt-${tone(net)}`}>{formatInr(net, { signed: true, decimals: 2 })}</span>
          </div>
        </div>
        <div className="pt-ledger-side">
          <span>
            Open now{' '}
            <b className={`pt-${unrealizedTone}`}>
              {unrealized === null
                ? '—'
                : unrealized.unrealized_pnl === null
                  ? 'unpriced'
                  : formatInr(unrealized.unrealized_pnl, { signed: true, decimals: 2 })}
            </b>
          </span>
          <span>Open positions <b>{openCount}</b></span>
          <span>Fills today <b>{today?.fill_count ?? '—'}</b></span>
          {unrealized && unrealized.positions_unpriced > 0 && (
            <span>Unpriced <b style={{ color: '#fbbf24' }}>{unrealized.positions_unpriced}</b></span>
          )}
        </div>
      </div>
      {today && <Waterfall gross={today.realized_pnl} costs={today.costs_total} />}
    </section>
  )
}
