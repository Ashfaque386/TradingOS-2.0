'use client'

import { useMemo, useState } from 'react'
import { Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine, Tooltip, XAxis, YAxis } from 'recharts'
import { Activity, AlertTriangle, Bot, Cable, Check, ChevronDown, Clock3, Globe2, LineChart as LineChartIcon, ListTree, Radio, Search, Sparkles } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { ChartContainer } from '@/components/ui/chart'

const TABS = [
  { id: 'pulse', label: 'Pulse', icon: Activity },
  { id: 'technical', label: 'Technical Indicators', icon: LineChartIcon },
  { id: 'freshness', label: 'Data Freshness', icon: Clock3 },
  { id: 'providers', label: 'Provider Status', icon: Cable },
  { id: 'chain', label: 'Live Option Chain', icon: ListTree },
] as const

type TabId = (typeof TABS)[number]['id']

const vix = 13.42
const vixChange = -0.68

const sectors = [
  { name: 'Nifty Bank', change: 1.42 }, { name: 'Nifty IT', change: -0.86 }, { name: 'Nifty Auto', change: 2.14 },
  { name: 'Nifty Pharma', change: 0.38 }, { name: 'Nifty FMCG', change: -0.24 }, { name: 'Nifty Metal', change: 3.05 },
  { name: 'Nifty Energy', change: 1.08 }, { name: 'Nifty Realty', change: -1.62 }, { name: 'Nifty PSU Bank', change: 2.61 },
  { name: 'Nifty Media', change: -2.38 }, { name: 'Nifty Infra', change: 0.72 }, { name: 'Nifty Cons. Durables', change: -0.44 },
]

const globalIndices = [
  { name: 'Dow Jones', change: 0.34 }, { name: 'Nasdaq', change: -0.52 }, { name: 'S&P 500', change: 0.11 },
  { name: 'Nikkei 225', change: 1.24 }, { name: 'Hang Seng', change: -0.88 }, { name: 'FTSE 100', change: 0.27 },
  { name: 'DAX', change: 0.63 }, { name: 'Shanghai Comp.', change: -0.31 },
]

const symbols = ['NIFTY 50', 'BANKNIFTY', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK']

function buildSeries() {
  const rows: { t: string; close: number; sma: number; ema: number; upper: number; lower: number; rsi: number; atr: number; macd: number; signal: number }[] = []
  let price = 24180
  let smaSeed = price
  let emaSeed = price
  for (let i = 0; i < 30; i++) {
    const wave = Math.sin(i / 3.2) * 95 + Math.sin(i / 1.3) * 32
    price = 24180 + wave + i * 6.4
    smaSeed = smaSeed + (price - smaSeed) * 0.22
    emaSeed = emaSeed + (price - emaSeed) * 0.32
    const rsi = 50 + Math.sin(i / 2.6) * 24 + (i > 20 ? 8 : 0)
    const atr = 62 + Math.abs(Math.sin(i / 4)) * 34
    const macd = Math.sin(i / 3) * 28
    rows.push({
      t: `D${i + 1}`,
      close: Math.round(price),
      sma: Math.round(smaSeed),
      ema: Math.round(emaSeed),
      upper: Math.round(smaSeed + atr * 1.6),
      lower: Math.round(smaSeed - atr * 1.6),
      rsi: Math.round(Math.max(8, Math.min(92, rsi))),
      atr: Math.round(atr),
      macd: Math.round(macd),
      signal: Math.round(macd * 0.7 + Math.sin(i / 5) * 6),
    })
  }
  return rows
}
const series = buildSeries()

const datasets = [
  { name: 'OHLCV Daily (NSE + BSE)', updated: '2 min ago', fresh: true },
  { name: 'Instrument Master', updated: '41 min ago', fresh: true },
  { name: 'Corporate Actions', updated: '3 hr ago', fresh: true },
  { name: 'News Feed', updated: '1 min ago', fresh: true },
  { name: 'Index OHLCV', updated: '2 min ago', fresh: true },
  { name: 'Options Chain Snapshot', updated: '18 min ago', fresh: false },
  { name: 'Economic Calendar', updated: '1 day ago', fresh: false },
]

const llmProviders = [
  { name: 'OpenAI', status: 'healthy', lastFailure: '—' },
  { name: 'Anthropic', status: 'healthy', lastFailure: '—' },
  { name: 'Google Gemini', status: 'healthy', lastFailure: '—' },
  { name: 'xAI Grok', status: 'degraded', lastFailure: '14 min ago' },
  { name: 'Mistral', status: 'healthy', lastFailure: '—' },
  { name: 'DeepSeek', status: 'healthy', lastFailure: '2 days ago' },
  { name: 'Groq', status: 'down', lastFailure: '3 min ago' },
]

const brokerConnections = [
  { name: 'Zerodha Kite', status: 'healthy', lastFailure: '—' },
  { name: 'Angel One SmartAPI', status: 'healthy', lastFailure: '6 hr ago' },
]

const optionChainSymbols = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'RELIANCE']
const atmStrike = 24500
const optionChain = Array.from({ length: 11 }, (_, i) => {
  const strike = atmStrike - 500 + i * 100
  const dist = Math.abs(strike - atmStrike) / 100
  return {
    strike,
    callOi: Math.round(3200000 - dist * 210000 + (i % 2 === 0 ? 40000 : 0)),
    callIv: (12.4 + dist * 0.28).toFixed(2),
    callLtp: Math.max(2, 420 - dist * 62 + (strike < atmStrike ? dist * 4 : 0)).toFixed(2),
    putOi: Math.round(2800000 - dist * 190000 + (i % 2 === 1 ? 36000 : 0)),
    putIv: (12.9 + dist * 0.24).toFixed(2),
    putLtp: Math.max(2, 60 + dist * 58 - (strike > atmStrike ? dist * 3 : 0)).toFixed(2),
  }
})

