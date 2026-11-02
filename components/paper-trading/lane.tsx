'use client'

import { useEffect, useMemo, useState } from 'react'
import { ChevronRight, Download } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/error-notice'
import {
  type PaperFill,
  type PaperPosition,
  type PaperSubscription,
  listPaperFills,
  listPaperPositions,
  listPaperSignals,
} from '@/lib/api'
import { downloadText } from '@/lib/download'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'
import {
  KIND_SHORT,
  OPTION_STRATEGY_LABEL,
  ORDERS_PER_STEP,
  costLines,
  describeContract,
  earliestLegExpiry,
  expiryCountdown,
  fillsCsv,
  fillsFilename,
  formatAge,
  formatInr,
  groupFills,
  legsUnrealized,
  openLegs,
  ordersWindow,
  payoffAt,
  payoffSummary,
  signalRows,
  signalStatus,
  subscriptionName,
  tone,
  underlyingSpot,
} from '@/lib/paper-trading'
import { PayoffChart } from './payoff-chart'
import { PriceFreshness } from './price-freshness'

type Tab = 'legs' | 'payoff' | 'signals' | 'costs' | 'settings'

const STATE_CHIP: Record<PaperSubscription['state'], { label: string; className: string }> = {
  active: { label: 'Trading', className: 'pt-chip-ok' },
  awaiting_history: { label: 'Waiting for history', className: 'pt-chip-info' },
  halted_no_data: { label: 'Halted: no data', className: 'pt-chip-warn' },
  inactive: { label: 'Off', className: '' },
}

