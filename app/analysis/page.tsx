'use client'

import { useEffect, useMemo, useState } from 'react'
import { Bar, CartesianGrid, ComposedChart, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, AlertTriangle, Cable, Clock3, Globe2, LineChart as LineChartIcon, ListTree, Search, Sparkles } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { ChartContainer } from '@/components/ui/chart'
import {
  ApiError,
  type BrokerCircuitBreakerStatus,
  type BrokerCredentialStatus,
  type FreshnessRecord,
  type IndicatorSeries,
  type Instrument,
  type LiveOptionChain,
  type LlmProviderStatus,
  type MarketPulse,
  LLM_PROVIDER_LABELS,
  getFreshness,
  getIndicators,
  getLiveOptionChain,
  getLiveOptionExpiries,
  getMarketPulse,
  listBrokerCircuitBreakerStatus,
  listBrokerCredentialStatus,
  listInstruments,
  listLlmProviderStatus,
} from '@/lib/api'

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

const OVERLAYS = [
  { id: 'sma', label: 'SMA' },
  { id: 'ema', label: 'EMA' },
  { id: 'bollinger', label: 'Bollinger' },
] as const
type OverlayId = (typeof OVERLAYS)[number]['id']

function TechnicalTab() {
  const [instruments, setInstruments] = useState<Instrument[]>([])
  const [symbol, setSymbol] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [series, setSeries] = useState<IndicatorSeries | null>(null)
  const [overlays, setOverlays] = useState<OverlayId[]>(['sma', 'ema'])

  useEffect(() => { listInstruments().then((data) => { setInstruments(data); setSymbol((prev) => prev ?? data[0]?.symbol ?? null) }) }, [])
  useEffect(() => { if (symbol) getIndicators(symbol).then(setSeries) }, [symbol])

  const filtered = instruments.filter((i) => i.symbol.toLowerCase().includes(query.toLowerCase())).slice(0, 50)
  const toggleOverlay = (id: OverlayId) => setOverlays((prev) => prev.includes(id) ? prev.filter((o) => o !== id) : [...prev, id])

  const chartData = useMemo(() => {
    if (!series) return []
    return series.dates.map((date, i) => ({
      date,
      close: series.close[i],
      sma: series.sma[i],
      ema: series.ema[i],
      bollinger_upper: series.bollinger_upper[i],
      bollinger_lower: series.bollinger_lower[i],
      rsi: series.rsi[i],
      macd: series.macd[i],
      macd_signal: series.macd_signal[i],
      macd_histogram: series.macd_histogram[i],
    }))
  }, [series])

  const hasData = (series?.dates.length ?? 0) > 0

  return (
    <div className="technical-layout">
      <Panel title="Symbol" eyebrow="SEARCH" className="symbol-panel">
        <label className="symbol-search-box"><Search className="size-3.5" /><input placeholder="Search NSE / BSE symbol..." value={query} onChange={(e) => setQuery(e.target.value)} /></label>
        <div className="symbol-list">{filtered.map((i) => <button key={i.symbol} className={i.symbol === symbol ? 'symbol-chip active' : 'symbol-chip'} onClick={() => setSymbol(i.symbol)}>{i.symbol}</button>)}</div>
      </Panel>
      <Panel
        title={`${symbol ?? '—'} · close · SMA(${series?.sma_window ?? 20}) · EMA(${series?.ema_span ?? 20}) · Bollinger`}
        eyebrow="REAL OHLCV FROM THE DATA LAKE"
        className="indicator-chart-panel"
        actions={<div className="overlay-toggles">{OVERLAYS.map((o) => <button key={o.id} className={overlays.includes(o.id) ? 'active' : ''} onClick={() => toggleOverlay(o.id)}>{o.label}</button>)}</div>}
      >
        {!hasData ? <p className="p-4 text-xs text-muted-foreground">No ingested OHLCV history for this symbol yet — see Data Freshness. Real indicators need real bars from the data lake, not a live quote, so nothing is shown here until the pipeline has actually ingested this symbol.</p> : (
          <ChartContainer config={{ close: { label: 'Close', color: 'var(--chart-1)' } }} className="h-[300px] w-full px-2">
            <LineChart data={chartData} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
              <CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} minTickGap={40} />
              <YAxis tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} domain={['auto', 'auto']} />
              <Tooltip />
              <Line type="monotone" dataKey="close" stroke="#e2e8f0" dot={false} strokeWidth={1.5} name="Close" />
              {overlays.includes('sma') && <Line type="monotone" dataKey="sma" stroke="#22d3ee" dot={false} strokeWidth={1.25} name={`SMA(${series?.sma_window})`} connectNulls={false} />}
              {overlays.includes('ema') && <Line type="monotone" dataKey="ema" stroke="#c084fc" dot={false} strokeWidth={1.25} name={`EMA(${series?.ema_span})`} connectNulls={false} />}
              {overlays.includes('bollinger') && <Line type="monotone" dataKey="bollinger_upper" stroke="#fbbf24" dot={false} strokeWidth={1} strokeDasharray="3 3" name="Bollinger Upper" connectNulls={false} />}
              {overlays.includes('bollinger') && <Line type="monotone" dataKey="bollinger_lower" stroke="#fbbf24" dot={false} strokeWidth={1} strokeDasharray="3 3" name="Bollinger Lower" connectNulls={false} />}
            </LineChart>
          </ChartContainer>
        )}
      </Panel>
      <div className="sub-indicators">
        <Panel title={`RSI(${series?.rsi_period ?? 14})`} eyebrow="MOMENTUM">
          {!hasData ? <p className="p-4 text-xs text-muted-foreground">—</p> : (
            <ChartContainer config={{ rsi: { label: 'RSI', color: 'var(--chart-2)' } }} className="h-[160px] w-full px-2">
              <LineChart data={chartData} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
                <CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} />
                <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 9 }} axisLine={false} tickLine={false} minTickGap={60} />
                <YAxis domain={[0, 100]} tick={{ fill: '#64748b', fontSize: 9 }} axisLine={false} tickLine={false} />
                <Tooltip />
                <Line type="monotone" dataKey="rsi" stroke="#f472b6" dot={false} strokeWidth={1.25} connectNulls={false} />
              </LineChart>
            </ChartContainer>
          )}
        </Panel>
        <Panel title="MACD(12, 26, 9)" eyebrow="TREND">
          {!hasData ? <p className="p-4 text-xs text-muted-foreground">—</p> : (
            <ChartContainer config={{ macd: { label: 'MACD', color: 'var(--chart-3)' } }} className="h-[160px] w-full px-2">
              <ComposedChart data={chartData} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
                <CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} />
                <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 9 }} axisLine={false} tickLine={false} minTickGap={60} />
                <YAxis tick={{ fill: '#64748b', fontSize: 9 }} axisLine={false} tickLine={false} />
                <Tooltip />
                <Bar dataKey="macd_histogram" fill="#475569" />
                <Line type="monotone" dataKey="macd" stroke="#22d3ee" dot={false} strokeWidth={1.25} connectNulls={false} />
                <Line type="monotone" dataKey="macd_signal" stroke="#fbbf24" dot={false} strokeWidth={1.25} connectNulls={false} />
              </ComposedChart>
            </ChartContainer>
          )}
        </Panel>
      </div>
    </div>
  )
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
  const [providers, setProviders] = useState<LlmProviderStatus[]>([])
  const [brokers, setBrokers] = useState<BrokerCredentialStatus[]>([])
  const [breakers, setBreakers] = useState<BrokerCircuitBreakerStatus[]>([])

  useEffect(() => {
    listLlmProviderStatus().then(setProviders)
    listBrokerCredentialStatus().then(setBrokers)
    listBrokerCircuitBreakerStatus().then(setBreakers)
    const interval = setInterval(() => listBrokerCircuitBreakerStatus().then(setBreakers), 10_000)
    return () => clearInterval(interval)
  }, [])

  const breakerByBroker = Object.fromEntries(breakers.map((b) => [b.broker, b]))

  return (
    <div className="technical-layout">
      <Panel title="LLM providers" eyebrow="CREDENTIAL + FALLBACK-CHAIN STATUS">
        <div className="provider-status-list">
          {providers.map((p) => (
            <div className="provider-status-row" key={p.provider}>
              <span className={`provider-status-dot ${p.configured ? (p.in_fallback_order ? 'green' : 'amber') : 'gray'}`} />
              <div className="min-w-0 flex-1">
                <strong>{LLM_PROVIDER_LABELS[p.provider as keyof typeof LLM_PROVIDER_LABELS] ?? p.provider}</strong>
                <small>{p.configured ? (p.in_fallback_order ? 'configured · in fallback chain' : 'configured · not in fallback chain') : 'not configured'}</small>
              </div>
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Broker connections" eyebrow="OAUTH TOKEN STATUS + LIVE CIRCUIT-BREAKER STATE">
        <div className="provider-status-list">
          {brokers.map((b) => {
            const breaker = breakerByBroker[b.broker]
            const tripped = breaker?.state === 'open'
            return (
              <div className="provider-status-row" key={b.broker}>
                <span className={`provider-status-dot ${tripped ? 'red' : b.token_status === 'valid' ? 'green' : b.token_status === 'expired' ? 'amber' : 'gray'}`} />
                <div className="min-w-0 flex-1">
                  <strong className="capitalize">{b.broker}</strong>
                  <small>{b.token_status === 'valid' ? 'token valid' : b.token_status === 'expired' ? 'token expired — reconnect in Settings' : 'not connected'}</small>
                  <small>
                    {tripped
                      ? `circuit OPEN — ${breaker.consecutive_failures} consecutive failures${breaker.cooldown_remaining_seconds != null ? `, retry in ${Math.ceil(breaker.cooldown_remaining_seconds)}s` : ''}`
                      : breaker
                        ? `circuit closed · ${breaker.consecutive_failures}/${breaker.failure_threshold} consecutive failures`
                        : 'circuit closed · no dispatch attempts yet'}
                  </small>
                </div>
              </div>
            )
          })}
        </div>
      </Panel>
    </div>
  )
}

function LiveOptionChainPanel() {
  const [underlyingInput, setUnderlyingInput] = useState('')
  const [expiries, setExpiries] = useState<string[] | null>(null)
  const [selectedExpiry, setSelectedExpiry] = useState('')
  const [chain, setChain] = useState<LiveOptionChain | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function loadExpiries() {
    const underlying = underlyingInput.trim()
    if (!underlying) return
    setLoading(true)
    setError(null)
    setChain(null)
    setExpiries(null)
    try {
      const result = await getLiveOptionExpiries(underlying)
      setExpiries(result)
      if (result.length > 0) setSelectedExpiry(result[0])
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load expiries.')
    } finally {
      setLoading(false)
    }
  }

  async function loadChain(expiry: string) {
    const underlying = underlyingInput.trim()
    if (!underlying || !expiry) return
    setLoading(true)
    setError(null)
    try {
      setChain(await getLiveOptionChain(underlying, expiry))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load option chain.')
      setChain(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (selectedExpiry) loadChain(selectedExpiry)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExpiry])

  return (
    <Panel
      title="Live option chain"
      eyebrow="REAL OI / IV / LTP — VIA WHICHEVER BROKER IS CONFIGURED"
      actions={
        <div className="flex items-center gap-2">
          <input
            value={underlyingInput}
            onChange={(e) => setUnderlyingInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && loadExpiries()}
            placeholder="Broker instrument key, e.g. NSE_INDEX|Nifty 50"
            className="backtest-select w-64 font-mono text-xs"
          />
          <button onClick={loadExpiries} className="backtest-select" disabled={loading || !underlyingInput.trim()}>Load</button>
          {expiries && expiries.length > 0 && (
            <select value={selectedExpiry} onChange={(e) => setSelectedExpiry(e.target.value)} className="backtest-select">
              {expiries.map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
          )}
        </div>
      }
    >
      {!underlyingInput && expiries === null && (
        <GapNotice>Enter a broker instrument key (the format your configured broker's API expects — e.g. Upstox's `NSE_INDEX|Nifty 50`) and click Load. Zerodha has no option-chain endpoint at all and honestly reports so rather than returning a fabricated result.</GapNotice>
      )}
      {error && <GapNotice>{error}</GapNotice>}
      {chain && (
        <div className="overflow-x-auto mt-4">
          <p className="mb-2 text-[10px] text-muted-foreground">
            Broker: <span className="font-mono text-foreground">{chain.broker}</span> · {chain.underlying} · {chain.expiry}
            {chain.underlying_ltp !== null && <> · Spot: <span className="font-mono text-foreground">{chain.underlying_ltp}</span></>}
          </p>
          <table className="option-chain-table">
            <thead><tr><th>Call OI</th><th>Call IV</th><th>Call LTP</th><th>Strike</th><th>Put LTP</th><th>Put IV</th><th>Put OI</th></tr></thead>
            <tbody>
              {chain.entries.map((e) => (
                <tr key={e.strike} className={chain.atm_strike !== null && e.strike === chain.atm_strike ? 'atm-row' : undefined}>
                  <td className="mono">{e.call_oi ?? '—'}</td>
                  <td className="mono">{e.call_iv ?? '—'}</td>
                  <td className="mono">{e.call_ltp ?? '—'}</td>
                  <td className="strike-cell">{e.strike}{chain.atm_strike !== null && e.strike === chain.atm_strike && <span className="atm-tag">ATM</span>}</td>
                  <td className="mono">{e.put_ltp ?? '—'}</td>
                  <td className="mono">{e.put_iv ?? '—'}</td>
                  <td className="mono">{e.put_oi ?? '—'}</td>
                </tr>
              ))}
              {chain.entries.length === 0 && <tr><td colSpan={7} className="p-4 text-center text-xs text-muted-foreground">No entries returned for this underlying/expiry.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function OptionChainTab() {
  const [instruments, setInstruments] = useState<Instrument[]>([])
  const [underlying, setUnderlying] = useState('NIFTY')

  useEffect(() => { listInstruments().then(setInstruments) }, [])

  const options = instruments.filter((i) => i.instrument_type === 'option' && i.underlying_symbol === underlying)
  const underlyings = Array.from(new Set(instruments.filter((i) => i.instrument_type === 'option').map((i) => i.underlying_symbol).filter((s): s is string => Boolean(s))))

  return (
    <div className="flex flex-col gap-5">
      <LiveOptionChainPanel />
      <Panel title="Option instrument master" eyebrow={`${underlying} · STATIC INSTRUMENT MASTER`} actions={<select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="backtest-select">{underlyings.length > 0 ? underlyings.map((s) => <option key={s}>{s}</option>) : <option>{underlying}</option>}</select>}>
        <div className="overflow-x-auto mt-4"><table className="option-chain-table"><thead><tr><th>Symbol</th><th>Strike</th><th>Type</th><th>Expiry</th><th>Lot size</th><th>Tick size</th></tr></thead><tbody>{options.map((o) => <tr key={o.symbol}><td className="mono">{o.symbol}</td><td className="strike-cell">{o.strike_price}</td><td>{o.option_type}</td><td className="mono">{o.expiry_date}</td><td className="mono">{o.lot_size}</td><td className="mono">{o.tick_size}</td></tr>)}{options.length === 0 && <tr><td colSpan={6} className="p-4 text-center text-xs text-muted-foreground">No option instruments found for this underlying.</td></tr>}</tbody></table></div>
      </Panel>
    </div>
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
