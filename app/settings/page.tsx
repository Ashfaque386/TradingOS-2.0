'use client'

import { useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bell,
  Braces,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  History,
  KeyRound,
  Lock,
  Plug,
  RotateCcw,
  Search,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  X,
  XCircle,
} from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'

type SectionId = 'gateway' | 'skills' | 'credentials' | 'notifications' | 'risk'

const SECTIONS: { id: SectionId; label: string; icon: typeof Braces; desc: string }[] = [
  { id: 'gateway', label: 'Agent Gateway Config', icon: Braces, desc: 'Infra & agent defaults' },
  { id: 'skills', label: 'Skill Marketplace', icon: Sparkles, desc: 'Grant tools to agents' },
  { id: 'credentials', label: 'Broker & LLM Credentials', icon: KeyRound, desc: 'Write-only secrets' },
  { id: 'notifications', label: 'Notification Channels', icon: Bell, desc: 'Telegram / Discord / Slack' },
  { id: 'risk', label: 'Risk Limits', icon: SlidersHorizontal, desc: 'Dual-control thresholds' },
]

/* ------------------------------- Section 1 -------------------------------- */

const RAW_JSON5 = `{
  // TradingOS agent gateway — resolved config
  infra: {
    region: "ap-south-1",
    latencyBudgetMs: 50,
    maxConcurrentAgents: 12,
    killSwitchArmed: true,
  },
  agentDefaults: {
    model: "gpt-4o",
    temperature: 0.2,
    maxToolCalls: 8,
    retryPolicy: { attempts: 3, backoffMs: 400 },
  },
  agents: {
    entries: {
      "ceo-agent":     { model: "claude-3.7-sonnet", temperature: 0.1 },
      "risk-guardian": { model: "gpt-4o", latencyBudgetMs: 30 },
      "execution-02":  { model: "gpt-4o-mini", maxToolCalls: 4 },
    },
  },
  channels: {
    slack:    { bound: true,  workspace: "quant-desk" },
    telegram: { bound: true,  chatId: "-100•••••" },
    discord:  { bound: false, guild: null },
  },
}`

const VALIDATION_ERRORS = [
  { path: 'agents.entries.ceo-agent.foo', message: 'unknown key: agents.entries.ceo-agent.foo' },
  { path: 'infra.latencyBudgetMs', message: 'value 50 is at the hard ceiling (max 50ms) — no headroom for retries' },
]

const CONFIG_VERSIONS = [
  { v: 'v38', when: '2025-06-11 09:02 IST', author: 'N. Iyer (Admin)', note: 'Tightened execution-02 tool calls', current: true },
  { v: 'v37', when: '2025-06-10 17:44 IST', author: 'S. Rao (PM)', note: 'Bound Telegram channel', current: false },
  { v: 'v36', when: '2025-06-10 11:20 IST', author: 'N. Iyer (Admin)', note: 'Lowered latency budget 60→50ms', current: false },
  { v: 'v35', when: '2025-06-09 15:08 IST', author: 'A. Mehta (Risk)', note: 'Armed kill-switch by default', current: false },
]