export function Lane({
  sub,
  expanded,
  onToggle,
  canManage,
  now,
  refreshKey,
  onDeactivate,
  onClose,
  onActivate,
  onEditSettings,
  onBackfill,
}: {
  sub: PaperSubscription
  expanded: boolean
  onToggle: () => void
  canManage: boolean
  now: Date
  refreshKey: number
  onDeactivate: () => void
  onClose: () => void
  onActivate: () => void
  onEditSettings: () => void
  onBackfill: () => void
}) {
  const [tab, setTab] = useState<Tab>('legs')
  const [positions, setPositions] = useState<PaperPosition[] | null>(null)
  const [fills, setFills] = useState<PaperFill[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Positions and fills are only fetched for an open lane, and re-fetched on every poll.
  useEffect(() => {
    if (!expanded) return
    let cancelled = false
    Promise.all([listPaperPositions(sub.id), listPaperFills(sub.id)])
      .then(([p, f]) => {
        if (cancelled) return
        setPositions(p)
        setFills(f)
        setLoadError(null)
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Could not load this subscription')
      })
    return () => {
      cancelled = true
    }
  }, [expanded, sub.id, refreshKey])

  const isOption = sub.instrument_kind === 'option'
  const countdown = expiryCountdown(sub.instrument_kind, sub.contract_expiry ?? earliestLegExpiry(positions ?? []), now)
  const chip = STATE_CHIP[sub.state]
  const tabs: { id: Tab; label: string }[] = [
    { id: 'legs', label: sub.open_legs > 1 ? 'Legs' : 'Position' },
    ...(isOption ? [{ id: 'payoff' as const, label: 'Payoff' }] : []),
    { id: 'signals', label: 'Signals' },
    { id: 'costs', label: 'Costs and fills' },
    { id: 'settings', label: 'Settings' },
  ]
  const bodyId = `pt-lane-${sub.id}`
  const subtitle = [
    sub.instrument_kind === 'equity' ? 'Shares' : sub.contract_symbol ? describeContract(sub.contract_symbol, now) : null,
    sub.lot_size ? `${sub.lots} × ${sub.lot_size}` : null,
    sub.builtin_strategy === 'sma_crossover' ? `${sub.sma_window}-point average` : 'Always long',
  ]
    .filter(Boolean)
    .join('  ·  ')

  return (
    <article id={`lane-${sub.id}`} className="pt-lane" data-state={sub.state} data-unmanaged={!sub.managed} data-testid={`lane-${sub.symbol}`}>
      <button type="button" className="pt-lane-head" aria-expanded={expanded} aria-controls={bodyId} onClick={onToggle}>
        <div>
          <div className="pt-lane-title">
            <h3>{subscriptionName(sub)}</h3>
            <span className="pt-chip">{KIND_SHORT[sub.instrument_kind]}</span>
            <span className={`pt-chip ${chip.className}`}>{chip.label}</span>
            {!sub.managed && <span className="pt-chip pt-chip-crit">Unmanaged</span>}
            {sub.instrument_kind === 'future' && countdown && countdown.phase !== 'ok' && <span className="pt-chip pt-chip-warn">Expires {countdown.label}</span>}
          </div>
          <p className="pt-lane-sub">{subtitle}</p>
        </div>
        <div className="pt-lane-figs">
          <div className="pt-fig">
            <span>OPEN NOW</span>
            <span className={`pt-${sub.open_legs ? tone(sub.unrealized_pnl) : 'flat'}`}>
              {sub.open_legs === 0 ? '—' : sub.unrealized_pnl === null ? 'unpriced' : formatInr(sub.unrealized_pnl, { signed: true })}
            </span>
          </div>
          <div className="pt-fig">
            <span>NET REALIZED</span>
            <span className={`pt-${tone(sub.net_realized_pnl)}`}>{formatInr(sub.net_realized_pnl, { signed: true })}</span>
          </div>
          <div className="pt-fig">
            <span>COSTS</span>
            <span>{formatInr(sub.costs_total)}</span>
          </div>
        </div>
        <ChevronRight className="pt-caret size-4" aria-hidden="true" />
      </button>

      {expanded && (
        <div className="pt-lane-body" id={bodyId}>
          {sub.state_reason && sub.state !== 'active' && sub.is_active && (
            <p className="pt-muted" style={{ marginBottom: '.75rem', fontSize: '.68rem' }}>{sub.state_reason}</p>
          )}
          <PriceFreshness sub={sub} />
          <div className="pt-tabs" role="tablist" aria-label={`${subscriptionName(sub)} details`}>
            {tabs.map((t) => (
              <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
          {loadError && <div className="ui-error" role="alert">{loadError}</div>}
          {tab === 'legs' && <LegsTab sub={sub} positions={positions} now={now} />}
          {tab === 'payoff' && <PayoffTab positions={positions} sub={sub} />}
          {tab === 'signals' && <SignalsTab sub={sub} refreshKey={refreshKey} />}
          {tab === 'costs' && <CostsTab sub={sub} fills={fills} now={now} />}
          {tab === 'settings' && <SettingsTab sub={sub} countdown={countdown} canManage={canManage} onEdit={onEditSettings} />}
          {canManage && (
            <div className="pt-actions">
              {sub.open_legs > 0 && <button type="button" className="button-danger" onClick={onClose}>Close position</button>}
              {sub.is_active && sub.state === 'awaiting_history' && (
                <button type="button" className="button-secondary" onClick={onBackfill}>Fetch price history</button>
              )}
              {sub.is_active ? (
                <button type="button" className="button-secondary" onClick={onDeactivate}>Switch off</button>
              ) : (
                <button type="button" className="button-primary" onClick={onActivate}>Switch on</button>
              )}
            </div>
          )}
        </div>
      )}
    </article>
  )
}

// ---- Tabs ----

function LegsTab({ sub, positions, now }: { sub: PaperSubscription; positions: PaperPosition[] | null; now: Date }) {
  if (positions === null) return <p className="pt-muted">Loading…</p>
  const open = openLegs(positions)
  if (open.length === 0) {
    return <p className="pt-muted" style={{ fontSize: '.72rem' }}>Nothing open. {sub.is_active ? 'It will enter when its signal says so.' : 'It is switched off.'}</p>
  }
  const total = legsUnrealized(positions)
  return (
    <div className="table-scroll">
      <table className="pt-table">
        <thead>
          <tr>
            <th>Contract</th><th>Side</th><th className="pt-r">Qty</th><th className="pt-r">Avg cost</th>
            <th className="pt-r">Mark</th><th className="pt-r">Open P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {open.map((leg) => (
            <tr key={leg.symbol}>
              <td>{describeContract(leg.symbol, now)}<small>{leg.symbol}</small></td>
              <td><span className={`pt-chip ${leg.quantity < 0 ? 'pt-chip-warn' : 'pt-chip-ok'}`}>{leg.quantity < 0 ? 'Sold' : 'Bought'}</span></td>
              <td className="pt-r pt-num">{Math.abs(leg.quantity)}</td>
              <td className="pt-r pt-num">{formatInr(leg.avg_cost, { decimals: 2 })}</td>
              <td className="pt-r pt-num">
                {leg.mark_price === null ? 'never priced' : formatInr(leg.mark_price, { decimals: 2 })}
                {leg.mark_age_seconds !== null && <small>{formatAge(leg.mark_age_seconds)}</small>}
              </td>
              <td className={`pt-r pt-num pt-${tone(leg.unrealized_pnl)}`}>
                {leg.unrealized_pnl === null ? 'unknown' : formatInr(leg.unrealized_pnl, { signed: true, decimals: 2 })}
              </td>
            </tr>
          ))}
        </tbody>
        {open.length > 1 && (
          <tfoot>
            <tr>
              <td colSpan={5} className="pt-r pt-muted">All legs</td>
              <td className={`pt-r pt-num pt-${tone(total)}`}>{total === null ? 'unknown' : formatInr(total, { signed: true, decimals: 2 })}</td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}

function PayoffTab({ positions, sub }: { positions: PaperPosition[] | null; sub: PaperSubscription }) {
  const legs = useMemo(() => openLegs(positions ?? []), [positions])
  const spot = underlyingSpot(sub)
  const summary = useMemo(() => payoffSummary(legs, spot?.price ?? null), [legs, spot?.price])
  if (positions === null) return <p className="pt-muted">Loading…</p>
  if (legs.length === 0) {
    return <p className="pt-muted" style={{ fontSize: '.72rem' }}>The payoff shows once it holds legs. {OPTION_STRATEGY_LABEL[sub.option_strategy ?? 'long_option']} is {sub.option_strategy === 'vertical_spread' ? 'capped on both sides' : 'limited to the premium paid'}.</p>
  }
  if (summary.curve.length === 0) {
    return <p className="pt-muted">The legs are not options of one underlying, so no payoff is drawn.</p>
  }
  return (
    <>
      <PayoffChart summary={summary} spot={spot} />
      {spot && (
        <p className="pt-spot-line">
          {sub.symbol} is at <b className="pt-num">{formatInr(spot.price, { decimals: 2 })}</b>
          <span className="pt-muted"> ({formatAge(spot.ageSeconds)}{spot.stale ? ', an old price' : ''}). Held to expiry from here it would be worth{' '}</span>
          <b className={`pt-num pt-${tone(payoffAt(legs, spot.price))}`}>{formatInr(payoffAt(legs, spot.price), { signed: true })}</b>
          <span className="pt-muted"> before costs.</span>
        </p>
      )}
      <p className="pt-muted" style={{ marginTop: '.5rem', fontSize: '.6rem' }}>
        At expiry, from what each leg cost, before trading costs. Value before expiry also depends on time and volatility, which this does not model.
      </p>
    </>
  )
}

/** What a subscription's daily signals decided, newest first, and whether a position was taken on each. */
function SignalsTab({ sub, refreshKey }: { sub: PaperSubscription; refreshKey: number }) {
  const { data, error, refresh } = useQuery(`signals:${sub.id}:${refreshKey}`, () => listPaperSignals(sub.id))
  if (error && !data) return <ErrorNotice message={`Could not load the signals: ${error}`} onRetry={() => void refresh()} />
  if (!data) return <p className="pt-muted">Loading…</p>
  const rows = signalRows(data)
  if (rows.length === 0) {
    return <p className="pt-muted" style={{ fontSize: '.72rem' }}>The daily signal run has not written a signal for this subscription yet.</p>
  }
  return (
    <>
      <div className="table-scroll">
        <table className="pt-table" aria-label="Daily signals">
          <thead>
            <tr><th>Detected (India)</th><th>Signal</th><th className="pt-r">Reference price</th><th>Position</th></tr>
          </thead>
          <tbody>
            {rows.map((signal) => (
              <tr key={signal.id}>
                <td className="pt-num">{indiaStamp(signal.generated_at)}</td>
                <td><span className={`pt-chip ${signal.signal_type === 'BUY' ? 'pt-chip-ok' : 'pt-chip-warn'}`}>{signal.signal_type === 'BUY' ? 'Buy' : 'Sell'}</span></td>
                <td className="pt-r pt-num">{formatInr(signal.reference_price, { decimals: 2 })}</td>
                <td>{signalStatus(signal)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="pt-muted" style={{ marginTop: '.5rem', fontSize: '.6rem' }}>
        The reference price is the daily bar’s close. The risk gate judges the price of each order against it.
      </p>
    </>
  )
}

function CostsTab({ sub, fills, now }: { sub: PaperSubscription; fills: PaperFill[] | null; now: Date }) {
  const [shown, setShown] = useState(ORDERS_PER_STEP)
  const lines = useMemo(() => costLines(fills ?? []), [fills])
  const orders = useMemo(() => groupFills(fills ?? []), [fills])
  if (fills === null) return <p className="pt-muted">Loading…</p>
  const largest = lines[0]?.amount ?? 0
  const window = ordersWindow(orders.length, shown)
  return (
    <div className="pt-costs">
      <div>
        <div className="pt-term-label" style={{ marginBottom: '.5rem' }}>WHERE THE COSTS WENT · {formatInr(sub.costs_total, { decimals: 2 })}</div>
        {lines.length === 0 ? (
          <p className="pt-muted" style={{ fontSize: '.68rem' }}>No fills have been charged yet.</p>
        ) : (
          <div className="pt-cost-lines">
            {lines.map((line) => (
              <div className="pt-cost-line" key={line.label}>
                <span>{line.label}</span>
                <div className="pt-cost-bar"><i style={{ width: `${(line.amount / largest) * 100}%` }} /></div>
                <span className="pt-num">{formatInr(line.amount, { decimals: 2 })}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div>
        <div className="pt-orders-head">
          <div className="pt-term-label">ORDERS · {orders.length}</div>
          {orders.length > 0 && (
            <button type="button" className="button-secondary" onClick={() => downloadText(fillsFilename(sub.symbol, new Date()), fillsCsv(fills))}>
              <Download className="size-3" aria-hidden="true" />Export all fills (CSV)
            </button>
          )}
        </div>
        {orders.length === 0 ? (
          <p className="pt-muted" style={{ fontSize: '.68rem' }}>No orders yet.</p>
        ) : (
          <div className="pt-orders">
            {orders.slice(0, window.visible).map((order) => (
              <div className="pt-order" key={order.orderGroupId}>
                <header>
                  <span>
                    {indiaStamp(order.createdAt)}
                    {order.fills.some((fill) => fill.administrative) && (
                      <span className="pt-chip pt-chip-info" style={{ marginLeft: '.5rem' }} title="Closed by hand without the risk gate: not a trade the strategy made">manual close</span>
                    )}
                  </span>
                  <span>
                    <span className={`pt-${tone(order.realizedPnl)}`} title="The price result before costs">{formatInr(order.realizedPnl, { signed: true, decimals: 2 })}</span>
                    {' '}before costs · costs {formatInr(order.costsTotal, { decimals: 2 })}
                  </span>
                </header>
                <ul>
                  {order.fills.map((fill) => (
                    <li key={fill.id}>
                      <span>
                        {fill.side === 'buy' ? 'Bought' : 'Sold'} {fill.filled_quantity}
                        {fill.filled_quantity !== fill.requested_quantity ? ` of ${fill.requested_quantity}` : ''} {describeContract(fill.symbol, now)}
                      </span>
                      <span className="pt-num">
                        {fill.avg_fill_price === null ? '—' : `@ ${formatInr(fill.avg_fill_price, { decimals: 2 })}`}
                        {fill.data_source !== 'real' && <span className="pt-chip pt-chip-warn" style={{ marginLeft: '.5rem' }}>{fill.data_source}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {window.remaining > 0 && (
              <div className="pt-actions">
                <button type="button" className="button-secondary" onClick={() => setShown(window.next)}>
                  Show {Math.min(ORDERS_PER_STEP, window.remaining)} more ({window.remaining} older)
                </button>
                <button type="button" className="button-secondary" onClick={() => setShown(orders.length)}>Show all {orders.length}</button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function SettingsTab({
  sub,
  countdown,
  canManage,
  onEdit,
}: {
  sub: PaperSubscription
  countdown: ReturnType<typeof expiryCountdown>
  canManage: boolean
  onEdit: () => void
}) {
  const rows: [string, string][] = [
    ['Capital', formatInr(sub.initial_capital)],
    ['Stop-loss', `${sub.stop_loss_pct}%${sub.instrument_kind === 'option' ? ' of the premium' : ''}`],
    ['Signal', sub.builtin_strategy === 'sma_crossover' ? `Moving average, ${sub.sma_window} points` : 'Always long'],
    ...(sub.instrument_kind === 'equity' ? [['Size per entry', `${sub.position_size_pct}% of capital`] as [string, string]] : []),
    ...(sub.instrument_kind !== 'equity' ? [['Lots', `${sub.lots}${sub.lot_size ? ` × ${sub.lot_size}` : ''}`] as [string, string]] : []),
    ...(sub.option_strategy === 'vertical_spread' ? [['Hedge distance', `${sub.spread_width} strikes`] as [string, string]] : []),
    ...(sub.contract_symbol ? [['Contract', sub.contract_symbol] as [string, string]] : []),
    ...(countdown ? [['Expiry', `${sub.contract_expiry} (${countdown.label}). ${countdown.note}.`] as [string, string]] : []),
    ['Added', `${new Date(sub.created_at).toLocaleString()}${sub.created_by ? ` by ${sub.created_by}` : ''}`],
    ...(sub.deactivated_at
      ? [['Switched off', `${new Date(sub.deactivated_at).toLocaleString()}${sub.deactivated_by ? ` by ${sub.deactivated_by}` : ''}${sub.deactivation_reason ? `: ${sub.deactivation_reason}` : ''}`] as [string, string]]
      : []),
  ]
  return (
    <>
      <dl style={{ display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '.45rem 1.5rem', fontSize: '.68rem' }}>
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: 'contents' }}>
            <dt className="pt-muted">{label}</dt>
            <dd style={{ color: '#e2e8f0', overflowWrap: 'anywhere' }}>{value}</dd>
          </div>
        ))}
      </dl>
      {canManage && (
        <div className="pt-actions">
          <button type="button" className="button-secondary" onClick={onEdit}>Edit settings</button>
        </div>
      )}
    </>
  )
}
