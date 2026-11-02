'use client'

import { Fragment, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Pager } from '@/components/ui/pager'
import { ErrorNotice } from '@/components/ui/error-notice'
import { type LiveOrderDto, type PositionByStrategy, type StrategySummary, listLiveOrderTrades, listStrategyLiveOrders } from '@/lib/api'
import { PNL_CAPTION, failureLine, formatPnl, formatRupees, positionNote, sourceNote, statusLabel, statusTone } from '@/lib/orders'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'
import { Panel } from './panel'

const SIZE = 10

/** The attempts on one order: each is a fill or a try, with the price it was placed at and where that came from. */
function OrderTrades({ order }: { order: LiveOrderDto }) {
  const { data, error, refresh } = useQuery(`trades:${order.id}`, () => listLiveOrderTrades(order.id))
  if (error && !data) return <ErrorNotice className="m-2" message={`Could not load the trades: ${error}`} onRetry={() => void refresh()} />
  if (!data) return <p className="p-3 text-xs text-muted-foreground">Loading the trades…</p>
  if (data.length === 0) return <p className="p-3 text-xs text-muted-foreground">No trade was recorded against this order.</p>
  return (
    <table className="orders-table order-trades" aria-label={`Trades of order ${order.id}`}>
      <thead>
        <tr>
          <th>TIME (INDIA)</th>
          <th>QTY</th>
          <th>PLACED AT</th>
          <th>STATUS</th>
          <th>PRICE FROM</th>
        </tr>
      </thead>
      <tbody>
        {data.map((trade) => {
          const source = sourceNote(trade.data_source)
          return (
            <tr key={trade.id}>
              <td className="mono">{indiaStamp(trade.executed_at)}</td>
              <td>{trade.quantity}</td>
              <td className="mono" title="The quote the order was placed at; a broker-confirmed fill price is shown in the ledger">
                {formatRupees(trade.price)}
              </td>
              <td>
                <span className={`status-badge status-${trade.status.toLowerCase()}`} data-tone={statusTone(trade.status)}>
                  {statusLabel(trade.status)}
                </span>
              </td>
              <td>
                <span className="source-chip" data-caution={source.caution} title={source.explanation}>
                  {source.label}
                </span>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/** What one strategy holds and what it has sent to a broker, with the broker's own order id or the reason it failed. */
export function StrategyLive({ strategies, positions }: { strategies: StrategySummary[]; positions: PositionByStrategy[] }) {
  const [strategyId, setStrategyId] = useState('')
  const [page, setPage] = useState(1)
  const [open, setOpen] = useState<string | null>(null)

  const { data, error, loading, refresh } = useQuery(
    `live-orders:${strategyId}:${page}`,
    () => listStrategyLiveOrders(strategyId, { limit: SIZE, offset: (page - 1) * SIZE }),
    { enabled: strategyId !== '' },
  )
  const held = positions.filter((p) => p.strategy_id === strategyId)
  const orders = strategyId ? (data?.items ?? []) : []

  return (
    <Panel title="Live by strategy" eyebrow="WHAT A STRATEGY HOLDS AND WHAT IT SENT TO A BROKER">
      <div className="orders-filters">
        <select
          aria-label="Strategy to inspect"
          value={strategyId}
          onChange={(e) => {
            setStrategyId(e.target.value)
            setPage(1)
            setOpen(null)
          }}
        >
          <option value="">Pick a strategy…</option>
          {strategies.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      </div>
      {!strategyId ? (
        <p className="p-4 text-xs text-muted-foreground">Pick a strategy to see its positions and every order it has sent to a broker.</p>
      ) : (
        <>
          <div className="position-list" aria-label="Positions">
            {held.length === 0 && <p className="p-4 text-xs text-muted-foreground">This strategy holds no open position, live or paper.</p>}
            {held.map((p) => (
              <div className="position-row" key={`${p.mode}-${p.symbol}`}>
                <div className="min-w-0">
                  <strong>{p.symbol}</strong>
                  <small>
                    {p.mode.toUpperCase()} · avg {formatRupees(p.avg_cost)}
                  </small>
                  {positionNote(p) && <small className="position-warning">{positionNote(p)}</small>}
                </div>
                <div className="text-right">
                  <span className={p.quantity < 0 ? 'sell-badge' : 'buy-badge'}>
                    {p.quantity > 0 ? '+' : ''}
                    {p.quantity}
                  </span>
                  <small className="block text-muted-foreground" title={PNL_CAPTION}>
                    {formatPnl(p.realized_pnl)} <em>before costs</em>
                  </small>
                </div>
              </div>
            ))}
          </div>
          {error && <ErrorNotice className="m-3" message={`Could not load the orders: ${error}`} onRetry={() => void refresh()} />}
          <div className="orders-table-wrap" aria-busy={loading}>
            <table className="orders-table" aria-label="Live orders">
              <thead>
                <tr>
                  <th></th>
                  <th>SUBMITTED (INDIA)</th>
                  <th>SYMBOL</th>
                  <th>SIDE</th>
                  <th>QTY</th>
                  <th>BROKER</th>
                  <th>BROKER ORDER ID</th>
                  <th>STATUS</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((order) => {
                  const isOpen = open === order.id
                  const failure = failureLine(order)
                  return (
                    <Fragment key={order.id}>
                      <tr>
                        <td>
                          <button type="button" className="audit-expand-btn" aria-expanded={isOpen} aria-label={`${isOpen ? 'Hide' : 'Show'} trades of order ${order.broker_order_id ?? order.id.slice(0, 8)}`} onClick={() => setOpen(isOpen ? null : order.id)}>
                            {isOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                          </button>
                        </td>
                        <td className="mono">{indiaStamp(order.submitted_at)}</td>
                        <td>{order.symbol}</td>
                        <td>{order.side.toUpperCase()}</td>
                        <td>{order.quantity}</td>
                        <td>{order.broker_name}</td>
                        <td className="mono">{order.broker_order_id ?? '—'}</td>
                        <td>
                          <span className={`status-badge status-${order.status.toLowerCase()}`} data-tone={statusTone(order.status)}>
                            {statusLabel(order.status)}
                          </span>
                        </td>
                      </tr>
                      {failure && (
                        <tr className="order-failure-row">
                          <td colSpan={8} role="alert">
                            <strong>Failed:</strong> {failure}
                          </td>
                        </tr>
                      )}
                      {isOpen && (
                        <tr className="order-trades-row">
                          <td colSpan={8}>
                            <OrderTrades order={order} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
                {orders.length === 0 && (
                  <tr>
                    <td colSpan={8} className="p-4 text-center text-xs text-muted-foreground">
                      {loading ? 'Loading the orders…' : 'This strategy has not sent an order to a broker.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <Pager total={data?.total ?? 0} page={page} size={SIZE} noun="orders" onPage={(next) => { setPage(next); setOpen(null) }} />
        </>
      )}
    </Panel>
  )
}
