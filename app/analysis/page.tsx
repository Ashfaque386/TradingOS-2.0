'use client'

import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, Cable, Clock3, Globe2, LineChart as LineChartIcon, ListTree, Search, Sparkles } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { type FreshnessRecord, type Instrument, type MarketPulse, getFreshness, getMarketPulse, listInstruments } from '@/lib/api'

const TABS = [
  { id: 'pulse', label: 'Pulse', icon: Activity },
  { id: 'technical', label: 'Technical Indicators', icon: LineChartIcon },
  { id: 'freshness', label: 'Data Freshness', icon: Clock3 },
  { id: 'providers', label: 'Provider Status', icon: Cable },
  { id: 'chain', label: 'Live Option Chain', icon: ListTree },
] as const

type TabId = (typeof TABS)[number]['id']

function Panel({ title, eyebrow, children, actions, className = '' }: { title: string; eyebrow?: string; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={`analysis-panel ${className}`}>
      <div className="analysis-panel-head"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h2>{title}</h2></div>{actions}</div>
      {children}
    </section>
  )
}

function GapNotice({ children }: { children: React.ReactNode }) {
  return <div className="rounded-xl border border-amber-300/20 bg-amber-300/[.06] p-4 text-xs text-amber-100"><AlertTriangle className="mb-2 size-4" /><p>{children}</p></div>
}

function VixGauge({ vix }: { vix: number }) {
  const pct = Math.min(Math.max((vix - 10) / (35 - 10), 0), 1)
  const angle = pct * 180 - 90
  const zone = vix < 15 ? 'Calm' : vix < 22 ? 'Watchful' : 'Elevated'
  return (
    <div className="vix-gauge-wrap">
      <div className="vix-gauge">
        <div className="vix-gauge-arc" />
        <div className="vix-gauge-cover" />
        <div className="vix-gauge-needle" style={{ transform: `translateX(-50%) rotate(${angle}deg)` }} />
        <div className="vix-gauge-value"><strong>{vix.toFixed(2)}</strong><span>India VIX</span></div>
      </div>
      <div className="vix-gauge-legend"><span><i className="dot dot-green" /> Calm &lt;15</span><span><i className="dot dot-amber" /> Watchful 15–22</span><span><i className="dot dot-red" /> Elevated &gt;22</span></div>
      <p className="vix-gauge-note">Regime: <strong>{zone}</strong></p>
    </div>
  )
}

function ChangeTile({ name, change }: { name: string; change: number }) {
  const intensity = Math.min(Math.abs(change) / 3.5, 1)
  const positive = change >= 0
  return (
    <div className="sector-tile" style={{ background: positive ? `rgba(52, 211, 153, ${0.08 + intensity * 0.34})` : `rgba(251, 113, 133, ${0.08 + intensity * 0.34})`, borderColor: positive ? `rgba(52,211,153,${0.25 + intensity * 0.4})` : `rgba(251,113,133,${0.25 + intensity * 0.4})` }}>
      <span>{name}</span>
      <strong className={positive ? 'gain' : 'loss'}>{positive ? '+' : ''}{change.toFixed(2)}%</strong>
    </div>
  )
}

function PulseTab() {
  const [pulse, setPulse] = useState<MarketPulse | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      const data = await getMarketPulse()
      if (!cancelled) setPulse(data)
    }
    load()
    const interval = setInterval(load, 30000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  if (!pulse) return <p className="p-4 text-xs text-muted-foreground">Loading market pulse…</p>

  return (
    <div className="pulse-grid">
      <Panel title="India VIX" eyebrow="VOLATILITY GAUGE"><VixGauge vix={pulse.india_vix} /></Panel>
      <Panel title="NSE sector indices" eyebrow="DAY CHANGE HEATMAP" className="sector-panel"><div className="sector-grid">{Object.entries(pulse.sector_indices_change_pct).map(([name, change]) => <ChangeTile key={name} name={name} change={change} />)}</div></Panel>
      <Panel title="Global indices" eyebrow="OVERNIGHT & LIVE FEED" className="global-panel"><div className="global-strip">{Object.entries(pulse.global_indices_change_pct).map(([name, change]) => <div className="global-chip" key={name}><Globe2 className="size-3.5" /><span>{name}</span><strong className={change >= 0 ? 'gain' : 'loss'}>{change >= 0 ? '+' : ''}{change.toFixed(2)}%</strong></div>)}</div></Panel>
    </div>
  )
}

function TechnicalTab() {
  return <Panel title="Technical Indicators" eyebrow="NOT AVAILABLE"><GapNotice>No backend endpoint computes technical indicators (SMA/EMA/RSI/MACD/Bollinger Bands) for arbitrary symbols — only the backtest engine computes signal-generation indicators internally, and doesn't expose a general-purpose indicator series API. Real OHLCV data exists in the data lake (see Data Freshness) but isn't served as a chartable time series here.</GapNotice></Panel>
}