function Panel({ title, eyebrow, children, actions, className = '' }: { title: string; eyebrow?: string; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={`analysis-panel ${className}`}>
      <div className="analysis-panel-head">
        <div>
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h2>{title}</h2>
        </div>
        {actions}
      </div>
      {children}
    </section>
  )
}

function VixGauge() {
  const pct = Math.min(Math.max((vix - 10) / (35 - 10), 0), 1)
  const angle = pct * 180 - 90
  const zone = vix < 15 ? 'Calm' : vix < 22 ? 'Watchful' : 'Elevated'
  return (
    <div className="vix-gauge-wrap">
      <div className="vix-gauge">
        <div className="vix-gauge-arc" />
        <div className="vix-gauge-cover" />
        <div className="vix-gauge-needle" style={{ transform: `translateX(-50%) rotate(${angle}deg)` }} />
        <div className="vix-gauge-value">
          <strong>{vix}</strong>
          <span>India VIX</span>
        </div>
      </div>
      <div className="vix-gauge-legend">
        <span><i className="dot dot-green" /> Calm &lt;15</span>
        <span><i className="dot dot-amber" /> Watchful 15–22</span>
        <span><i className="dot dot-red" /> Elevated &gt;22</span>
      </div>
      <p className="vix-gauge-note">
        <span className={vixChange < 0 ? 'gain' : 'loss'}>{vixChange > 0 ? '+' : ''}{vixChange}</span> vs previous close · Regime: <strong>{zone}</strong>
      </p>
    </div>
  )
}

function ChangeTile({ name, change }: { name: string; change: number }) {
  const intensity = Math.min(Math.abs(change) / 3.5, 1)
  const positive = change >= 0
  return (
    <div
      className="sector-tile"
      style={{
        background: positive
          ? `rgba(52, 211, 153, ${0.08 + intensity * 0.34})`
          : `rgba(251, 113, 133, ${0.08 + intensity * 0.34})`,
        borderColor: positive ? `rgba(52,211,153,${0.25 + intensity * 0.4})` : `rgba(251,113,133,${0.25 + intensity * 0.4})`,
      }}
    >
      <span>{name}</span>
      <strong className={positive ? 'gain' : 'loss'}>{positive ? '+' : ''}{change.toFixed(2)}%</strong>
    </div>
  )
}

