'use client'

import { useState } from 'react'
import { useAuth } from '@/components/auth/auth-provider'
import { IntentsPanel } from '@/components/orders/intents-panel'
import { ExecutionLedger } from '@/components/orders/execution-ledger'
import { Panel } from '@/components/orders/panel'
import { PreTradeCheck } from '@/components/orders/pre-trade-check'
import { StrategyLive } from '@/components/orders/strategy-live'
import { ErrorNotice } from '@/components/ui/error-notice'
import { TabList } from '@/components/ui/tab-list'
import { getSystemVitals, listBrokerCircuitBreakerStatus, listLiveIntents, listOrders, listPositionsByStrategy, listStrategies } from '@/lib/api'
import { PNL_CAPTION, formatPnl, formatRupees, positionKey, positionNote } from '@/lib/orders'
import { can } from '@/lib/permissions'
import { useQuery } from '@/lib/use-query'

type Tab = 'orders' | 'strategy' | 'intents'
const REFRESH_MS = 10_000

export default function OrdersPage() {
  const { role } = useAuth()
  const canTrade = can(role, 'POST /api/v1/risk/order-intents')
  const [tab, setTab] = useState<Tab>('orders')

  // The header numbers each ask the server for a count (a page of one row and its total), so they stay
  // right however long the ledger grows.
  const paper = useQuery('count:paper', () => listOrders({ mode: 'paper', limit: 1 }), { intervalMs: REFRESH_MS })
  const live = useQuery('count:live', () => listOrders({ mode: 'live', limit: 1 }), { intervalMs: REFRESH_MS })
  const capped = useQuery('count:capped', () => listLiveIntents({ statusFilter: 'capped', limit: 1 }), { intervalMs: REFRESH_MS })
  const allIntents = useQuery('count:intents', () => listLiveIntents({ limit: 1 }), { intervalMs: REFRESH_MS })
  const positionsQuery = useQuery('positions', listPositionsByStrategy, { intervalMs: REFRESH_MS })
  const vitalsQuery = useQuery('vitals', getSystemVitals, { intervalMs: REFRESH_MS })
  const breakersQuery = useQuery('breakers', listBrokerCircuitBreakerStatus, { intervalMs: REFRESH_MS })
  const strategiesQuery = useQuery('strategies', listStrategies)

  const positions = positionsQuery.data ?? []
  const strategies = strategiesQuery.data ?? []
  const vitals = vitalsQuery.error ? null : vitalsQuery.data
  const breakers = breakersQuery.error ? null : breakersQuery.data
  const paperTotal = paper.data?.total ?? 0
  const liveTotal = live.data?.total ?? 0
  const loadError = [paper, live, capped, positionsQuery, strategiesQuery].find((q) => q.error)?.error ?? null

  const tabs: { id: Tab; label: string; count: number | null; amber?: boolean }[] = [
    { id: 'orders', label: 'Orders & Trades', count: paper.data && live.data ? paperTotal + liveTotal : null },
    { id: 'strategy', label: 'Live by strategy', count: null },
    { id: 'intents', label: 'Live Order Intents', count: allIntents.data?.total ?? null, amber: true },
  ]

  return (
    <main className="orders-page mx-auto w-full max-w-[1700px] pb-10">
      <header className="orders-header">
        <div>
          <p className="eyebrow">TRADINGOS // EXECUTION CONTROL</p>
          <h1>Orders &amp; Trades</h1>
          <p>Unified paper and live execution ledger</p>
        </div>
      </header>
      {loadError && (
        <ErrorNotice
          className="mb-4"
          message={`Could not load part of this page: ${loadError}`}
          onRetry={() => {
            for (const q of [paper, live, capped, positionsQuery, strategiesQuery]) void q.refresh()
          }}
        />
      )}
      <section className="orders-summary">
        <div className="summary-stat">
          <span>TOTAL EXECUTIONS</span>
          <strong>{paper.data && live.data ? (paperTotal + liveTotal).toLocaleString('en-IN') : '—'}</strong>
          <small>
            {paperTotal.toLocaleString('en-IN')} paper · {liveTotal.toLocaleString('en-IN')} live
          </small>
        </div>
        <div className="summary-stat">
          <span>CAPPED INTENTS</span>
          <strong>{capped.data ? capped.data.total.toLocaleString('en-IN') : '—'}</strong>
          <small>live order intents blocked by the standing rate/notional cap</small>
        </div>
        <div className="summary-stat">
          <span>ORDER DISPATCH</span>
          <strong className="text-muted-foreground">
            {vitals === null || vitals === undefined
              ? '—'
              : vitals.latency.order_dispatch_p50_ms === null
                ? 'no orders yet'
                : `${vitals.latency.order_dispatch_p50_ms.toFixed(0)} / ${vitals.latency.order_dispatch_p95_ms?.toFixed(0) ?? '—'} ms`}
          </strong>
          <small>
            median / 95th percentile{vitals ? ` · budget ${vitals.latency.budget_ms.toFixed(0)} ms` : ''}
          </small>
        </div>
        <div className="summary-stat">
          <span>OPEN POSITIONS</span>
          <strong>{positions.length}</strong>
          <small>
            {positions.filter((p) => p.mode === 'live').length} live · {positions.filter((p) => p.mode === 'paper').length} paper
          </small>
        </div>
      </section>
      <div className="orders-layout">
        <div className="orders-main">
          <TabList label="Orders views" className="orders-tabs">
            {tabs.map((t) => (
              <button key={t.id} role="tab" aria-selected={tab === t.id} tabIndex={tab === t.id ? 0 : -1} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>
                {t.label} {t.count !== null && <b className={t.amber ? 'amber-count' : undefined}>{t.count.toLocaleString('en-IN')}</b>}
              </button>
            ))}
          </TabList>
          {tab === 'orders' && <ExecutionLedger strategies={strategies} />}
          {tab === 'strategy' && <StrategyLive strategies={strategies} positions={positions} />}
          {tab === 'intents' && <IntentsPanel strategies={strategies} />}
        </div>
        <aside className="orders-side">
          {canTrade ? (
            <PreTradeCheck />
          ) : (
            <Panel title="Pre-trade check" eyebrow="READ-ONLY">
              <p className="p-4 text-xs text-muted-foreground">Your role does not have access to the pre-trade check.</p>
            </Panel>
          )}
          <Panel title="Positions by strategy" eyebrow="LIVE + PAPER · CURRENT NET EXPOSURE">
            {positions.length === 0 ? (
              <p className="p-4 text-xs text-muted-foreground">No open positions — every position closes out to flat.</p>
            ) : (
              <div className="position-list">
                {positions.map((p) => (
                  <div className="position-row" key={positionKey(p)}>
                    <div className="min-w-0">
                      <strong>{p.strategy_name}</strong>
                      <small>
                        {p.symbol} · {p.mode.toUpperCase()}
                      </small>
                      {positionNote(p) && <small className="position-warning">{positionNote(p)}</small>}
                    </div>
                    <div className="text-right">
                      <span className={p.quantity < 0 ? 'sell-badge' : 'buy-badge'}>
                        {p.quantity > 0 ? '+' : ''}
                        {p.quantity}
                      </span>
                      <small className="block text-muted-foreground">avg {formatRupees(p.avg_cost)}</small>
                      <small className="block text-muted-foreground" title={PNL_CAPTION}>
                        {formatPnl(p.realized_pnl)} <em>before costs</em>
                      </small>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>
          <Panel title="Broker gateways" eyebrow="CIRCUIT BREAKERS">
            {breakers === null || breakers === undefined ? (
              <p className="p-4 text-xs text-muted-foreground">Could not read the broker circuit breakers.</p>
            ) : breakers.length === 0 ? (
              <p className="p-4 text-xs text-muted-foreground">No broker is configured.</p>
            ) : (
              <div className="position-list">
                {breakers.map((b) => (
                  <div className="position-row" key={b.broker}>
                    <div className="min-w-0">
                      <strong>{b.broker}</strong>
                      <small>
                        {b.consecutive_failures} of {b.failure_threshold} failures
                        {b.cooldown_remaining_seconds ? ` · retry in ${Math.ceil(b.cooldown_remaining_seconds)}s` : ''}
                      </small>
                    </div>
                    <div className="text-right">
                      <strong className={b.state === 'open' ? 'text-rose-300' : 'text-emerald-300'}>{b.state === 'open' ? 'OPEN (blocking)' : 'CLOSED'}</strong>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </aside>
      </div>
    </main>
  )
}
