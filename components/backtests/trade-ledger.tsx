'use client'

import { useState } from 'react'
import { Pager } from '@/components/ui/pager'
import type { BacktestRun } from '@/lib/api'
import { LEDGER_PAGE, holdingDays, ledgerTrades, summariseTrades } from '@/lib/backtests'
import { formatPnl, formatRupees } from '@/lib/orders'

/** Every trade of a run, with when it opened and closed, what it cost and what it earned, a page at a
 * time. A run from before dates were kept shows only its results, and says so. */
export function TradeLedger({ run }: { run: BacktestRun }) {
  const [page, setPage] = useState(1)
  const { dated, resultsOnly } = ledgerTrades(run)
  const rows = dated ?? []
  const summary = summariseTrades(rows)
  const total = dated ? dated.length : resultsOnly.length

  if (total === 0) return <p className="p-4 text-xs text-muted-foreground">No trades recorded.</p>

  const from = (page - 1) * LEDGER_PAGE
  return (
    <>
      {dated ? (
        <p className="bt-ledger-summary">
          {summary.count} trades · {summary.wins} won · {summary.losses} lost · net <b className={summary.net < 0 ? 'loss' : 'gain'}>{formatPnl(summary.net)}</b> after {formatRupees(summary.costs)} of costs
          {summary.best && summary.worst ? ` · best ${formatPnl(summary.best.net_pnl)} · worst ${formatPnl(summary.worst.net_pnl)}` : ''}
          {summary.averageDays !== null ? ` · held ${summary.averageDays.toFixed(0)} days on average` : ''}
        </p>
      ) : (
        <p className="bt-ledger-summary">Dates were not recorded for this run, only each trade’s net result.</p>
      )}
      <div className="overflow-x-auto">
        <table className="backtest-table" aria-label="Trades">
          {dated ? (
            <>
              <thead>
                <tr>
                  <th>#</th><th>Opened</th><th>Closed</th><th>Days</th><th>Shares</th><th>Bought at</th><th>Sold at</th><th>Costs</th><th>Net P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {dated.slice(from, from + LEDGER_PAGE).map((trade, i) => (
                  <tr key={from + i}>
                    <td>{from + i + 1}</td>
                    <td className="mono">{trade.entry_date}</td>
                    <td className="mono">{trade.exit_date}</td>
                    <td>{holdingDays(trade)}</td>
                    <td>{trade.quantity.toLocaleString('en-IN')}</td>
                    <td className="mono">{formatRupees(trade.entry_price)}</td>
                    <td className="mono">{formatRupees(trade.exit_price)}</td>
                    <td className="mono">{formatRupees(trade.costs)}</td>
                    <td className={trade.net_pnl < 0 ? 'loss' : 'gain'}>{formatPnl(trade.net_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </>
          ) : (
            <>
              <thead>
                <tr><th>#</th><th>Net P&amp;L</th></tr>
              </thead>
              <tbody>
                {resultsOnly.slice(from, from + LEDGER_PAGE).map((pnl, i) => (
                  <tr key={from + i}>
                    <td>{from + i + 1}</td>
                    <td className={pnl < 0 ? 'loss' : 'gain'}>{formatPnl(pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </>
          )}
        </table>
      </div>
      <Pager total={total} page={page} size={LEDGER_PAGE} noun="trades" onPage={setPage} />
    </>
  )
}
