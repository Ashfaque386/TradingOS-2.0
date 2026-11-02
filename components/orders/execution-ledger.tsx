'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { ArrowDown, ArrowUp, Search } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { Pager } from '@/components/ui/pager'
import { ErrorNotice } from '@/components/ui/error-notice'
import { type StrategySummary, listOrders } from '@/lib/api'
import {
  EXECUTION_STATUSES,
  NO_ORDERS_FILTER,
  type OrdersFilter,
  formatRupees,
  ordersFiltered,
  ordersQuery,
  sourceNote,
  statusLabel,
  statusTone,
} from '@/lib/orders'
import { can } from '@/lib/permissions'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'
import { Panel } from './panel'

const PAGE_SIZES = [25, 50, 100]
const LIVE_REFRESH_MS = 10_000

/** Every paper fill and live trade, newest first, paged and filtered by the server. */
export function ExecutionLedger({ strategies }: { strategies: StrategySummary[] }) {
  const { role } = useAuth()
  const canReadAudit = can(role, 'GET /api/v1/audit/entries')
  const [filter, setFilter] = useState<OrdersFilter>(NO_ORDERS_FILTER)
  const [typedSymbol, setTypedSymbol] = useState('')
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(PAGE_SIZES[0])

  // The symbol box applies a moment after typing stops, so each keystroke is not a request.
  useEffect(() => {
    const id = setTimeout(() => {
      setFilter((current) => (current.symbol === typedSymbol ? current : { ...current, symbol: typedSymbol }))
      setPage(1)
    }, 300)
    return () => clearTimeout(id)
  }, [typedSymbol])

  const query = ordersQuery(filter, page, size)
  const { data, error, loading, refresh } = useQuery(JSON.stringify(query), () => listOrders(query), {
    intervalMs: page === 1 ? LIVE_REFRESH_MS : undefined,
  })
  const rows = data?.items ?? []
  const total = data?.total ?? 0
  const filtered = ordersFiltered(filter)

  function change(patch: Partial<OrdersFilter>) {
    setFilter((current) => ({ ...current, ...patch }))
    setPage(1)
  }

  function clear() {
    setFilter(NO_ORDERS_FILTER)
    setTypedSymbol('')
    setPage(1)
  }

  return (
    <Panel title="Execution ledger" eyebrow="PAPER + LIVE · NEWEST FIRST">
      <div className="orders-filters">
        <div className="filter-chips" role="group" aria-label="Mode">
          {(['both', 'paper', 'live'] as const).map((mode) => (
            <button key={mode} type="button" className={filter.mode === mode ? 'active' : ''} aria-pressed={filter.mode === mode} onClick={() => change({ mode })}>
              {mode.toUpperCase()}
            </button>
          ))}
        </div>
        <select aria-label="Status" value={filter.status} onChange={(e) => change({ status: e.target.value })}>
          <option value="">All statuses</option>
          {EXECUTION_STATUSES.map((status) => (
            <option key={status} value={status}>
              {statusLabel(status)}
            </option>
          ))}
        </select>
        <select aria-label="Strategy" value={filter.strategyId} onChange={(e) => change({ strategyId: e.target.value })}>
          <option value="">All strategies</option>
          {strategies.map((strategy) => (
            <option key={strategy.id} value={strategy.id}>
              {strategy.name}
            </option>
          ))}
        </select>
        <label className="symbol-search">
          <Search className="size-3" />
          <input placeholder="Search symbol..." aria-label="Search symbol" value={typedSymbol} onChange={(e) => setTypedSymbol(e.target.value)} />
        </label>
        <select aria-label="Rows per page" value={size} onChange={(e) => { setSize(Number(e.target.value)); setPage(1) }}>
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n} per page
            </option>
          ))}
        </select>
        {filtered && (
          <button type="button" className="button-secondary" onClick={clear}>
            Clear filters
          </button>
        )}
      </div>
      {error && <ErrorNotice className="m-3" message={`Could not load the ledger: ${error}`} onRetry={() => void refresh()} />}
      <div className="orders-table-wrap" aria-busy={loading}>
        <table className="orders-table">
          <thead>
            <tr>
              <th>TIME (INDIA)</th>
              <th>SYMBOL</th>
              <th>SIDE</th>
              <th>QTY</th>
              <th>PRICE</th>
              <th>STATUS</th>
              <th>PRICE FROM</th>
              <th>COSTS</th>
              <th>STRATEGY</th>
              {canReadAudit && <th>AUDIT</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const source = sourceNote(row.data_source)
              const buy = row.side.toLowerCase() === 'buy'
              return (
                <tr key={`${row.mode}-${row.id}`}>
                  <td className="mono">
                    {indiaStamp(row.time)}
                    <small>{row.mode.toUpperCase()}</small>
                  </td>
                  <td>{row.symbol}</td>
                  <td>
                    <span className={buy ? 'buy-badge' : 'sell-badge'}>
                      {buy ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />}
                      {row.side.toUpperCase()}
                    </span>
                  </td>
                  <td>{row.quantity}</td>
                  <td className="mono">{formatRupees(row.price)}</td>
                  <td>
                    <span className={`status-badge status-${row.status.toLowerCase()}`} data-tone={statusTone(row.status)}>
                      {statusLabel(row.status)}
                    </span>
                  </td>
                  <td>
                    <span className="source-chip" data-caution={source.caution} title={source.explanation}>
                      {source.label}
                    </span>
                  </td>
                  <td className="mono">{row.costs === null ? '—' : formatRupees(row.costs)}</td>
                  <td className="source-cell">
                    {row.strategy_name ?? '—'}
                    {row.administrative && <small title="Closed by hand without the risk gate: not a trade the strategy made">manual close</small>}
                  </td>
                  {canReadAudit && (
                    <td>
                      {/* A live trade is audited under its own id; a paper fill is not, so it has no trail to open. */}
                      {row.mode === 'live' ? (
                        <Link className="audit-link" href={`/audit?entity_id=${encodeURIComponent(row.id)}`} aria-label={`Audit trail of ${row.side} ${row.symbol}`}>
                          Trail
                        </Link>
                      ) : (
                        '—'
                      )}
                    </td>
                  )}
                </tr>
              )
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={canReadAudit ? 10 : 9} className="p-4 text-center text-xs text-muted-foreground">
                  {loading ? 'Loading the ledger…' : filtered ? 'No executions match these filters.' : 'Nothing has been executed yet.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager
        total={total}
        page={page}
        size={size}
        noun="executions"
        onPage={setPage}
        note={page === 1 ? 'Refreshes every 10 seconds' : 'Not refreshing on this page, so the rows stay put'}
      />
    </Panel>
  )
}
