'use client'

import Link from 'next/link'
import { useMemo } from 'react'
import { Zap } from 'lucide-react'
import { type Strategy, listLiveSubscriptions } from '@/lib/api'
import { liveSubscriptionRows } from '@/lib/live-limits'
import { ErrorNotice } from '@/components/ui/error-notice'
import { useQuery } from '@/lib/use-query'

/** Every live subscription on one screen: which are trading on their own with real money, which have
 * autonomy off, which are switched off, and the limits each carries. Each opens its strategy's lifecycle
 * tab, where autonomy and the limits are changed. */
export function LiveSubscriptions({ strategies }: { strategies: Strategy[] }) {
  const { data, error, refresh } = useQuery('live-subscriptions', listLiveSubscriptions, { intervalMs: 15_000 })
  const rows = useMemo(() => liveSubscriptionRows(data ?? [], strategies), [data, strategies])
  const autonomous = rows.filter((r) => r.state === 'autonomous').length

  if (error && !data) return <ErrorNotice className="mt-5" message={`Could not load the live subscriptions: ${error}`} onRetry={() => void refresh()} />
  if (!data || rows.length === 0) return null

  return (
    <section className="live-subs mt-5" aria-label="Live subscriptions">
      <div className="live-subs-head">
        <div>
          <p className="eyebrow">LIVE SUBSCRIPTIONS · {rows.length}</p>
          <h2>{autonomous === 0 ? 'Nothing is trading on its own' : `${autonomous} ${autonomous === 1 ? 'is' : 'are'} trading with real money on their own`}</h2>
        </div>
        <Zap className={`size-4 ${autonomous > 0 ? 'text-emerald-300' : 'text-slate-500'}`} aria-hidden="true" />
      </div>
      {error && <ErrorNotice className="m-3" message={`The list may be out of date: ${error}`} onRetry={() => void refresh()} />}
      <div className="overflow-x-auto">
        <table className="sv-table" aria-label="Live subscriptions list">
          <thead>
            <tr><th>Strategy</th><th>Symbol</th><th>Broker</th><th>State</th><th>Limits</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td><Link className="audit-link" href={`/strategies/${row.strategyId}?tab=Lifecycle`}>{row.strategyName}</Link></td>
                <td>{row.symbol}</td>
                <td>{row.broker}</td>
                <td>
                  <span className="live-state" data-state={row.state}>{row.stateLabel}</span>
                  {row.autonomyNote && <small className="sv-muted"> {row.autonomyNote}</small>}
                </td>
                <td>{row.limits}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
