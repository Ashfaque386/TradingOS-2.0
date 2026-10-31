'use client'

import { FormEvent, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Check, ChevronLeft, ChevronRight, Clock3, Search, ShieldCheck, Timer, X } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { ErrorNotice } from '@/components/ui/error-notice'
import {
  type BrokerCircuitBreakerStatus,
  type LiveOrderIntentDto,
  type OrderIntentResult,
  type PositionByStrategy,
  type SystemVitals,
  type UnifiedExecution,
  getSystemVitals,
  listBrokerCircuitBreakerStatus,
  listLiveIntents,
  listOrders,
  listPositionsByStrategy,
  submitOrderIntent,
} from '@/lib/api'
import { messageOf } from '@/lib/errors'

function Panel({ title, eyebrow, children }: { title: string; eyebrow?: string; children: React.ReactNode }) {
  return <section className="orders-panel"><div className="orders-panel-head"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div></div>{children}</section>
}

export default function OrdersPage() {
  const { role } = useAuth()
  const canTrade = role === 'SystemAdministrator' || role === 'PortfolioManager'
  const [orders, setOrders] = useState<UnifiedExecution[]>([])
  const [intents, setIntents] = useState<LiveOrderIntentDto[]>([])
  const [positions, setPositions] = useState<PositionByStrategy[]>([])
  const [mode, setMode] = useState('BOTH')
  const [status, setStatus] = useState('ALL')
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState('orders')
  const [page, setPage] = useState(1)
  const [live, setLive] = useState(false)
  // No pre-filled numbers: a default portfolio value or price would be checked as if it were real.
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('')
  const [price, setPrice] = useState('')
  const [referencePrice, setReferencePrice] = useState('')
  const [portfolioValue, setPortfolioValue] = useState('')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [checkResult, setCheckResult] = useState<OrderIntentResult | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [vitals, setVitals] = useState<SystemVitals | null>(null)
  const [breakers, setBreakers] = useState<BrokerCircuitBreakerStatus[] | null>(null)

  async function reload() {
    const [ordersRes, intentsRes, positionsRes, vitalsRes, breakersRes] = await Promise.allSettled([
      listOrders('both'),
      listLiveIntents(),
      listPositionsByStrategy(),
      getSystemVitals(),
      listBrokerCircuitBreakerStatus(),
    ])
    if (ordersRes.status === 'fulfilled') setOrders(ordersRes.value)
    if (intentsRes.status === 'fulfilled') setIntents(intentsRes.value)
    if (positionsRes.status === 'fulfilled') setPositions(positionsRes.value)
    setVitals(vitalsRes.status === 'fulfilled' ? vitalsRes.value : null)
    setBreakers(breakersRes.status === 'fulfilled' ? breakersRes.value : null)
    const failed = [ordersRes, intentsRes, positionsRes].find((r) => r.status === 'rejected')
    setLoadError(failed && failed.status === 'rejected' ? messageOf(failed.reason, 'Could not load the ledger') : null)
  }

  useEffect(() => {
    reload()
    const interval = setInterval(reload, 10000)
    return () => clearInterval(interval)
  }, [])

  const filtered = useMemo(
    () => orders.filter(o => (mode === 'BOTH' || o.mode === mode.toLowerCase()) && (status === 'ALL' || o.status.toUpperCase() === status) && o.symbol.toLowerCase().includes(query.toLowerCase())),
    [orders, mode, status, query],
  )
  // Offer the statuses that exist (paper: filled/partial; live: pending_confirmation/filled/rejected/
  // cancelled/failed), not a fixed list that includes ones that never occur and omits ones that do.
  const statusOptions = useMemo(
    () => ['ALL', ...Array.from(new Set(orders.map(o => o.status.toUpperCase()))).sort()],
    [orders],
  )
  useEffect(() => { setPage(1) }, [mode, status, query])
  const paperCount = orders.filter(o => o.mode === 'paper').length
  const liveCount = orders.filter(o => o.mode === 'live').length

  // Is every number the check needs a real, positive one?
  const inputsReady =
    symbol.trim() !== '' && [qty, price, referencePrice, portfolioValue].every((v) => Number(v) > 0)

  // A dry run: the risk gate judges the order and nothing is sent anywhere.
  async function runCheck() {
    if (!canTrade || !inputsReady) return
    setSubmitting(true)
    setSubmitError(null)
    setCheckResult(null)
    try {
      const proposedPrice = Number(price)
      const quantity = Number(qty)
      setCheckResult(
        await submitOrderIntent({
          mode: live ? 'live' : 'paper',
          symbol: symbol.trim(),
          side,
          quantity,
          proposed_price: proposedPrice,
          reference_price: Number(referencePrice),
          proposed_position_value: proposedPrice * quantity,
          portfolio_value: Number(portfolioValue),
        }),
      )
    } catch (err) {
      setSubmitError(messageOf(err, 'The pre-trade check could not be run'))
    } finally {
      setSubmitting(false)
    }
  }

  function handleFormSubmit(event: FormEvent) {
    event.preventDefault()
    runCheck()
  }

  return <><main className="orders-page mx-auto w-full max-w-[1700px] pb-10">
    <header className="orders-header"><div><p className="eyebrow">TRADINGOS // EXECUTION CONTROL</p><h1>Orders &amp; Trades</h1><p>Unified paper and live execution ledger</p></div></header>
    {loadError && <ErrorNotice className="mb-4" message={loadError} onRetry={reload} />}
    <section className="orders-summary">
      <div className="summary-stat"><span>TOTAL EXECUTIONS</span><strong>{orders.length}</strong><small>{paperCount} paper · {liveCount} live</small></div>
      <div className="summary-stat"><span>CAPPED INTENTS</span><strong>{intents.filter(i => i.status === 'capped').length}</strong><small>live order intents blocked by the standing rate/notional cap</small></div>
      <div className="summary-stat"><span>ORDER DISPATCH</span><strong className="text-muted-foreground">{vitals === null ? '—' : vitals.latency.order_dispatch_p50_ms === null ? 'no orders yet' : `${vitals.latency.order_dispatch_p50_ms.toFixed(0)} / ${vitals.latency.order_dispatch_p95_ms?.toFixed(0) ?? '—'} ms`}</strong><small>median / 95th percentile{vitals ? ` · budget ${vitals.latency.budget_ms.toFixed(0)} ms` : ''}</small></div>
      <div className="summary-stat"><span>OPEN POSITIONS</span><strong>{positions.length}</strong><small>{positions.filter(p => p.mode === 'live').length} live · {positions.filter(p => p.mode === 'paper').length} paper</small></div>
    </section>
    <div className="orders-layout"><div className="orders-main">
      <div className="orders-tabs"><button className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}>Orders &amp; Trades <b>{orders.length}</b></button><button className={tab === 'intents' ? 'active' : ''} onClick={() => setTab('intents')}>Live Order Intents <b className="amber-count">{intents.length}</b></button></div>
      {tab === 'orders' ? <Panel title="Execution ledger" eyebrow="PAPER + LIVE · NEWEST FIRST"><div className="orders-filters"><div className="filter-chips">{['BOTH', 'PAPER', 'LIVE'].map(x => <button className={mode === x ? 'active' : ''} onClick={() => setMode(x)} key={x}>{x}</button>)}</div><select value={status} onChange={e => setStatus(e.target.value)}>{statusOptions.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}</select><label className="symbol-search"><Search className="size-3" /><input placeholder="Search symbol..." value={query} onChange={e => setQuery(e.target.value)} /></label></div><div className="orders-table-wrap"><table className="orders-table"><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>QTY</th><th>PRICE</th><th>STATUS</th><th>STRATEGY</th></tr></thead><tbody>{filtered.slice((page - 1) * 5, page * 5).map(o => <tr key={o.id}><td className="mono">{new Date(o.time).toLocaleString()}<small>{o.mode.toUpperCase()}</small></td><td>{o.symbol}</td><td><span className={o.side.toLowerCase() === 'buy' ? 'buy-badge' : 'sell-badge'}>{o.side.toLowerCase() === 'buy' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />}{o.side.toUpperCase()}</span></td><td>{o.quantity}</td><td className="mono">{o.price !== null ? `₹${o.price.toFixed(2)}` : '—'}</td><td><span className={`status-badge status-${o.status.toLowerCase()}`}>{o.status}</span></td><td className="source-cell">{o.strategy_id ? o.strategy_id.slice(0, 8) : '—'}</td></tr>)}{filtered.length === 0 && <tr><td colSpan={7} className="p-4 text-center text-xs text-muted-foreground">No executions match these filters.</td></tr>}</tbody></table></div><div className="orders-footer"><span>Showing {filtered.length === 0 ? 0 : (page - 1) * 5 + 1}–{Math.min(page * 5, filtered.length)} of {filtered.length} executions</span><div><button disabled={page === 1} onClick={() => setPage(page - 1)}><ChevronLeft className="size-3" /></button><button disabled={page * 5 >= filtered.length} onClick={() => setPage(page + 1)}><ChevronRight className="size-3" /></button></div></div></Panel> : <Panel title="Live Order Intents" eyebrow="FULL HISTORY · AUTONOMOUS SAFETY-LAYER TRAIL"><div className="intent-history">{intents.map(intent => <div className="intent-row" key={intent.id}><div className={`intent-ring ring-${intent.status.toLowerCase()}`}><Timer className="size-4" /></div><div className="intent-info"><strong>{intent.symbol}</strong><span>{intent.side.toUpperCase()} · {intent.quantity} qty</span><small>{intent.id.slice(0, 8)} · {new Date(intent.generated_at).toLocaleString()}</small></div><span className={`outcome outcome-${intent.status.toLowerCase()}`}>{intent.status === 'approved' || intent.status === 'submitted' ? <Check className="size-3" /> : intent.status === 'rejected' || intent.status === 'expired' || intent.status === 'failed' ? <X className="size-3" /> : <Clock3 className="size-3" />}{intent.status.replace('_', ' ').toUpperCase()}</span></div>)}{intents.length === 0 && <p className="p-4 text-xs text-muted-foreground">No live order intents recorded.</p>}</div></Panel>}
    </div>
    <aside className="orders-side">
      {canTrade ? <Panel title="Pre-trade check" eyebrow="DRY RUN · NOTHING IS SENT"><form onSubmit={handleFormSubmit}><p className="px-4 pt-3 text-xs text-muted-foreground">Asks the real risk gate (kill switch, compliance, naked options, correlation) whether an order like this would be allowed. It places no order and sends nothing to a broker; live orders come only from a strategy with autonomy on.</p><div className="mode-toggle"><button type="button" className={!live ? 'selected-paper' : ''} onClick={() => setLive(false)}>PAPER<small>paper-mode checks</small></button><button type="button" className={live ? 'selected-live' : ''} onClick={() => setLive(true)}>LIVE<small>live-mode checks</small></button></div>{submitError && <div className="live-warning" role="alert"><AlertTriangle className="size-4" /><span><strong>Would be refused</strong><small>{submitError}</small></span></div>}{checkResult && <div className="readiness-banner" role="status"><ShieldCheck className="size-4" /><span><strong>Would be allowed</strong><small>{checkResult.side.toUpperCase()} {checkResult.quantity} {checkResult.symbol} at ₹{checkResult.price.toFixed(2)} passes every {checkResult.mode} pre-trade check. Nothing was sent.</small></span></div>}<label className="order-field">SYMBOL<input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} /></label><div className="field-grid"><label className="order-field">SIDE<select value={side} onChange={e => setSide(e.target.value as 'buy' | 'sell')}><option value="buy">BUY</option><option value="sell">SELL</option></select></label><label className="order-field">QUANTITY<input type="number" min="1" value={qty} onChange={e => setQty(e.target.value)} /></label></div><div className="field-grid"><label className="order-field">PROPOSED PRICE<input type="number" min="0" step="any" value={price} onChange={e => setPrice(e.target.value)} /></label><label className="order-field">REFERENCE PRICE<input type="number" min="0" step="any" value={referencePrice} onChange={e => setReferencePrice(e.target.value)} /></label></div><label className="order-field">PORTFOLIO VALUE<input type="number" min="0" step="any" value={portfolioValue} onChange={e => setPortfolioValue(e.target.value)} /></label><p className="px-4 text-[11px] text-muted-foreground">Fill every field with a real figure: the position limit and the circuit band are judged against them.</p><button type="submit" disabled={submitting || !inputsReady} className="submit-paper"><ShieldCheck className="size-4" />Run pre-trade check</button></form></Panel> : <Panel title="Pre-trade check" eyebrow="READ-ONLY"><p className="p-4 text-xs text-muted-foreground">Your role does not have access to the pre-trade check.</p></Panel>}
      <Panel title="Positions by strategy" eyebrow="LIVE + PAPER · CURRENT NET EXPOSURE">
        {positions.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No open positions — every position closes out to flat.</p> : <div className="position-list">{positions.map(p => <div className="position-row" key={`${p.mode}-${p.strategy_id}-${p.symbol}`}><div className="min-w-0"><strong>{p.strategy_name}</strong><small>{p.symbol} · {p.mode.toUpperCase()}</small></div><div className="text-right"><span className={p.quantity < 0 ? 'sell-badge' : 'buy-badge'}>{p.quantity > 0 ? '+' : ''}{p.quantity}</span><small className="block text-muted-foreground">avg ₹{p.avg_cost.toFixed(2)}</small></div></div>)}</div>}
      </Panel>
      <Panel title="Broker gateways" eyebrow="CIRCUIT BREAKERS">{breakers === null ? <p className="p-4 text-xs text-muted-foreground">Could not read the broker circuit breakers.</p> : breakers.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No broker is configured.</p> : <div className="position-list">{breakers.map(b => <div className="position-row" key={b.broker}><div className="min-w-0"><strong>{b.broker}</strong><small>{b.consecutive_failures} of {b.failure_threshold} failures{b.cooldown_remaining_seconds ? ` · retry in ${Math.ceil(b.cooldown_remaining_seconds)}s` : ''}</small></div><div className="text-right"><strong className={b.state === 'open' ? 'text-rose-300' : 'text-emerald-300'}>{b.state === 'open' ? 'OPEN (blocking)' : 'CLOSED'}</strong></div></div>)}</div>}</Panel>
    </aside></div>
  </main></>
}