function PulseTab() {
  return (
    <div className="pulse-grid">
      <Panel title="India VIX" eyebrow="VOLATILITY GAUGE">
        <VixGauge />
      </Panel>
      <Panel title="NSE sector indices" eyebrow="DAY CHANGE HEATMAP" className="sector-panel">
        <div className="sector-grid">
          {sectors.map((s) => <ChangeTile key={s.name} name={s.name} change={s.change} />)}
        </div>
      </Panel>
      <Panel title="Global indices" eyebrow="OVERNIGHT & LIVE FEED" className="global-panel">
        <div className="global-strip">
          {globalIndices.map((g) => (
            <div className="global-chip" key={g.name}>
              <Globe2 className="size-3.5" />
              <span>{g.name}</span>
              <strong className={g.change >= 0 ? 'gain' : 'loss'}>{g.change >= 0 ? '+' : ''}{g.change.toFixed(2)}%</strong>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  )
}

function TechnicalTab() {
  const [symbol, setSymbol] = useState('NIFTY 50')
  const [query, setQuery] = useState('')
  const [overlays, setOverlays] = useState<Record<string, boolean>>({ sma: true, ema: true, bollinger: true })
  const [sub, setSub] = useState<Record<string, boolean>>({ rsi: true, atr: false, macd: true })
  const filtered = query ? symbols.filter((s) => s.toLowerCase().includes(query.toLowerCase())) : symbols
  const toggleOverlay = (key: string) => setOverlays((p) => ({ ...p, [key]: !p[key] }))
  const toggleSub = (key: string) => setSub((p) => ({ ...p, [key]: !p[key] }))
  return (
    <div className="technical-layout">
      <Panel title="Symbol" eyebrow="SEARCH" className="symbol-panel">
        <label className="symbol-search-box">
          <Search className="size-3.5" />
          <input placeholder="Search NSE / BSE symbol..." value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <div className="symbol-list">
          {filtered.map((s) => (
            <button key={s} className={s === symbol ? 'symbol-chip active' : 'symbol-chip'} onClick={() => setSymbol(s)}>{s}</button>
          ))}
        </div>
      </Panel>
      <Panel title={`${symbol} · overlay chart`} eyebrow="SMA · EMA · BOLLINGER BANDS" className="indicator-chart-panel"
        actions={
          <div className="overlay-toggles">
            <button className={overlays.sma ? 'active' : ''} onClick={() => toggleOverlay('sma')}>SMA 20</button>
            <button className={overlays.ema ? 'active' : ''} onClick={() => toggleOverlay('ema')}>EMA 50</button>
            <button className={overlays.bollinger ? 'active' : ''} onClick={() => toggleOverlay('bollinger')}>Bollinger</button>
          </div>
        }
      >
        <ChartContainer
          config={{
            close: { label: 'Close', color: 'var(--chart-1)' },
            sma: { label: 'SMA 20', color: 'var(--chart-2)' },
            ema: { label: 'EMA 50', color: 'var(--chart-3)' },
            upper: { label: 'Bollinger Upper', color: 'var(--chart-4)' },
            lower: { label: 'Bollinger Lower', color: 'var(--chart-4)' },
          }}
          className="h-[260px] w-full px-2"
        >
          <ComposedChart data={series} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
            <CartesianGrid stroke="rgba(148,163,184,.1)" vertical={false} />
            <XAxis dataKey="t" tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#64748b', fontSize: 10 }} axisLine={false} tickLine={false} domain={['auto', 'auto']} />
            <Tooltip contentStyle={{ background: 'rgba(2,6,16,.95)', border: '1px solid rgba(103,232,249,.2)', borderRadius: 8, fontSize: 11 }} />
            {overlays.bollinger && <Area type="monotone" dataKey="upper" stroke="none" fill="var(--color-upper)" fillOpacity={0.06} />}
            {overlays.bollinger && <Area type="monotone" dataKey="lower" stroke="none" fill="var(--color-lower)" fillOpacity={0.06} />}
            <Line type="monotone" dataKey="close" stroke="var(--color-close)" strokeWidth={2.4} dot={false} />
            {overlays.sma && <Line type="monotone" dataKey="sma" stroke="var(--color-sma)" strokeWidth={1.4} dot={false} strokeDasharray="4 3" />}
            {overlays.ema && <Line type="monotone" dataKey="ema" stroke="var(--color-ema)" strokeWidth={1.4} dot={false} strokeDasharray="2 2" />}
          </ComposedChart>
        </ChartContainer>
      </Panel>
      <div className="sub-indicators">
        <Panel title="RSI (14)" eyebrow="MOMENTUM" actions={<button className={sub.rsi ? 'sub-toggle active' : 'sub-toggle'} onClick={() => toggleSub('rsi')}>{sub.rsi ? 'On' : 'Off'}</button>}>
          {sub.rsi && (
            <ChartContainer config={{ rsi: { label: 'RSI', color: 'var(--chart-2)' } }} className="h-[120px] w-full px-1">
              <ComposedChart data={series} margin={{ left: -18, right: 4, top: 8, bottom: 0 }}>
                <XAxis dataKey="t" hide />
                <YAxis hide domain={[0, 100]} />
                <ReferenceLine y={70} stroke="#fb7185" strokeDasharray="3 3" />
                <ReferenceLine y={30} stroke="#34d399" strokeDasharray="3 3" />
                <Line type="monotone" dataKey="rsi" stroke="var(--color-rsi)" strokeWidth={1.8} dot={false} />
              </ComposedChart>
            </ChartContainer>
          )}
        </Panel>
        <Panel title="ATR (14)" eyebrow="VOLATILITY" actions={<button className={sub.atr ? 'sub-toggle active' : 'sub-toggle'} onClick={() => toggleSub('atr')}>{sub.atr ? 'On' : 'Off'}</button>}>
          {sub.atr && (
            <ChartContainer config={{ atr: { label: 'ATR', color: 'var(--chart-4)' } }} className="h-[120px] w-full px-1">
              <ComposedChart data={series} margin={{ left: -18, right: 4, top: 8, bottom: 0 }}>
                <XAxis dataKey="t" hide />
                <YAxis hide />
                <Area type="monotone" dataKey="atr" stroke="var(--color-atr)" fill="var(--color-atr)" fillOpacity={0.22} />
              </ComposedChart>
            </ChartContainer>
          )}
        </Panel>
        <Panel title="MACD (12,26,9)" eyebrow="TREND" actions={<button className={sub.macd ? 'sub-toggle active' : 'sub-toggle'} onClick={() => toggleSub('macd')}>{sub.macd ? 'On' : 'Off'}</button>}>
          {sub.macd && (
            <ChartContainer config={{ macd: { label: 'MACD', color: 'var(--chart-1)' }, signal: { label: 'Signal', color: 'var(--chart-3)' } }} className="h-[120px] w-full px-1">
              <BarChart data={series} margin={{ left: -18, right: 4, top: 8, bottom: 0 }}>
                <XAxis dataKey="t" hide />
                <YAxis hide />
                <ReferenceLine y={0} stroke="rgba(148,163,184,.3)" />
                <Bar dataKey="macd" radius={[2, 2, 0, 0]}>
                  {series.map((row, i) => <Cell key={i} fill={row.macd >= 0 ? '#34d399' : '#fb7185'} />)}
                </Bar>
                <Line type="monotone" dataKey="signal" stroke="var(--color-signal)" strokeWidth={1.4} dot={false} />
              </BarChart>
            </ChartContainer>
          )}
        </Panel>
      </div>
    </div>
  )
}

function FreshnessTab() {
  return (
    <Panel title="Tracked dataset freshness" eyebrow="DATA PIPELINE HEALTH">
      <div className="overflow-x-auto">
        <table className="analysis-table">
          <thead>
            <tr><th>Dataset</th><th>Last updated</th><th>Status</th></tr>
          </thead>
          <tbody>
            {datasets.map((d) => (
              <tr key={d.name}>
                <td>{d.name}</td>
                <td className="mono">{d.updated}</td>
                <td>
                  <span className={d.fresh ? 'fresh-badge' : 'stale-badge'}>
                    {d.fresh ? <Check className="size-3" /> : <AlertTriangle className="size-3" />}
                    {d.fresh ? 'Fresh' : 'Stale'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function ProviderCard({ name, status, lastFailure, icon: Icon }: { name: string; status: string; lastFailure: string; icon: any }) {
  return (
    <div className={`provider-card provider-${status}`}>
      <div className="provider-card-top">
        <Icon className="size-4" />
        <span className={`provider-dot dot-${status}`} />
      </div>
      <h3>{name}</h3>
      <p className="provider-status-label">{status.toUpperCase()}</p>
      <p className="provider-failure">Last failure: {lastFailure}</p>
    </div>
  )
}

function ProvidersTab() {
  return (
    <div className="providers-layout">
      <Panel title="LLM providers" eyebrow="7 GATEWAYS · AGENT REASONING">
        <div className="provider-grid">
          {llmProviders.map((p) => <ProviderCard key={p.name} {...p} icon={Bot} />)}
        </div>
      </Panel>
      <Panel title="Broker connections" eyebrow="EXECUTION GATEWAYS">
        <div className="provider-grid provider-grid-broker">
          {brokerConnections.map((p) => <ProviderCard key={p.name} {...p} icon={Radio} />)}
        </div>
      </Panel>
    </div>
  )
}

function OptionChainTab() {
  const [symbol, setSymbol] = useState('NIFTY')
  return (
    <Panel
      title="Live option chain"
      eyebrow={`${symbol} · WEEKLY EXPIRY`}
      actions={
        <div className="chain-symbol-select">
          <ChevronDown className="size-3.5" />
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {optionChainSymbols.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
      }
    >
      <div className="overflow-x-auto">
        <table className="option-chain-table">
          <thead>
            <tr>
              <th colSpan={3} className="calls-head">CALLS</th>
              <th>STRIKE</th>
              <th colSpan={3} className="puts-head">PUTS</th>
            </tr>
            <tr>
              <th>OI</th><th>IV%</th><th>LTP</th><th /><th>LTP</th><th>IV%</th><th>OI</th>
            </tr>
          </thead>
          <tbody>
            {optionChain.map((row) => {
              const isAtm = row.strike === atmStrike
              return (
                <tr key={row.strike} className={isAtm ? 'atm-row' : ''}>
                  <td className="mono">{row.callOi.toLocaleString('en-IN')}</td>
                  <td className="mono">{row.callIv}</td>
                  <td className="mono call-ltp">₹{row.callLtp}</td>
                  <td className="strike-cell">{row.strike}{isAtm && <span className="atm-tag">ATM</span>}</td>
                  <td className="mono put-ltp">₹{row.putLtp}</td>
                  <td className="mono">{row.putIv}</td>
                  <td className="mono">{row.putOi.toLocaleString('en-IN')}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
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
          <div>
            <p className="eyebrow">TRADINGOS // MARKET INTELLIGENCE</p>
            <h1>Market Analysis</h1>
            <p>Volatility, sector breadth, technicals, data health, provider uptime, and live derivatives — one operating view.</p>
          </div>
          <div className="analysis-live-chip">
            <Sparkles className="size-3.5" /> Live feed synced
          </div>
        </header>
        <div className="analysis-tabs">
          {TABS.map((t) => {
            const Icon = t.icon
            return (
              <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>
                <Icon className="size-3.5" /> {t.label}
              </button>
            )
          })}
        </div>
        {tab === 'pulse' && <PulseTab />}
        {tab === 'technical' && <TechnicalTab />}
        {tab === 'freshness' && <FreshnessTab />}
        {tab === 'providers' && <ProvidersTab />}
        {tab === 'chain' && <OptionChainTab />}
      </main>
    </ShellLayout>
  )
}
