'use client'

import { FormEvent, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Check, ChevronLeft, ChevronRight, Clock3, History, LockKeyhole, Search, ShieldAlert, Timer, X, Zap } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type LiveOrderIntentDto,
  type PositionByStrategy,
  type UnifiedExecution,
  ApiError,
  listLiveIntents,
  listOrders,
  listPositionsByStrategy,
  submitOrderIntent,
} from '@/lib/api'

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
  const [confirm, setConfirm] = useState(false)
  const [symbol, setSymbol] = useState('RELIANCE')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState('50')
  const [price, setPrice] = useState('100')
  const [referencePrice, setReferencePrice] = useState('100')
  const [portfolioValue, setPortfolioValue] = useState('1000000')
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function reload() {
    const [ordersData, intentsData, positionsData] = await Promise.all([
      listOrders('both'),
      listLiveIntents(),
      listPositionsByStrategy(),
    ])
    setOrders(ordersData)
    setIntents(intentsData)
    setPositions(positionsData)
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
  const paperCount = orders.filter(o => o.mode === 'paper').length
  const liveCount = orders.filter(o => o.mode === 'live').length

  async function submitOrder() {
    if (!canTrade) return
    if (live) { setConfirm(true); return }
    await doSubmit()
  }

  async function doSubmit() {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const proposedPrice = Number(price)
      const quantity = Number(qty)
      await submitOrderIntent({
        mode: live ? 'live' : 'paper',
        symbol,
        side,
        quantity,
        proposed_price: proposedPrice,
        reference_price: Number(referencePrice),
        proposed_position_value: proposedPrice * quantity,
        portfolio_value: Number(portfolioValue),
      })
      setConfirm(false)
      await reload()
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : 'Order intent rejected')
    } finally {
      setSubmitting(false)
    }
  }

  function handleFormSubmit(event: FormEvent) {
    event.preventDefault()
    submitOrder()
  }

  return <ShellLayout><main className="orders-page mx-auto w-full max-w-[1700px] pb-10">
    <header className="orders-header"><div><p className="eyebrow">TRADINGOS // EXECUTION CONTROL</p><h1>Orders &amp; Trades</h1><p>Unified paper and live execution ledger · NSE / BSE gateways synchronized</p></div></header>
    <section className="orders-summary">
      <div className="summary-stat"><span>TOTAL EXECUTIONS</span><strong>{orders.length}</strong><small>{paperCount} paper · {liveCount} live</small></div>
      <div className="summary-stat"><span>PENDING SIGN-OFFS</span><strong>{intents.filter(i => i.status === 'pending_approval').length}</strong><small>live order intents awaiting approval</small></div>
      <div className="summary-stat"><span>LATENCY</span><strong className="text-muted-foreground">Prometheus only</strong><small>tradingos_order_dispatch_latency_seconds · see Grafana</small></div>
      <div className="summary-stat"><span>OPEN POSITIONS</span><strong>{positions.length}</strong><small>{positions.filter(p => p.mode === 'live').length} live · {positions.filter(p => p.mode === 'paper').length} paper</small></div>
    </section>
    <div className="orders-layout"><div className="orders-main">
      <div className="orders-tabs"><button className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}>Orders &amp; Trades <b>{orders.length}</b></button><button className={tab === 'intents' ? 'active' : ''} onClick={() => setTab('intents')}>Live Order Intents <b className="amber-count">{intents.length}</b></button></div>
      {tab === 'orders' ? <Panel title="Execution ledger" eyebrow="PAPER + LIVE · NEWEST FIRST"><div className="orders-filters"><div className="filter-chips">{['BOTH', 'PAPER', 'LIVE'].map(x => <button className={mode === x ? 'active' : ''} onClick={() => setMode(x)} key={x}>{x}</button>)}</div><select value={status} onChange={e => setStatus(e.target.value)}><option>ALL</option><option>FILLED</option><option>PARTIAL</option><option>OPEN</option><option>REJECTED</option><option>CANCELLED</option></select><label className="symbol-search"><Search className="size-3" /><input placeholder="Search symbol..." value={query} onChange={e => setQuery(e.target.value)} /></label></div><div className="orders-table-wrap"><table className="orders-table"><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>QTY</th><th>PRICE</th><th>STATUS</th><th>STRATEGY</th></tr></thead><tbody>{filtered.slice((page - 1) * 5, page * 5).map(o => <tr key={o.id}><td className="mono">{new Date(o.time).toLocaleTimeString()}<small>{o.mode.toUpperCase()}</small></td><td>{o.symbol}</td><td><span className={o.side.toLowerCase() === 'buy' ? 'buy-badge' : 'sell-badge'}>{o.side.toLowerCase() === 'buy' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />}{o.side.toUpperCase()}</span></td><td>{o.quantity}</td><td className="mono">{o.price !== null ? `₹${o.price.toFixed(2)}` : '—'}</td><td><span className={`status-badge status-${o.status.toLowerCase()}`}>{o.status}</span></td><td className="source-cell">{o.strategy_id ? o.strategy_id.slice(0, 8) : '—'}</td></tr>)}{filtered.length === 0 && <tr><td colSpan={7} className="p-4 text-center text-xs text-muted-foreground">No executions match these filters.</td></tr>}</tbody></table></div><div className="orders-footer"><span>Showing {filtered.length === 0 ? 0 : (page - 1) * 5 + 1}–{Math.min(page * 5, filtered.length)} of {filtered.length} executions</span><div><button disabled={page === 1} onClick={() => setPage(page - 1)}><ChevronLeft className="size-3" /></button><button disabled={page * 5 >= filtered.length} onClick={() => setPage(page + 1)}><ChevronRight className="size-3" /></button></div></div></Panel> : <Panel title="Live Order Intents" eyebrow="FULL HISTORY · HUMAN SIGN-OFF TRAIL"><div className="intent-history">{intents.map(intent => <div className="intent-row" key={intent.id}><div className={`intent-ring ring-${intent.status.toLowerCase()}`}><Timer className="size-4" /></div><div className="intent-info"><strong>{intent.symbol}</strong><span>{intent.side.toUpperCase()} · {intent.quantity} qty</span><small>{intent.id.slice(0, 8)} · {new Date(intent.generated_at).toLocaleString()}</small></div><span className={`outcome outcome-${intent.status.toLowerCase()}`}>{intent.status === 'approved' ? <Check className="size-3" /> : intent.status === 'rejected' || intent.status === 'expired' ? <X className="size-3" /> : <Clock3 className="size-3" />}{intent.status.replace('_', ' ').toUpperCase()}</span></div>)}{intents.length === 0 && <p className="p-4 text-xs text-muted-foreground">No live order intents recorded.</p>}</div></Panel>}
    </div>
    <aside className="orders-side">
      {canTrade ? <Panel title="Manual order entry" eyebrow="ROUTED THROUGH THE REAL RISK GATE"><form onSubmit={handleFormSubmit}><div className="mode-toggle"><button type="button" className={!live ? 'selected-paper' : ''} onClick={() => setLive(false)}>PAPER<small>Safe simulation</small></button><button type="button" className={live ? 'selected-live' : ''} onClick={() => setLive(true)}>LIVE<small>Requires confirmation</small></button></div>{live && <div className="live-warning"><ShieldAlert className="size-4" /><span><strong>LIVE ORDER</strong><small>This routes to the real risk gate and, if accepted, real broker exposure.</small></span></div>}{submitError && <div className="live-warning"><AlertTriangle className="size-4" /><span><small>{submitError}</small></span></div>}<label className="order-field">SYMBOL<input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} /></label><div className="field-grid"><label className="order-field">SIDE<select value={side} onChange={e => setSide(e.target.value as 'buy' | 'sell')}><option value="buy">BUY</option><option value="sell">SELL</option></select></label><label className="order-field">QUANTITY<input type="number" value={qty} onChange={e => setQty(e.target.value)} /></label></div><div className="field-grid"><label className="order-field">PROPOSED PRICE<input type="number" value={price} onChange={e => setPrice(e.target.value)} /></label><label className="order-field">REFERENCE PRICE<input type="number" value={referencePrice} onChange={e => setReferencePrice(e.target.value)} /></label></div><label className="order-field">PORTFOLIO VALUE<input type="number" value={portfolioValue} onChange={e => setPortfolioValue(e.target.value)} /></label><button type="submit" disabled={submitting} className={live ? 'submit-live' : 'submit-paper'}>{live ? <><LockKeyhole className="size-4" />Review LIVE order</> : <><Zap className="size-4" />Submit PAPER intent</>}</button></form></Panel> : <Panel title="Manual order entry" eyebrow="READ-ONLY"><p className="p-4 text-xs text-muted-foreground">Your role does not have order-submission access.</p></Panel>}
      <Panel title="Positions by strategy" eyebrow="LIVE + PAPER · CURRENT NET EXPOSURE">
        {positions.length === 0 ? <p className="p-4 text-xs text-muted-foreground">No open positions — every position closes out to flat.</p> : <div className="position-list">{positions.map(p => <div className="position-row" key={`${p.mode}-${p.strategy_id}-${p.symbol}`}><div className="min-w-0"><strong>{p.strategy_name}</strong><small>{p.symbol} · {p.mode.toUpperCase()}</small></div><div className="text-right"><span className={p.quantity < 0 ? 'sell-badge' : 'buy-badge'}>{p.quantity > 0 ? '+' : ''}{p.quantity}</span><small className="block text-muted-foreground">avg ₹{p.avg_cost.toFixed(2)}</small></div></div>)}</div>}
      </Panel>
      <Panel title="Gateway health" eyebrow="EXECUTION INFRA"><p className="p-4 text-xs text-muted-foreground"><History className="mr-1 inline size-3" />Per-gateway latency/session status isn't exposed via API yet — see the Prometheus/Grafana dashboards for real infra metrics.</p></Panel>
    </aside></div>
    {confirm && <div className="confirm-backdrop"><div className="confirm-modal"><button className="modal-close" onClick={() => setConfirm(false)}><X className="size-4" /></button><ShieldAlert className="modal-icon" /><p className="eyebrow">FINAL LIVE ORDER CONFIRMATION</p><h2>{side.toUpperCase()} {qty} {symbol}</h2><p>This order will be routed through the real risk gate at the proposed price. Type the symbol below to confirm deliberate intent.</p><input autoFocus placeholder={`Type ${symbol} to confirm`} onChange={e => { if (e.target.value.toUpperCase() === symbol) doSubmit() }} /><button className="submit-live" disabled={submitting} onClick={doSubmit}>Confirm and route LIVE order</button></div></div>}
  </main></ShellLayout>
}