function FreshnessTab() {
  const [instruments, setInstruments] = useState<Instrument[]>([])
  const [symbol, setSymbol] = useState<string | null>(null)
  const [records, setRecords] = useState<FreshnessRecord[]>([])
  const [query, setQuery] = useState('')

  useEffect(() => { listInstruments().then((data) => { setInstruments(data); setSymbol((prev) => prev ?? data[0]?.symbol ?? null) }) }, [])
  useEffect(() => { if (symbol) getFreshness(symbol).then(setRecords) }, [symbol])

  const filtered = instruments.filter((i) => i.symbol.toLowerCase().includes(query.toLowerCase())).slice(0, 50)

  return (
    <div className="technical-layout">
      <Panel title="Symbol" eyebrow="SEARCH" className="symbol-panel">
        <label className="symbol-search-box"><Search className="size-3.5" /><input placeholder="Search NSE / BSE symbol..." value={query} onChange={(e) => setQuery(e.target.value)} /></label>
        <div className="symbol-list">{filtered.map((i) => <button key={i.symbol} className={i.symbol === symbol ? 'symbol-chip active' : 'symbol-chip'} onClick={() => setSymbol(i.symbol)}>{i.symbol}</button>)}</div>
      </Panel>
      <Panel title={`${symbol ?? '—'} · dataset freshness`} eyebrow="DATA PIPELINE HEALTH">
        <div className="overflow-x-auto"><table className="analysis-table"><thead><tr><th>Data type</th><th>Data date</th><th>Row count</th><th>Ingested at</th></tr></thead><tbody>{records.map((r, i) => <tr key={i}><td>{r.data_type}</td><td className="mono">{r.data_date}</td><td className="mono">{r.row_count.toLocaleString('en-IN')}</td><td className="mono">{new Date(r.ingested_at).toLocaleString()}</td></tr>)}{records.length === 0 && <tr><td colSpan={4} className="p-4 text-center text-xs text-muted-foreground">No freshness records for this symbol.</td></tr>}</tbody></table></div>
      </Panel>
    </div>
  )
}

function ProvidersTab() {
  return <Panel title="Provider Status" eyebrow="NOT AVAILABLE"><GapNotice>No backend endpoint reports live LLM provider or broker connectivity health. LLM fallback order is visible in Settings → Agent Gateway Config; the LlmRouter itself tracks per-call failures internally but doesn't expose a health/uptime API. Broker circuit-breaker state is enforced server-side (src/brokers/factory.py's ResilientBrokerAdapter) but also isn't surfaced over HTTP yet.</GapNotice></Panel>
}

function OptionChainTab() {
  const [instruments, setInstruments] = useState<Instrument[]>([])
  const [underlying, setUnderlying] = useState('NIFTY')

  useEffect(() => { listInstruments().then(setInstruments) }, [])

  const options = instruments.filter((i) => i.instrument_type === 'option' && i.underlying_symbol === underlying)
  const underlyings = Array.from(new Set(instruments.filter((i) => i.instrument_type === 'option').map((i) => i.underlying_symbol).filter((s): s is string => Boolean(s))))

  return (
    <Panel title="Option instrument master" eyebrow={`${underlying} · STATIC INSTRUMENT MASTER — NO LIVE OI/IV/LTP FEED`} actions={<select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="backtest-select">{underlyings.length > 0 ? underlyings.map((s) => <option key={s}>{s}</option>) : <option>{underlying}</option>}</select>}>
      <GapNotice>No live option-chain feed (open interest, implied volatility, last-traded price) is exposed by the backend — only the static instrument master (strike, expiry, lot size, tick size) below.</GapNotice>
      <div className="overflow-x-auto mt-4"><table className="option-chain-table"><thead><tr><th>Symbol</th><th>Strike</th><th>Type</th><th>Expiry</th><th>Lot size</th><th>Tick size</th></tr></thead><tbody>{options.map((o) => <tr key={o.symbol}><td className="mono">{o.symbol}</td><td className="strike-cell">{o.strike_price}</td><td>{o.option_type}</td><td className="mono">{o.expiry_date}</td><td className="mono">{o.lot_size}</td><td className="mono">{o.tick_size}</td></tr>)}{options.length === 0 && <tr><td colSpan={6} className="p-4 text-center text-xs text-muted-foreground">No option instruments found for this underlying.</td></tr>}</tbody></table></div>
    </Panel>
  )
}

export default function MarketAnalysisPage() {
  const [tab, setTab] = useState<TabId>('pulse')
  const active = useMemo(() => TABS.find((t) => t.id === tab), [tab])
  return (
    <ShellLayout>
      <main className="analysis-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="analysis-header">
          <div><p className="eyebrow">TRADINGOS // MARKET INTELLIGENCE</p><h1>Market Analysis</h1><p>Volatility, sector breadth, data health, and derivatives instrument master — real backend state, honest gaps labeled where no live feed exists.</p></div>
          <div className="analysis-live-chip"><Sparkles className="size-3.5" /> {active?.label}</div>
        </header>
        <div className="analysis-tabs">{TABS.map((t) => { const Icon = t.icon; return <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}><Icon className="size-3.5" /> {t.label}</button> })}</div>
        {tab === 'pulse' && <PulseTab />}
        {tab === 'technical' && <TechnicalTab />}
        {tab === 'freshness' && <FreshnessTab />}
        {tab === 'providers' && <ProvidersTab />}
        {tab === 'chain' && <OptionChainTab />}
      </main>
    </ShellLayout>
  )
}