function GatewaySection() {
  const [tab, setTab] = useState<'form' | 'raw'>('form')
  const [validated, setValidated] = useState(false)
  const [rollingBack, setRollingBack] = useState<string | null>(null)

  const [latencyBudget, setLatencyBudget] = useState('50')
  const [maxConcurrent, setMaxConcurrent] = useState('12')
  const [defaultModel, setDefaultModel] = useState('gpt-4o')
  const [temperature, setTemperature] = useState('0.2')
  const [killSwitch, setKillSwitch] = useState(true)

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Agent Gateway Config</h2>
          <p>Resolved configuration across infra defaults, agent defaults, per-agent overrides, and channel bindings.</p>
        </div>
        <div className="set-tabgroup">
          <button className={tab === 'form' ? 'active' : ''} onClick={() => setTab('form')}>
            <SlidersHorizontal className="size-3.5" /> Form
          </button>
          <button className={tab === 'raw' ? 'active' : ''} onClick={() => setTab('raw')}>
            <Braces className="size-3.5" /> Raw JSON5
          </button>
        </div>
      </div>

      {tab === 'form' ? (
        <div className="set-form-grid">
          <fieldset className="set-fieldset">
            <legend>Infra defaults</legend>
            <label className="set-field">
              <span>Latency budget (ms)</span>
              <input value={latencyBudget} onChange={(e) => setLatencyBudget(e.target.value)} inputMode="numeric" />
            </label>
            <label className="set-field">
              <span>Max concurrent agents</span>
              <input value={maxConcurrent} onChange={(e) => setMaxConcurrent(e.target.value)} inputMode="numeric" />
            </label>
            <label className="set-toggle-row">
              <span>Kill-switch armed on boot</span>
              <button
                type="button"
                role="switch"
                aria-checked={killSwitch}
                className={`set-switch ${killSwitch ? 'on' : ''}`}
                onClick={() => setKillSwitch((v) => !v)}
              >
                <span />
              </button>
            </label>
          </fieldset>

          <fieldset className="set-fieldset">
            <legend>Agent defaults</legend>
            <label className="set-field">
              <span>Default model</span>
              <select value={defaultModel} onChange={(e) => setDefaultModel(e.target.value)}>
                <option>gpt-4o</option>
                <option>gpt-4o-mini</option>
                <option>claude-3.7-sonnet</option>
                <option>gemini-2.0-flash</option>
              </select>
            </label>
            <label className="set-field">
              <span>Temperature</span>
              <input value={temperature} onChange={(e) => setTemperature(e.target.value)} inputMode="decimal" />
            </label>
          </fieldset>

          <fieldset className="set-fieldset set-fieldset-wide">
            <legend>Per-agent overrides</legend>
            <div className="set-override-list">
              {[
                { id: 'ceo-agent', model: 'claude-3.7-sonnet', extra: 'temp 0.1' },
                { id: 'risk-guardian', model: 'gpt-4o', extra: 'latency 30ms' },
                { id: 'execution-02', model: 'gpt-4o-mini', extra: 'maxToolCalls 4' },
              ].map((o) => (
                <div key={o.id} className="set-override-row">
                  <code>{o.id}</code>
                  <span className="set-override-model">{o.model}</span>
                  <span className="set-override-extra">{o.extra}</span>
                </div>
              ))}
            </div>
          </fieldset>

          <fieldset className="set-fieldset set-fieldset-wide">
            <legend>Channel bindings</legend>
            <div className="set-binding-row">
              {[
                { name: 'Slack', bound: true, meta: 'quant-desk' },
                { name: 'Telegram', bound: true, meta: 'chatId -100•••••' },
                { name: 'Discord', bound: false, meta: 'not bound' },
              ].map((c) => (
                <div key={c.name} className={`set-binding ${c.bound ? 'bound' : ''}`}>
                  <span className="set-binding-dot" />
                  <strong>{c.name}</strong>
                  <small>{c.meta}</small>
                </div>
              ))}
            </div>
          </fieldset>
        </div>
      ) : (
        <pre className="set-raw-editor">{RAW_JSON5}</pre>
      )}

      <div className="set-validate-bar">
        <button className="set-btn set-btn-primary" onClick={() => setValidated(true)}>
          <ShieldCheck className="size-3.5" /> Validate
        </button>
        {validated && (
          <div className="set-validate-result">
            <p className="set-validate-title">
              <AlertTriangle className="size-3.5" /> {VALIDATION_ERRORS.length} schema issues found
            </p>
            {VALIDATION_ERRORS.map((err) => (
              <div key={err.path} className="set-validate-err">
                <code>{err.path}</code>
                <span>{err.message}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="set-versions">
        <p className="set-versions-title">
          <History className="size-3.5" /> Config version history
        </p>
        <ul>
          {CONFIG_VERSIONS.map((cv) => (
            <li key={cv.v}>
              <span className="set-version-tag mono">{cv.v}</span>
              <span className="set-version-when mono">{cv.when}</span>
              <span className="set-version-author">{cv.author}</span>
              <span className="set-version-note">{cv.note}</span>
              {cv.current ? (
                <span className="set-version-current">Current</span>
              ) : (
                <button
                  className="set-rollback"
                  onClick={() => {
                    setRollingBack(cv.v)
                    setTimeout(() => setRollingBack(null), 1400)
                  }}
                >
                  {rollingBack === cv.v ? (
                    <>
                      <Check className="size-3" /> Staged
                    </>
                  ) : (
                    <>
                      <RotateCcw className="size-3" /> Rollback
                    </>
                  )}
                </button>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

/* ------------------------------- Section 2 -------------------------------- */

type Health = 'green' | 'yellow' | 'red'

interface Skill {
  id: string
  desc: string
  health: Health
  grantedTo: string[]
}

const AGENTS = ['CEO', 'RiskGuardian', 'Execution-02', 'SignalScout', 'StrategyOps', 'ComplianceBot']

const SKILLS: Skill[] = [
  { id: 'market-data-read', desc: 'Read live and historical OHLCV across NSE/BSE instruments.', health: 'green', grantedTo: ['SignalScout', 'StrategyOps', 'RiskGuardian'] },
  { id: 'option-chain-read', desc: 'Fetch live F&O option chains, OI, and IV surfaces.', health: 'green', grantedTo: ['SignalScout', 'StrategyOps'] },
  { id: 'portfolio-status-read', desc: 'Read open positions, exposure, and realized/unrealized P&L.', health: 'yellow', grantedTo: ['RiskGuardian', 'CEO'] },
  { id: 'code-format-lint', desc: 'Format and lint generated strategy code before review.', health: 'green', grantedTo: ['StrategyOps'] },
  { id: 'sandbox-dry-run', desc: 'Execute strategy logic in an isolated no-order sandbox.', health: 'red', grantedTo: ['StrategyOps', 'Execution-02'] },
  { id: 'notification-send', desc: 'Dispatch alerts to bound Telegram/Discord/Slack channels.', health: 'green', grantedTo: ['ComplianceBot', 'RiskGuardian', 'CEO'] },
]

function initials(name: string) {
  return name.replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase()
}

function SkillsSection() {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Skill | null>(null)
  const [enabled, setEnabled] = useState<Record<string, boolean>>(
    () => Object.fromEntries(SKILLS.map((s) => [s.id, s.health !== 'red'])),
  )
  const [matrix, setMatrix] = useState<Record<string, Record<string, boolean>>>(() =>
    Object.fromEntries(
      SKILLS.map((s) => [s.id, Object.fromEntries(AGENTS.map((a) => [a, s.grantedTo.includes(a)]))]),
    ),
  )

  const filtered = useMemo(
    () => SKILLS.filter((s) => s.id.toLowerCase().includes(query.toLowerCase()) || s.desc.toLowerCase().includes(query.toLowerCase())),
    [query],
  )

  const toggleGrant = (skillId: string, agent: string) =>
    setMatrix((m) => ({ ...m, [skillId]: { ...m[skillId], [agent]: !m[skillId][agent] } }))

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Skill Marketplace</h2>
          <p>Reusable capabilities granted to agents. Toggle a skill on/off or open it to manage per-agent grants.</p>
        </div>
        <label className="set-search">
          <Search className="size-3.5" />
          <input placeholder="Search skills..." value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
      </div>

      <div className="set-skill-grid">
        {filtered.map((s) => {
          const grants = matrix[s.id]
          const granted = AGENTS.filter((a) => grants[a])
          return (
            <button key={s.id} className="set-skill-card" onClick={() => setSelected(s)}>
              <div className="set-skill-card-top">
                <span className={`set-health set-health-${s.health}`} title={`health: ${s.health}`} />
                <code>{s.id}</code>
                <span
                  role="switch"
                  aria-checked={enabled[s.id]}
                  className={`set-switch set-switch-sm ${enabled[s.id] ? 'on' : ''}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    setEnabled((v) => ({ ...v, [s.id]: !v[s.id] }))
                  }}
                >
                  <span />
                </span>
              </div>
              <p className="set-skill-desc">{s.desc}</p>
              <div className="set-skill-card-foot">
                <div className="avatar-stack">
                  {granted.slice(0, 4).map((a) => (
                    <span key={a} title={a}>{initials(a)}</span>
                  ))}
                </div>
                <small>{granted.length} agent{granted.length === 1 ? '' : 's'} granted</small>
              </div>
            </button>
          )
        })}
      </div>

      {selected && (
        <div className="set-modal-scrim" onClick={() => setSelected(null)}>
          <div className="set-detail" onClick={(e) => e.stopPropagation()}>
            <div className="set-detail-head">
              <div>
                <span className={`set-health set-health-${selected.health}`} />
                <code>{selected.id}</code>
              </div>
              <button className="set-icon-btn" onClick={() => setSelected(null)} aria-label="Close">
                <X className="size-4" />
              </button>
            </div>
            <p className="set-detail-desc">{selected.desc}</p>
            <p className="set-matrix-title">Per-agent grant matrix</p>
            <div className="set-matrix">
              <table>
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>{selected.id}</th>
                  </tr>
                </thead>
                <tbody>
                  {AGENTS.map((a) => (
                    <tr key={a}>
                      <td>{a}</td>
                      <td>
                        <button
                          className={`set-check ${matrix[selected.id][a] ? 'on' : ''}`}
                          onClick={() => toggleGrant(selected.id, a)}
                          aria-label={`${matrix[selected.id][a] ? 'Revoke' : 'Grant'} ${selected.id} for ${a}`}
                        >
                          {matrix[selected.id][a] && <Check className="size-3.5" />}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="set-matrix-hint">This grid is reusable to display all skills &times; all agents in a combined governance view.</p>
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------- Section 3 -------------------------------- */

type ConnState = 'connected' | 'not-connected' | 'error'

interface Credential {
  id: string
  label: string
  group: 'Broker' | 'LLM Provider'
  state: ConnState
  saved: boolean
}

const CREDENTIALS: Credential[] = [
  { id: 'zerodha', label: 'Zerodha Kite', group: 'Broker', state: 'connected', saved: true },
  { id: 'upstox', label: 'Upstox', group: 'Broker', state: 'error', saved: true },
  { id: 'openai', label: 'OpenAI', group: 'LLM Provider', state: 'connected', saved: true },
  { id: 'anthropic', label: 'Anthropic', group: 'LLM Provider', state: 'connected', saved: true },
  { id: 'google', label: 'Google Gemini', group: 'LLM Provider', state: 'connected', saved: true },
  { id: 'mistral', label: 'Mistral', group: 'LLM Provider', state: 'not-connected', saved: false },
  { id: 'cohere', label: 'Cohere', group: 'LLM Provider', state: 'connected', saved: true },
  { id: 'groq', label: 'Groq', group: 'LLM Provider', state: 'connected', saved: true },
  { id: 'perplexity', label: 'Perplexity', group: 'LLM Provider', state: 'not-connected', saved: false },
]

function CredentialCard({ cred }: { cred: Credential }) {
  const [value, setValue] = useState('')
  const [saved, setSaved] = useState(cred.saved)
  const [testing, setTesting] = useState(false)
  const [tested, setTested] = useState<ConnState | null>(null)

  const state = tested ?? cred.state

  return (
    <div className="set-cred-card">
      <div className="set-cred-head">
        <strong>{cred.label}</strong>
        <span className={`set-conn set-conn-${state}`}>
          {state === 'connected' ? <CheckCircle2 className="size-3" /> : state === 'error' ? <XCircle className="size-3" /> : <Circle className="size-3" />}
          {state === 'connected' ? 'Connected' : state === 'error' ? 'Auth failed' : 'Not connected'}
        </span>
      </div>
      <div className="set-cred-field">
        <Lock className="size-3.5" />
        <input
          type="password"
          placeholder={saved ? '•••• saved' : 'Paste API key / secret'}
          value={value}
          onChange={(e) => {
            setValue(e.target.value)
            setSaved(false)
          }}
        />
      </div>
      <div className="set-cred-actions">
        <button
          className="set-btn set-btn-ghost"
          disabled={!value}
          onClick={() => {
            setSaved(true)
            setValue('')
          }}
        >
          Save (write-only)
        </button>
        <button
          className="set-btn set-btn-secondary"
          disabled={testing}
          onClick={() => {
            setTesting(true)
            setTested(null)
            setTimeout(() => {
              setTesting(false)
              setTested(cred.state === 'error' ? 'error' : 'connected')
            }, 1100)
          }}
        >
          {testing ? 'Testing…' : 'Test Connection'}
        </button>
      </div>
    </div>
  )
}

function CredentialsSection() {
  const brokers = CREDENTIALS.filter((c) => c.group === 'Broker')
  const llms = CREDENTIALS.filter((c) => c.group === 'LLM Provider')
  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Broker & LLM Credentials</h2>
          <p>Secrets are write-only. Saved values are never displayed &mdash; only a masked &ldquo;saved&rdquo; state and live connection status.</p>
        </div>
      </div>

      <p className="set-group-label">Brokers</p>
      <div className="set-cred-grid">
        {brokers.map((c) => <CredentialCard key={c.id} cred={c} />)}
      </div>

      <p className="set-group-label">LLM Providers</p>
      <div className="set-cred-grid">
        {llms.map((c) => <CredentialCard key={c.id} cred={c} />)}
      </div>
    </div>
  )
}

/* ------------------------------- Section 4 -------------------------------- */

const ALERT_LEVELS = [
  { id: 'kill-switch', label: 'Kill-switch trips' },
  { id: 'sign-off', label: 'Sign-off items' },
  { id: 'go-live', label: 'Go-live gate passes' },
  { id: 'daily', label: 'Daily summary' },
]

interface Channel {
  id: string
  name: string
  meta: string
  defaults: string[]
}

const CHANNELS: Channel[] = [
  { id: 'telegram', name: 'Telegram', meta: 'Bot @tradingos_alerts', defaults: ['kill-switch', 'sign-off', 'go-live', 'daily'] },
  { id: 'discord', name: 'Discord', meta: 'Webhook · #ops-alerts', defaults: ['kill-switch', 'go-live'] },
  { id: 'slack', name: 'Slack', meta: 'Workspace quant-desk', defaults: [] },
]

function ChannelCard({ channel }: { channel: Channel }) {
  const [connected, setConnected] = useState(channel.defaults.length > 0)
  const [prefs, setPrefs] = useState<Record<string, boolean>>(
    () => Object.fromEntries(ALERT_LEVELS.map((l) => [l.id, channel.defaults.includes(l.id)])),
  )

  return (
    <div className={`set-channel ${connected ? 'connected' : ''}`}>
      <div className="set-channel-head">
        <div>
          <strong>{channel.name}</strong>
          <small>{channel.meta}</small>
        </div>
        <button
          className={connected ? 'set-btn set-btn-danger' : 'set-btn set-btn-primary'}
          onClick={() => setConnected((v) => !v)}
        >
          <Plug className="size-3.5" /> {connected ? 'Disconnect' : 'Connect'}
        </button>
      </div>
      <fieldset className="set-alert-prefs" disabled={!connected}>
        <legend>Alert levels</legend>
        {ALERT_LEVELS.map((l) => (
          <label key={l.id} className="set-alert-pref">
            <button
              type="button"
              className={`set-check ${prefs[l.id] ? 'on' : ''}`}
              onClick={() => setPrefs((p) => ({ ...p, [l.id]: !p[l.id] }))}
              aria-label={`${prefs[l.id] ? 'Disable' : 'Enable'} ${l.label} on ${channel.name}`}
            >
              {prefs[l.id] && <Check className="size-3.5" />}
            </button>
            <span>{l.label}</span>
          </label>
        ))}
      </fieldset>
    </div>
  )
}

function NotificationsSection() {
  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Notification Channels</h2>
          <p>Connect messaging channels and choose which alert levels each one receives.</p>
        </div>
      </div>
      <div className="set-channel-grid">
        {CHANNELS.map((c) => <ChannelCard key={c.id} channel={c} />)}
      </div>
    </div>
  )
}

/* ------------------------------- Section 5 -------------------------------- */

type ProposeState = 'idle' | 'staged' | 'awaiting' | 'confirmed'

interface RiskLimit {
  id: string
  label: string
  current: string
  unit: string
}

const RISK_LIMITS: RiskLimit[] = [
  { id: 'max-dd', label: 'Max drawdown', current: '12', unit: '%' },
  { id: 'corr', label: 'Correlation limit', current: '0.65', unit: 'ρ' },
  { id: 'latency', label: 'Latency guard', current: '50', unit: 'ms' },
]

function RiskRow({ limit }: { limit: RiskLimit }) {
  const [state, setState] = useState<ProposeState>('idle')
  const [proposed, setProposed] = useState('')

  return (
    <div className={`set-risk-row set-risk-${state}`}>
      <div className="set-risk-meta">
        <span className="set-risk-label">{limit.label}</span>
        <strong className="mono">
          {limit.current}
          <em>{limit.unit}</em>
        </strong>
      </div>

      {state === 'idle' && (
        <button className="set-btn set-btn-secondary" onClick={() => setState('staged')}>
          Propose Change
        </button>
      )}

      {state === 'staged' && (
        <div className="set-risk-stage">
          <input
            placeholder={`New value (${limit.unit})`}
            value={proposed}
            onChange={(e) => setProposed(e.target.value)}
            inputMode="decimal"
          />
          <button className="set-btn set-btn-ghost" onClick={() => setState('idle')}>Cancel</button>
          <button className="set-btn set-btn-primary" disabled={!proposed} onClick={() => setState('awaiting')}>
            Stage change
          </button>
        </div>
      )}

      {state === 'awaiting' && (
        <div className="set-risk-await">
          <span className="set-await-badge">
            <ShieldAlert className="size-3.5" /> Awaiting second approval
          </span>
          <span className="set-await-detail mono">
            {limit.current}{limit.unit} → {proposed}{limit.unit}
          </span>
          <span className="set-await-note">Requires a different user to confirm</span>
          <button className="set-btn set-btn-primary" onClick={() => setState('confirmed')}>
            Confirm as second user
          </button>
        </div>
      )}

      {state === 'confirmed' && (
        <div className="set-risk-confirmed">
          <span className="set-confirmed-badge">
            <CheckCircle2 className="size-3.5" /> Confirmed & applied
          </span>
          <span className="set-await-detail mono">Now {proposed}{limit.unit}</span>
          <button className="set-btn set-btn-ghost" onClick={() => { setState('idle'); setProposed('') }}>
            Reset demo
          </button>
        </div>
      )}
    </div>
  )
}

function RiskSection() {
  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Risk Limits</h2>
          <p>Thresholds are read-only. Changes follow a dual-control flow &mdash; a second, different user must confirm before they apply.</p>
        </div>
      </div>
      <div className="set-dualcontrol-banner">
        <Lock className="size-4" />
        <div>
          <strong>Dual-control enforced</strong>
          <p>Stage &rarr; awaiting second approval &rarr; confirmed. No single operator can change a live risk limit alone.</p>
        </div>
      </div>
      <div className="set-risk-list">
        {RISK_LIMITS.map((l) => <RiskRow key={l.id} limit={l} />)}
      </div>
    </div>
  )
}

/* --------------------------------- Page ----------------------------------- */

export default function SettingsPage() {
  const [section, setSection] = useState<SectionId>('gateway')

  return (
    <ShellLayout>
      <main className="set-page mx-auto w-full max-w-[1500px] pb-10">
        <header className="set-header">
          <p className="eyebrow">TRADINGOS // CONTROL PLANE</p>
          <h1>Settings</h1>
          <p>Gateway configuration, skill grants, credentials, notifications, and dual-controlled risk limits.</p>
        </header>

        <div className="set-shell">
          <nav className="set-subnav" aria-label="Settings sections">
            {SECTIONS.map((s) => {
              const Icon = s.icon
              const active = section === s.id
              return (
                <button key={s.id} className={active ? 'active' : ''} onClick={() => setSection(s.id)}>
                  <Icon className="size-4" />
                  <span className="set-subnav-text">
                    <strong>{s.label}</strong>
                    <small>{s.desc}</small>
                  </span>
                  <ChevronRight className="set-subnav-caret size-3.5" />
                </button>
              )
            })}
          </nav>

          <section className="set-content">
            {section === 'gateway' && <GatewaySection />}
            {section === 'skills' && <SkillsSection />}
            {section === 'credentials' && <CredentialsSection />}
            {section === 'notifications' && <NotificationsSection />}
            {section === 'risk' && <RiskSection />}
          </section>
        </div>
      </main>
    </ShellLayout>
  )
}
