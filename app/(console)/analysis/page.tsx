'use client'

import { Suspense, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { usePolling } from '@/lib/use-polling'
import { Bar, CartesianGrid, ComposedChart, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, AlertTriangle, Cable, CalendarDays, CheckCircle2, Clock3, Database, Globe2, LineChart as LineChartIcon, ListTree, Search, Sparkles, XCircle } from 'lucide-react'
import { IndicatorForm } from '@/components/analysis/indicator-form'
import { HolidaysTab } from '@/components/analysis/holidays-tab'
import { ChartContainer } from '@/components/ui/chart'
import { ErrorNotice } from '@/components/ui/error-notice'
import { TabList } from '@/components/ui/tab-list'
import { type AnalysisTab, type AnalysisView, isSymbol, viewFromQuery, viewToQuery } from '@/lib/analysis-view'
import { describeQuoteRefusals } from '@/lib/broker-quotes'
import { behindWords, pipelineLabel, statusLight } from '@/lib/datalake'
import { DEFAULT_PARAMS, type IndicatorParams, indicatorQuery, paramsOf, unfilled } from '@/lib/indicator-params'
import { section } from '@/lib/errors'
import { istDate } from '@/lib/paper-trading'
import { indiaStamp } from '@/lib/time'
import { useQuery } from '@/lib/use-query'
import {
  ApiError,
  type BrokerCircuitBreakerStatus,
  type BrokerCredentialStatus,
  type DatalakeStatus,
  type Instrument,
  type LiveOptionChain,
  type LlmProviderStatus,
  type MarketDataProvenanceEntry,
  LLM_PROVIDER_LABELS,
  getDatalakeStatus,
  getFreshness,
  getIndicators,
  getLiveOptionChain,
  getLiveOptionExpiries,
  getMarketPulse,
  getSystemVitals,
  listBrokerCircuitBreakerStatus,
  listBrokerCredentialStatus,
  listInstruments,
  listLlmProviderStatus,
  listProvenance,
} from '@/lib/api'

const TABS: { id: AnalysisTab; label: string; icon: typeof Activity }[] = [
  { id: 'pulse', label: 'Pulse', icon: Activity },
  { id: 'technical', label: 'Technical Indicators', icon: LineChartIcon },
  { id: 'freshness', label: 'Data Freshness', icon: Clock3 },
  { id: 'datalake', label: 'Datalake Status', icon: Database },
  { id: 'holidays', label: 'Holiday Calendar', icon: CalendarDays },
  { id: 'providers', label: 'Provider Status', icon: Cable },
  { id: 'chain', label: 'Live Option Chain', icon: ListTree },
]

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
  // The server answers 503 with its reason when no broker is connected: say that, not "Loading".
  const { data: pulse, error, refresh } = usePolling(getMarketPulse, { intervalMs: 30_000 })

  if (error && !pulse) return <div className="p-4"><ErrorNotice message={error} onRetry={() => void refresh()} /></div>
  if (!pulse) return <p className="p-4 text-xs text-muted-foreground">Loading market pulse…</p>

  return (
    <div className="pulse-grid">
      <p className="analysis-asof">As of {indiaStamp(pulse.as_of)} India time, refreshed every 30 seconds</p>
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

/** The share to look at: the instrument list, filtered as you type, and any symbol typed in full, since
 * the lake can hold bars for a share the list does not. */
function SymbolPicker({ instruments, symbol, onSymbol }: { instruments: Instrument[]; symbol: string | null; onSymbol: (symbol: string) => void }) {
  const [query, setQuery] = useState('')
  const typed = query.trim().toUpperCase()
  const filtered = instruments.filter((i) => i.symbol.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 50)
  const openable = typed !== '' && isSymbol(typed) && !instruments.some((i) => i.symbol === typed)
  return (
    <Panel title="Symbol" eyebrow="SEARCH" className="symbol-panel">
      <label className="symbol-search-box">
        <Search className="size-3.5" />
        <input
          aria-label="Search symbol"
          placeholder="Search NSE / BSE symbol..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && typed !== '' && isSymbol(typed)) onSymbol(typed) }}
        />
      </label>
      <div className="symbol-list">
        {openable && <button className="symbol-chip" onClick={() => onSymbol(typed)}>Open {typed} (not in the instrument list)</button>}
        {filtered.map((i) => <button key={i.symbol} className={i.symbol === symbol ? 'symbol-chip active' : 'symbol-chip'} onClick={() => onSymbol(i.symbol)}>{i.symbol}</button>)}
      </div>
    </Panel>
  )
}

function TechnicalTab({ symbol: chosen, onSymbol, params, onParams }: { symbol: string | null; onSymbol: (symbol: string) => void; params: IndicatorParams; onParams: (params: IndicatorParams) => void }) {
  const instruments = useQuery('instruments', listInstruments)
  const symbol = chosen ?? instruments.data?.[0]?.symbol ?? null
  const indicators = useQuery(
    `indicators:${symbol}:${indicatorQuery(params)}`,
    () => getIndicators(symbol as string, params),
    { enabled: symbol !== null },
  )
  const series = indicators.data?.symbol === symbol ? indicators.data : null
  const [overlays, setOverlays] = useState<OverlayId[]>(['sma', 'ema'])
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
  const bars = series?.dates.length ?? 0
  // What the server computed with, not what was asked for: while a new request is out they can differ.
  const used = series ? paramsOf(series) : params
  const short = series && hasData ? unfilled(used, bars) : []
  const loadError = instruments.error && !instruments.data
    ? `Could not load the instrument list: ${instruments.error}`
    : indicators.error
      ? `Could not load indicators for ${symbol}: ${indicators.error}`
      : null

  return (
    <div className="technical-layout">
      {loadError && <div className="col-span-full"><ErrorNotice message={loadError} onRetry={() => void (instruments.error ? instruments.refresh() : indicators.refresh())} /></div>}
      <SymbolPicker instruments={instruments.data ?? []} symbol={symbol} onSymbol={onSymbol} />
      <div className="indicator-settings">
        <Panel title="Indicator settings" eyebrow="PERIODS USED FOR EVERY CHART BELOW">
          {/* Keyed by the applied periods, so Apply and Reset leave the boxes showing what was applied. */}
          <IndicatorForm key={indicatorQuery(params)} applied={params} onApply={onParams} />
        </Panel>
      </div>
      <Panel
        title={`${symbol ?? '—'} · close · SMA(${used.smaWindow}) · EMA(${used.emaSpan}) · Bollinger(${used.bollingerWindow}, ${used.bollingerStd})`}
        eyebrow={hasData ? `REAL DAILY BARS FROM THE DATA LAKE · ${bars} BARS, ${series?.dates[0]} TO ${series?.dates[bars - 1]}` : 'REAL OHLCV FROM THE DATA LAKE'}
        className="indicator-chart-panel"
        actions={<div className="overlay-toggles">{OVERLAYS.map((o) => <button key={o.id} aria-pressed={overlays.includes(o.id)} className={overlays.includes(o.id) ? 'active' : ''} onClick={() => toggleOverlay(o.id)}>{o.label}</button>)}</div>}
      >
        {short.length > 0 && (
          <GapNotice>
            {short.map((w) => `${w.name} needs ${w.needs} bars`).join('; ')}, and {symbol} has {bars} in the last {used.lookbackDays} days, so {short.length === 1 ? 'it is' : 'they are'} not drawn. Lengthen the history or shorten the period.
          </GapNotice>
        )}
        {!hasData ? <p className="p-4 text-xs text-muted-foreground">{indicators.loading && symbol ? 'Loading…' : 'No ingested OHLCV history for this symbol yet — see Data Freshness. Real indicators need real bars from the data lake, not a live quote, so nothing is shown here until the pipeline has actually ingested this symbol.'}</p> : (
          <ChartContainer config={{ close: { label: 'Close', color: 'var(--chart-1)' } }} className="h-[300px] w-full px-2">
            <LineChart data={chartData} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
              <CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} />
              <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} minTickGap={40} />
              <YAxis tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} domain={['auto', 'auto']} />
              <Tooltip />
              <Line type="monotone" dataKey="close" stroke="#e2e8f0" dot={false} strokeWidth={1.5} name="Close" />
              {overlays.includes('sma') && <Line type="monotone" dataKey="sma" stroke="#22d3ee" dot={false} strokeWidth={1.25} name={`SMA(${used.smaWindow})`} connectNulls={false} />}
              {overlays.includes('ema') && <Line type="monotone" dataKey="ema" stroke="#c084fc" dot={false} strokeWidth={1.25} name={`EMA(${used.emaSpan})`} connectNulls={false} />}
              {overlays.includes('bollinger') && <Line type="monotone" dataKey="bollinger_upper" stroke="#fbbf24" dot={false} strokeWidth={1} strokeDasharray="3 3" name="Bollinger Upper" connectNulls={false} />}
              {overlays.includes('bollinger') && <Line type="monotone" dataKey="bollinger_lower" stroke="#fbbf24" dot={false} strokeWidth={1} strokeDasharray="3 3" name="Bollinger Lower" connectNulls={false} />}
            </LineChart>
          </ChartContainer>
        )}
      </Panel>
      <div className="sub-indicators">
        <Panel title={`RSI(${used.rsiPeriod})`} eyebrow="MOMENTUM">
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
        <Panel title={`MACD(${used.macdFast}, ${used.macdSlow}, ${used.macdSignal})`} eyebrow="TREND">
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

function FreshnessTab({ symbol: chosen, onSymbol }: { symbol: string | null; onSymbol: (symbol: string) => void }) {
  const instruments = useQuery('instruments', listInstruments)
  const symbol = chosen ?? instruments.data?.[0]?.symbol ?? null
  const freshness = useQuery(`freshness:${symbol}`, () => getFreshness(symbol as string), { enabled: symbol !== null })
  const records = freshness.data ?? []
  const loadError = instruments.error && !instruments.data
    ? `Could not load the instrument list: ${instruments.error}`
    : freshness.error
      ? `Could not load the data freshness for ${symbol}: ${freshness.error}`
      : null

  return (
    <div className="technical-layout">
      {loadError && <div className="col-span-full"><ErrorNotice message={loadError} onRetry={() => void (instruments.error ? instruments.refresh() : freshness.refresh())} /></div>}
      <SymbolPicker instruments={instruments.data ?? []} symbol={symbol} onSymbol={onSymbol} />
      <Panel title={`${symbol ?? '—'} · dataset freshness`} eyebrow="DATA PIPELINE HEALTH">
        <div className="overflow-x-auto"><table className="analysis-table"><thead><tr><th>Data type</th><th>Data date</th><th>Row count</th><th>Ingested at (India time)</th></tr></thead><tbody>{records.map((r, i) => <tr key={i}><td>{r.data_type}</td><td className="mono">{r.data_date}</td><td className="mono">{r.row_count.toLocaleString('en-IN')}</td><td className="mono">{indiaStamp(r.ingested_at)}</td></tr>)}{records.length === 0 && <tr><td colSpan={4} className="p-4 text-center text-xs text-muted-foreground">No freshness records for this symbol.</td></tr>}</tbody></table></div>
      </Panel>
    </div>
  )
}

function DatalakeStatusTab() {
  const { data: lake, error, refresh } = usePolling(
    async () => {
      const [status, runs] = await Promise.all([getDatalakeStatus(), listProvenance()])
      return { status, runs }
    },
    { intervalMs: 30_000 },
  )
  const status: DatalakeStatus | null = lake?.status ?? null
  const runs: MarketDataProvenanceEntry[] = lake?.runs ?? []

  if (error && !status) return <div className="p-4"><ErrorNotice message={`Could not load the data lake status: ${error}`} onRetry={() => void refresh()} /></div>
  if (!status) return <p className="p-4 text-xs text-muted-foreground">Loading data lake status…</p>

  const behind = behindWords(status.daily_data_behind_trading_days)

  return (
    <div className="technical-layout">
      <Panel title="Lake coverage" eyebrow="REAL SYMBOL COUNTS FROM THE DATASET FRESHNESS TABLE" className="symbol-panel">
        <div className="flex flex-col gap-3 p-1 text-xs">
          <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">Symbols with daily data</span><strong className="mono">{status.symbols_with_daily_data.toLocaleString('en-IN')}</strong></div>
          <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">Most recent daily date</span><strong className="mono">{status.most_recent_daily_data_date ?? '—'}</strong></div>
          <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">Backtests require daily data for</span><strong className="mono whitespace-nowrap">{status.required_daily_data_date}</strong></div>
          <div className="flex items-center justify-between gap-2" data-testid="lake-behind"><span className="text-muted-foreground">Newest daily data is</span><strong className={behind.tone === 'ok' ? 'gain' : behind.tone === 'bad' ? 'loss' : 'text-muted-foreground'}>{behind.text}</strong></div>
          <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">Symbols with intraday data</span><strong className="mono">{status.symbols_with_intraday_data.toLocaleString('en-IN')}</strong></div>
          <div className="flex items-center justify-between gap-2"><span className="text-muted-foreground">Most recent intraday date</span><strong className="mono">{status.most_recent_intraday_data_date ?? '—'}</strong></div>
          <div className="flex items-center justify-between gap-2 border-t border-white/8 pt-3"><span className="text-muted-foreground">As of (India time)</span><strong className="mono">{indiaStamp(status.as_of)}</strong></div>
        </div>
      </Panel>
      <Panel title="Pipeline health" eyebrow="EVERY REAL PIPELINE, INCLUDING ONE THAT HAS NEVER RUN">
        <div className="provider-status-list">
          {status.pipelines.map((p) => (
            <div className="provider-status-row" key={p.pipeline}>
              <span className={`provider-status-dot ${statusLight(p.last_run_status)}`} />
              <div className="min-w-0 flex-1">
                <strong>{pipelineLabel(p.pipeline)}</strong>
                <small>
                  {p.last_run_status
                    ? `${p.last_run_status} · ${indiaStamp(p.last_run_at)}`
                    : 'never run'}
                  {p.last_error && ` · ${p.last_error}`}
                </small>
                {p.last_run_status === 'failed' && p.last_success_at && (
                  <small className="block text-emerald-300/80">last succeeded {indiaStamp(p.last_success_at)}</small>
                )}
              </div>
              {p.last_run_status === 'success' ? <CheckCircle2 className="size-3.5 shrink-0 text-emerald-300" /> : p.last_run_status === 'failed' ? <XCircle className="size-3.5 shrink-0 text-rose-300" /> : null}
            </div>
          ))}
        </div>
      </Panel>
      <Panel title="Recent runs" eyebrow={runs.length > 0 ? `RAW PROVENANCE LOG, NEWEST FIRST · THE LATEST ${runs.length}, IN INDIA TIME` : 'RAW PROVENANCE LOG, NEWEST FIRST, IN INDIA TIME'} className="sector-panel">
        <div className="overflow-x-auto"><table className="analysis-table"><thead><tr><th>Pipeline</th><th>Source</th><th>Status</th><th>Symbols</th><th>Rows</th><th>Completed at</th></tr></thead><tbody>{runs.map((r) => <tr key={r.id}><td>{pipelineLabel(r.pipeline)}</td><td className="mono">{r.source}</td><td>{r.status}{r.error_message && <small className="block text-rose-300/80" style={{ whiteSpace: 'normal' }}>{r.error_message}</small>}</td><td className="mono">{r.symbols_processed}</td><td className="mono">{r.rows_ingested.toLocaleString('en-IN')}</td><td className="mono">{indiaStamp(r.completed_at)}</td></tr>)}{runs.length === 0 && <tr><td colSpan={6} className="p-4 text-center text-xs text-muted-foreground">No pipeline runs recorded yet.</td></tr>}</tbody></table></div>
      </Panel>
    </div>
  )
}

function ProvidersTab() {
  const statuses = usePolling(
    async () => {
      const [providers, brokers] = await Promise.all([listLlmProviderStatus(), listBrokerCredentialStatus()])
      return { providers, brokers }
    },
    { intervalMs: 60_000 },
  )
  const circuit = usePolling(listBrokerCircuitBreakerStatus, { intervalMs: 10_000 })
  // Whether a provider has actually answered lately is the vitals' to say; credentials being saved says only that a key exists.
  const health = usePolling(() => section(getSystemVitals, 'the provider health'), { intervalMs: 60_000 })
  const providers: LlmProviderStatus[] = statuses.data?.providers ?? []
  const brokers: BrokerCredentialStatus[] = statuses.data?.brokers ?? []
  const breakers: BrokerCircuitBreakerStatus[] = circuit.data ?? []
  const loadError = statuses.error ?? circuit.error
  const vitals = health.data?.data ?? null
  const healthError = health.error ?? health.data?.error ?? null

  const breakerByBroker = Object.fromEntries(breakers.map((b) => [b.broker, b]))
  const healthByProvider = Object.fromEntries((vitals?.llm.provider_health ?? []).map((h) => [h.provider, h]))

  return (
    <div className="technical-layout">
      {loadError && (
        <div className="col-span-full">
          <ErrorNotice message={`Could not load the provider status: ${loadError}`} onRetry={() => void Promise.all([statuses.refresh(), circuit.refresh()])} />
        </div>
      )}
      <Panel title="LLM providers" eyebrow="CREDENTIAL + FALLBACK-CHAIN STATUS + WHETHER IT ANSWERED">
        {healthError && <div className="p-3"><ErrorNotice message={healthError} onRetry={() => void health.refresh()} /></div>}
        <div className="provider-status-list">
          {providers.map((p) => {
            const answered = healthByProvider[p.provider]
            const active = vitals?.llm.active_provider === p.provider
            return (
              <div className="provider-status-row" key={p.provider}>
                <span className={`provider-status-dot ${p.configured ? (answered?.last_failure_at && (!answered.last_success_at || answered.last_failure_at > answered.last_success_at) ? 'red' : p.in_fallback_order ? 'green' : 'amber') : 'gray'}`} />
                <div className="min-w-0 flex-1">
                  <strong>{LLM_PROVIDER_LABELS[p.provider as keyof typeof LLM_PROVIDER_LABELS] ?? p.provider}{active && <span className="atm-tag">ACTIVE</span>}</strong>
                  <small>{p.configured ? (p.in_fallback_order ? 'configured · in fallback chain' : 'configured · not in fallback chain') : 'not configured'}</small>
                  {p.configured && vitals && (
                    <small data-testid={`llm-health-${p.provider}`}>
                      {answered?.last_success_at ? `last answered ${indiaStamp(answered.last_success_at)}` : 'has not answered since the backend started'}
                      {answered?.last_failure_at && ` · last failed ${indiaStamp(answered.last_failure_at)}`}
                      {answered?.served_as_fallback && ' · has served as the fallback'}
                    </small>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </Panel>
      <Panel title="Broker connections" eyebrow="OAUTH TOKEN STATUS + LIVE CIRCUIT-BREAKER STATE">
        <div className="provider-status-list">
          {brokers.map((b) => {
            const breaker = breakerByBroker[b.broker]
            const tripped = breaker?.state === 'open'
            const rejected = Boolean(b.feed_rejected_at) // the broker refused the token: the clock says valid, the broker says no
            return (
              <div className="provider-status-row" key={b.broker}>
                <span className={`provider-status-dot ${tripped || rejected ? 'red' : b.token_status === 'valid' ? 'green' : b.token_status === 'expired' ? 'amber' : 'gray'}`} />
                <div className="min-w-0 flex-1">
                  <strong className="capitalize">{b.broker}</strong>
                  <small>{rejected ? `${b.broker} refused the stored token${b.feed_rejection_status ? ` (HTTP ${b.feed_rejection_status})` : ''}, so live quotes are not flowing — reconnect in Settings` : b.token_status === 'valid' ? 'token valid (by the clock)' : b.token_status === 'expired' ? 'token expired — reconnect in Settings' : 'not connected'}</small>
                  {describeQuoteRefusals(b) && <small data-testid={`quote-refusals-${b.broker}`}>{describeQuoteRefusals(b)}</small>}
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

function IvCell({ real, computed }: { real: number | null; computed: number | null }) {
  if (real !== null) return <td className="mono">{real}</td>
  if (computed !== null) {
    return (
      <td className="mono" title="Computed via Black-Scholes from LTP -- not reported by this broker">
        {computed.toFixed(1)}
        <span className="atm-tag">CALC</span>
      </td>
    )
  }
  return <td className="mono">—</td>
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
                  <IvCell real={e.call_iv} computed={e.call_iv_computed} />
                  <td className="mono">{e.call_ltp ?? '—'}</td>
                  <td className="strike-cell">{e.strike}{chain.atm_strike !== null && e.strike === chain.atm_strike && <span className="atm-tag">ATM</span>}</td>
                  <td className="mono">{e.put_ltp ?? '—'}</td>
                  <IvCell real={e.put_iv} computed={e.put_iv_computed} />
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
  const instrumentList = useQuery('instruments', listInstruments)
  const instruments = instrumentList.data ?? []
  const [underlying, setUnderlying] = useState('NIFTY')
  const loadError = instrumentList.error && !instrumentList.data ? `Could not load the instrument list: ${instrumentList.error}` : null

  const options = instruments.filter((i) => i.instrument_type === 'option' && i.underlying_symbol === underlying)
  const underlyings = Array.from(new Set(instruments.filter((i) => i.instrument_type === 'option').map((i) => i.underlying_symbol).filter((s): s is string => Boolean(s))))

  return (
    <div className="flex flex-col gap-5">
      {loadError && <ErrorNotice message={loadError} />}
      <LiveOptionChainPanel />
      <Panel title="Option instrument master" eyebrow={`${underlying} · STATIC INSTRUMENT MASTER`} actions={<select value={underlying} onChange={(e) => setUnderlying(e.target.value)} className="backtest-select">{underlyings.length > 0 ? underlyings.map((s) => <option key={s}>{s}</option>) : <option>{underlying}</option>}</select>}>
        <div className="overflow-x-auto mt-4"><table className="option-chain-table"><thead><tr><th>Symbol</th><th>Strike</th><th>Type</th><th>Expiry</th><th>Lot size</th><th>Tick size</th></tr></thead><tbody>{options.map((o) => <tr key={o.symbol}><td className="mono">{o.symbol}</td><td className="strike-cell">{o.strike_price}</td><td>{o.option_type}</td><td className="mono">{o.expiry_date}</td><td className="mono">{o.lot_size}</td><td className="mono">{o.tick_size}</td></tr>)}{options.length === 0 && <tr><td colSpan={6} className="p-4 text-center text-xs text-muted-foreground">No option instruments found for this underlying.</td></tr>}</tbody></table></div>
      </Panel>
    </div>
  )
}

function MarketAnalysis() {
  const thisYear = istDate(new Date()).year
  const search = useSearchParams()
  const [view, setView] = useState<AnalysisView>(() => viewFromQuery(search.toString(), thisYear))
  const [params, setParams] = useState<IndicatorParams>(DEFAULT_PARAMS)
  const active = TABS.find((t) => t.id === view.tab)

  // The address follows the view, so a reload or a shared link opens the same one.
  useEffect(() => {
    window.history.replaceState(null, '', `${window.location.pathname}${viewToQuery(view, thisYear)}`)
  }, [view, thisYear])

  return (
    <>
      <main className="analysis-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="analysis-header">
          <div><p className="eyebrow">TRADINGOS // MARKET INTELLIGENCE</p><h1>Market Analysis</h1><p>Volatility, sector breadth, data health, the holiday calendar and derivatives instrument master — real backend state, honest gaps labeled where no live feed exists.</p></div>
          <div className="analysis-live-chip"><Sparkles className="size-3.5" /> {active?.label}</div>
        </header>
        <TabList label="Market analysis views" className="analysis-tabs">{TABS.map((t) => { const Icon = t.icon; return <button key={t.id} role="tab" aria-selected={view.tab === t.id} tabIndex={view.tab === t.id ? 0 : -1} className={view.tab === t.id ? 'active' : ''} onClick={() => setView((v) => ({ ...v, tab: t.id }))}><Icon className="size-3.5" /> {t.label}</button> })}</TabList>
        {view.tab === 'pulse' && <PulseTab />}
        {view.tab === 'technical' && <TechnicalTab symbol={view.symbol} onSymbol={(symbol) => setView((v) => ({ ...v, symbol }))} params={params} onParams={setParams} />}
        {view.tab === 'freshness' && <FreshnessTab symbol={view.symbol} onSymbol={(symbol) => setView((v) => ({ ...v, symbol }))} />}
        {view.tab === 'datalake' && <DatalakeStatusTab />}
        {view.tab === 'holidays' && <HolidaysTab year={view.year} onYear={(year) => setView((v) => ({ ...v, year }))} />}
        {view.tab === 'providers' && <ProvidersTab />}
        {view.tab === 'chain' && <OptionChainTab />}
      </main>
    </>
  )
}

export default function MarketAnalysisPage() {
  return (
    <Suspense fallback={<main className="analysis-page mx-auto w-full max-w-[1700px] p-8 text-sm text-muted-foreground">Loading market analysis…</main>}>
      <MarketAnalysis />
    </Suspense>
  )
}
