'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Bell,
  Braces,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Copy,
  Cpu,
  ExternalLink,
  GripVertical,
  History,
  KeyRound,
  Lock,
  Pencil,
  Plug,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Timer,
  Wand2,
  Webhook,
  X,
  XCircle,
  Zap,
} from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import {
  type AgentSummary,
  type AlertLevel,
  type BrokerCredentialStatus,
  type ConfigVersionSummary,
  type GatewayConfigResponse,
  type LlmProviderModel,
  type LlmProviderName,
  type LlmProviderStatus,
  type NotificationChannelStatus,
  type RiskLimit,
  type RiskLimitChange,
  ALERT_LEVELS,
  ApiError,
  KNOWN_BROKERS,
  LLM_PROVIDERS,
  LLM_PROVIDER_LABELS,
  NOTIFICATION_CHANNELS,
  applyRiskLimitChange,
  confirmRiskLimitChange,
  deleteBrokerCredentials,
  fallbackBrokerRedirectUri,
  deleteLlmProviderCredentials,
  setLlmProviderDefaultModel,
  deleteNotificationChannel,
  detectTelegramChatId,
  getAgents,
  getBrokerLoginUrl,
  getGatewayConfig,
  listBrokerCredentialStatus,
  listGatewayConfigVersions,
  listLlmProviderModels,
  listLlmProviderStatus,
  listNotificationChannels,
  listRiskLimitChanges,
  listRiskLimits,
  putGatewayConfig,
  rollbackGatewayConfig,
  stageRiskLimitChange,
  testLlmProvider,
  testNotificationChannel,
  validateGatewayConfig,
  writeBrokerCredentials,
  writeBrokerRedirectUri,
  writeLlmProviderCredentials,
  writeNotificationChannel,
} from '@/lib/api'

type SectionId = 'gateway' | 'llm' | 'broker' | 'notifications' | 'skills' | 'risk'

const SECTIONS: { id: SectionId; label: string; icon: typeof Braces; desc: string }[] = [
  { id: 'gateway', label: 'Agent Gateway Config', icon: Braces, desc: 'Infra & agent defaults' },
  { id: 'llm', label: 'LLM Providers', icon: Cpu, desc: 'Keys, test, fallback order' },
  { id: 'broker', label: 'Broker Config', icon: KeyRound, desc: 'Zerodha / Upstox OAuth' },
  { id: 'notifications', label: 'Notification Channels', icon: Bell, desc: 'Telegram / Discord / Slack' },
  { id: 'skills', label: 'Skill Marketplace', icon: Sparkles, desc: 'Grant tools to agents' },
  { id: 'risk', label: 'Risk Limits', icon: SlidersHorizontal, desc: 'Dual-control thresholds' },
]

function copyToClipboard(text: string) {
  navigator.clipboard?.writeText(text).catch(() => {
    // Clipboard API unavailable (insecure context, permissions) -- the
    // field itself is still selectable/copyable by hand.
  })
}

function formatIstTime(iso: string): string {
  const d = new Date(iso)
  const time = d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' })
  const todayIst = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
  const dateIst = d.toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' })
  return dateIst === todayIst ? `${time} IST today` : `${time} IST on ${dateIst}`
}

/* ---------------------------- Status summary strip ------------------------ */

type StatusChip = { key: string; label: string; detail: string; color: 'green' | 'amber' | 'red' | 'gray'; section: SectionId }

function StatusStrip({ onNavigate, refreshKey }: { onNavigate: (s: SectionId) => void; refreshKey: number }) {
  const [chips, setChips] = useState<StatusChip[]>([])

  useEffect(() => {
    let cancelled = false
    async function load() {
      const [brokers, providers, channels] = await Promise.all([
        listBrokerCredentialStatus().catch(() => [] as BrokerCredentialStatus[]),
        listLlmProviderStatus().catch(() => [] as LlmProviderStatus[]),
        listNotificationChannels().catch(() => [] as NotificationChannelStatus[]),
      ])
      if (cancelled) return
      const next: StatusChip[] = []
      for (const b of brokers) {
        next.push({
          key: `broker-${b.broker}`,
          label: b.broker,
          detail: b.token_status === 'valid' ? 'connected' : b.token_status === 'expired' ? 'expired' : 'not connected',
          color: b.token_status === 'valid' ? 'green' : b.token_status === 'expired' ? 'amber' : 'gray',
          section: 'broker',
        })
      }
      for (const p of providers.filter((p) => p.configured)) {
        next.push({
          key: `llm-${p.provider}`,
          label: LLM_PROVIDER_LABELS[p.provider as LlmProviderName] ?? p.provider,
          detail: p.in_fallback_order ? 'in fallback chain' : 'configured, not in order',
          color: p.in_fallback_order ? 'green' : 'amber',
          section: 'llm',
        })
      }
      for (const c of channels) {
        next.push({
          key: `chan-${c.channel}`,
          label: c.channel,
          detail: c.configured ? (c.enabled ? 'active' : 'configured, disabled') : 'not configured',
          color: c.configured ? (c.enabled ? 'green' : 'amber') : 'gray',
          section: 'notifications',
        })
      }
      setChips(next)
    }
    load()
    return () => { cancelled = true }
    // refreshKey is bumped by BrokerConfigSection/LlmProvidersSection/
    // NotificationsSection whenever any of their own data actually
    // changes (save, delete, toggle, a real OAuth/test round-trip) -- a
    // save previously left this strip showing the pre-save status until
    // the whole page was reloaded, since this effect only ran once on
    // mount.
  }, [refreshKey])

  if (chips.length === 0) return null

  return (
    <div className="set-status-strip">
      {chips.map((c) => (
        <button key={c.key} className="set-status-chip" onClick={() => onNavigate(c.section)}>
          <span className={`set-status-dot ${c.color}`} />
          <span className="set-status-chip-text"><strong className="capitalize">{c.label}</strong><small>{c.detail}</small></span>
        </button>
      ))}
    </div>
  )
}

/* ------------------------------- Section 1 -------------------------------- */

function GatewaySection() {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [tab, setTab] = useState<'form' | 'raw'>('form')
  const [config, setConfig] = useState<GatewayConfigResponse | null>(null)
  const [rawText, setRawText] = useState('')
  const [versions, setVersions] = useState<ConfigVersionSummary[]>([])
  const [validateErrors, setValidateErrors] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    const [cfg, vers] = await Promise.all([getGatewayConfig(), listGatewayConfigVersions()])
    setConfig(cfg)
    setRawText(cfg.raw_text)
    setVersions(vers)
  }

  useEffect(() => { reload() }, [])

  async function handleValidate() {
    setBusy(true)
    setError(null)
    try {
      const result = await validateGatewayConfig(rawText)
      setValidateErrors(result.errors)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Validation failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleSave() {
    setBusy(true)
    setError(null)
    try {
      await putGatewayConfig(rawText)
      await reload()
      setValidateErrors(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleRollback(versionId: number) {
    setBusy(true)
    setError(null)
    try {
      await rollbackGatewayConfig(versionId)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Rollback failed')
    } finally {
      setBusy(false)
    }
  }

  const parsed = config?.parsed as Record<string, any> | undefined

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div><h2>Agent Gateway Config</h2><p>Resolved configuration across infra defaults, agent defaults, per-agent overrides, and channel bindings.</p></div>
        <div className="set-tabgroup">
          <button className={tab === 'form' ? 'active' : ''} onClick={() => setTab('form')}><SlidersHorizontal className="size-3.5" /> Summary</button>
          <button className={tab === 'raw' ? 'active' : ''} onClick={() => setTab('raw')}><Braces className="size-3.5" /> Raw JSON</button>
        </div>
      </div>
      {error && <div className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}

      {tab === 'form' ? (
        <div className="set-form-grid">
          <fieldset className="set-fieldset">
            <legend>LLM provider order</legend>
            <p className="text-xs text-muted-foreground">{parsed?.infra?.llmProviders?.order?.join(' → ') ?? '—'}</p>
          </fieldset>
          <fieldset className="set-fieldset">
            <legend>Broker failover</legend>
            <p className="text-xs text-muted-foreground">{parsed?.infra?.brokerFailover?.primary ?? '—'} → {parsed?.infra?.brokerFailover?.fallback ?? '—'}</p>
          </fieldset>
          <fieldset className="set-fieldset">
            <legend>Risk threshold pointers (read-only)</legend>
            <p className="text-xs text-muted-foreground">Max drawdown {parsed?.infra?.riskThresholdRefs?.maxDrawdownPct ?? '—'}% · WS latency {parsed?.infra?.riskThresholdRefs?.wsLatencyMs ?? '—'}ms — set via the Risk Limits section, not here.</p>
          </fieldset>
          <fieldset className="set-fieldset">
            <legend>Agent defaults</legend>
            <p className="text-xs text-muted-foreground">Model {parsed?.agents?.defaults?.model ?? 'auto'} · Heartbeat {parsed?.agents?.defaults?.heartbeatEnabled ? 'on' : 'off'} · Skills {(parsed?.agents?.defaults?.skills ?? []).join(', ') || '—'}</p>
          </fieldset>
          <fieldset className="set-fieldset set-fieldset-wide">
            <legend>Per-agent overrides ({Object.keys(parsed?.agents?.entries ?? {}).length})</legend>
            <div className="set-override-list">{Object.entries(parsed?.agents?.entries ?? {}).map(([id, entry]: [string, any]) => <div key={id} className="set-override-row"><code>{id}</code><span className="set-override-model">{entry.model ?? 'default model'}</span><span className="set-override-extra">{entry.enabled === false ? 'disabled' : ''} {entry.heartbeatEnabled !== undefined ? `heartbeat:${entry.heartbeatEnabled}` : ''}</span></div>)}{Object.keys(parsed?.agents?.entries ?? {}).length === 0 && <p className="text-xs text-muted-foreground">No per-agent overrides — use Agent Fleet or the Raw JSON tab to add one.</p>}</div>
          </fieldset>
          <fieldset className="set-fieldset set-fieldset-wide">
            <legend>Channel bindings ({(parsed?.bindings ?? []).length})</legend>
            <div className="set-binding-row">{(parsed?.bindings ?? []).map((b: any, i: number) => <div key={i} className="set-binding bound"><span className="set-binding-dot" /><strong>{b.agentId}</strong><small>{b.match?.channel} · {b.match?.accountId ?? 'any account'}</small></div>)}{(parsed?.bindings ?? []).length === 0 && <p className="text-xs text-muted-foreground">No channel bindings configured.</p>}</div>
          </fieldset>
        </div>
      ) : (
        <div>
          <textarea className="set-raw-editor" style={{ width: '100%', minHeight: 260 }} value={rawText} onChange={(e) => setRawText(e.target.value)} disabled={!canEdit} spellCheck={false} />
          {!canEdit && <p className="mt-2 text-xs text-muted-foreground">Read-only — SystemAdministrator role required to edit.</p>}
        </div>
      )}

      {canEdit && (
        <div className="set-validate-bar">
          <button className="set-btn set-btn-secondary" onClick={handleValidate} disabled={busy}><ShieldCheck className="size-3.5" /> Validate</button>
          <button className="set-btn set-btn-primary" onClick={handleSave} disabled={busy}>{busy ? <RefreshCw className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Save & apply</button>
          {validateErrors && (
            <div className="set-validate-result">
              <p className="set-validate-title"><AlertTriangle className="size-3.5" /> {validateErrors.length === 0 ? 'No schema issues' : `${validateErrors.length} schema issues found`}</p>
              {validateErrors.map((err) => <div key={err} className="set-validate-err"><span>{err}</span></div>)}
            </div>
          )}
        </div>
      )}

      <div className="set-versions">
        <p className="set-versions-title"><History className="size-3.5" /> Config version history</p>
        <ul>
          {versions.map((cv) => (
            <li key={cv.id}>
              <span className="set-version-tag mono">v{cv.id}</span>
              <span className="set-version-when mono">{new Date(cv.created_at).toLocaleString()}</span>
              <span className="set-version-author">{cv.source}</span>
              <span className="set-version-note">{cv.status}</span>
              {cv.id === config?.version_id ? <span className="set-version-current">Current</span> : canEdit ? <button className="set-rollback" disabled={busy} onClick={() => handleRollback(cv.id)}><RotateCcw className="size-3" /> Rollback</button> : null}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

/* ------------------------------- LLM Providers ----------------------------- */

const LOCAL_PROVIDERS: LlmProviderName[] = ['ollama', 'custom']

async function readGatewayOrder(): Promise<{ config: GatewayConfigResponse; parsed: Record<string, any>; order: string[] }> {
  const config = await getGatewayConfig()
  const parsed = JSON.parse(JSON.stringify(config.parsed)) as Record<string, any>
  const order: string[] = parsed?.infra?.llmProviders?.order ?? []
  return { config, parsed, order }
}

async function writeGatewayOrder(parsed: Record<string, any>, order: string[]) {
  parsed.infra = parsed.infra ?? {}
  parsed.infra.llmProviders = { ...(parsed.infra.llmProviders ?? {}), order }
  await putGatewayConfig(JSON.stringify(parsed))
}

// Providers with no universal default model: an agent's "auto" can't run on
// them until the operator picks one (otherwise the router skips them).
const NEEDS_DEFAULT_MODEL: LlmProviderName[] = ['ollama', 'custom', 'huggingface']

const API_KEY_PLACEHOLDERS: Partial<Record<LlmProviderName, string>> = {
  huggingface: 'Access token (hf_…)',
}

async function setProviderInOrder(provider: string, include: boolean) {
  const { parsed, order } = await readGatewayOrder()
  if (order.includes(provider) === include) return
  await writeGatewayOrder(parsed, include ? [...order, provider] : order.filter((p) => p !== provider))
}

function effectiveDefaultModel(status: LlmProviderStatus): string | null {
  return status.default_model ?? status.builtin_default_model ?? null
}

function LlmProviderCard({ status, canEdit, onChanged }: { status: LlmProviderStatus; canEdit: boolean; onChanged: () => void }) {
  const provider = status.provider as LlmProviderName
  const isLocal = LOCAL_PROVIDERS.includes(provider)
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState(status.base_url ?? '')
  const [models, setModels] = useState<LlmProviderModel[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const defaultModel = effectiveDefaultModel(status)
  const missingDefault = status.configured && !defaultModel && NEEDS_DEFAULT_MODEL.includes(provider)

  async function run(action: () => Promise<void>, failure: string) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : failure)
    } finally {
      setBusy(false)
    }
  }

  const handleSave = () => run(async () => {
    await writeLlmProviderCredentials(provider, isLocal ? undefined : apiKey || undefined, baseUrl || undefined)
    setApiKey('')
    // A newly configured provider joins the fallback chain; the switch
    // above can take it back out.
    if (!status.configured) await setProviderInOrder(provider, true)
  }, 'Save failed')

  const handleRemove = () => run(async () => {
    await deleteLlmProviderCredentials(provider)
    await setProviderInOrder(provider, false)
  }, 'Remove failed')

  const handleToggleOrder = () => run(() => setProviderInOrder(provider, !status.in_fallback_order), 'Failed to update fallback order')

  const handleSaveDefault = () => run(() => setLlmProviderDefaultModel(provider, selectedModel || null), 'Could not save the default model')

  const handleClearDefault = () => run(() => setLlmProviderDefaultModel(provider, null), 'Could not clear the default model')

  async function handleDiscover() {
    setDiscovering(true)
    setError(null)
    try {
      const res = await listLlmProviderModels(provider)
      setModels(res.models)
      const preferred = res.models.find((m) => m.id === defaultModel) ?? res.models[0]
      if (preferred) setSelectedModel(preferred.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Model discovery failed')
    } finally {
      setDiscovering(false)
    }
  }

  async function handleTest() {
    setBusy(true)
    setError(null)
    setTestResult(null)
    try {
      // No model picked: the server tests exactly what "auto" would use.
      const res = await testLlmProvider(provider, selectedModel || undefined)
      setTestResult({ ok: res.ok, detail: res.detail })
    } catch (err) {
      setTestResult({ ok: false, detail: err instanceof ApiError ? err.message : 'Test failed' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`set-llm-card ${status.in_fallback_order ? 'enabled' : ''}`}>
      <div className="set-llm-head">
        <strong>{LLM_PROVIDER_LABELS[provider] ?? provider}</strong>
        <span className={`set-conn set-conn-${status.configured ? 'connected' : 'not-connected'}`}>{status.configured ? <CheckCircle2 className="size-3" /> : <Circle className="size-3" />}{status.configured ? 'Configured' : 'Not configured'}</span>
      </div>

      {canEdit && (
        <label className="set-toggle-row">
          <span>In fallback priority chain</span>
          <button type="button" className={`set-switch set-switch-sm ${status.in_fallback_order ? 'on' : ''}`} onClick={handleToggleOrder} disabled={busy} aria-label={`${status.in_fallback_order ? 'Remove' : 'Add'} ${provider} ${status.in_fallback_order ? 'from' : 'to'} fallback order`}><span /></button>
        </label>
      )}

      {error && <p className="text-xs text-rose-300">{error}</p>}

      {canEdit && (
        <>
          {!isLocal && <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder={API_KEY_PLACEHOLDERS[provider] ?? 'API key'} value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>}
          {isLocal && <div className="set-cred-field"><Zap className="size-3.5" /><input placeholder={provider === 'ollama' ? 'http://host.docker.internal:11434' : 'http://host.docker.internal:8080'} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></div>}
          {isLocal && <p className="set-redirect-hint">Running the backend in Docker? Use <code>http://host.docker.internal:{provider === 'ollama' ? '11434' : '8080'}</code>, not <code>localhost</code> — inside the container, &quot;localhost&quot; means the container itself, not this machine.</p>}
          {provider === 'huggingface' && <p className="set-redirect-hint">Uses Hugging Face Inference Providers (<code>router.huggingface.co</code>). Create a fine-grained token with the &quot;Make calls to Inference Providers&quot; permission.</p>}
          <div className="set-cred-actions">
            <button className="set-btn set-btn-ghost" disabled={busy || (isLocal ? !baseUrl : !apiKey)} onClick={handleSave}>Save</button>
            {status.configured && <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleRemove}>Remove</button>}
          </div>
        </>
      )}

      <div className={`set-llm-default ${missingDefault ? 'warn' : ''}`}>
        <div className="set-llm-default-head">
          <span>Default model</span>
          <code>{status.default_model ?? (status.builtin_default_model ? `${status.builtin_default_model} (built-in)` : 'none set')}</code>
        </div>
        {missingDefault && <p className="set-llm-default-warn"><AlertTriangle className="size-3 shrink-0" />Agents skip this provider until a default model is chosen.</p>}
        {canEdit && status.configured && (
          <div className="set-llm-models">
            <button className="set-btn set-btn-ghost" disabled={discovering || busy} onClick={handleDiscover}><Wand2 className="size-3.5" /> {discovering ? 'Discovering…' : models.length ? 'Refresh' : 'Discover models'}</button>
            {models.length > 0 && (
              <>
                <select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}>
                  {models.map((m) => <option key={m.id} value={m.id}>{m.label ?? m.id}</option>)}
                </select>
                <button className="set-btn set-btn-ghost" disabled={busy || !selectedModel || selectedModel === status.default_model} onClick={handleSaveDefault}>Set default</button>
              </>
            )}
            {status.default_model && <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleClearDefault}>Clear</button>}
          </div>
        )}
      </div>

      <div className="set-llm-test-row">
        <button className="set-btn set-btn-secondary" disabled={busy || !status.configured || (!selectedModel && !defaultModel)} onClick={handleTest}>Test connection</button>
        {testResult && (
          <span className={`set-llm-test-result ${testResult.ok ? 'ok' : 'fail'}`}>{testResult.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}{testResult.detail}</span>
        )}
      </div>
    </div>
  )
}

function FallbackPriorityList({ canEdit, order, statuses, onChanged }: { canEdit: boolean; order: string[]; statuses: LlmProviderStatus[]; onChanged: () => void }) {
  // The order is owned by LlmProvidersSection and reloaded after every
  // change anywhere on this page. A private copy loaded once went stale
  // after a card's toggle, and the next drag wrote that stale copy back,
  // silently dropping the provider just added.
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function persist(next: string[]) {
    setBusy(true)
    setError(null)
    try {
      const { parsed } = await readGatewayOrder()
      await writeGatewayOrder(parsed, next)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to reorder')
    } finally {
      setBusy(false)
      onChanged()
    }
  }

  function handleDrop(targetIndex: number) {
    if (dragIndex === null || dragIndex === targetIndex) return
    const next = [...order]
    const [moved] = next.splice(dragIndex, 1)
    next.splice(targetIndex, 0, moved)
    setDragIndex(null)
    persist(next)
  }

  if (order.length === 0) return <p className="text-xs text-muted-foreground">No providers in the fallback chain yet — save a provider above or switch one on.</p>

  return (
    <div className="set-llm-priority-list">
      {error && <p className="text-xs text-rose-300">{error}</p>}
      {order.map((provider, i) => {
        const status = statuses.find((s) => s.provider === provider)
        const issue = !status?.configured
          ? 'not configured'
          : !effectiveDefaultModel(status) && NEEDS_DEFAULT_MODEL.includes(provider as LlmProviderName)
            ? 'no default model'
            : null
        return (
          <div
            key={provider}
            className={`set-llm-priority-row ${dragIndex === i ? 'dragging' : ''}`}
            draggable={canEdit && !busy}
            onDragStart={() => setDragIndex(i)}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => handleDrop(i)}
            onDragEnd={() => setDragIndex(null)}
          >
            <GripVertical className="size-3.5 text-muted-foreground" />
            <span className="set-llm-priority-rank">{i + 1}</span>
            <span className="set-llm-priority-label">{LLM_PROVIDER_LABELS[provider as LlmProviderName] ?? provider}</span>
            {status && effectiveDefaultModel(status) && <code className="set-llm-priority-model">{effectiveDefaultModel(status)}</code>}
            {issue && <span className="set-llm-priority-issue"><AlertTriangle className="size-3" />{issue} — skipped</span>}
          </div>
        )
      })}
    </div>
  )
}

function LlmProvidersSection({ onStatusChanged }: { onStatusChanged: () => void }) {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [statuses, setStatuses] = useState<LlmProviderStatus[]>([])
  const [order, setOrder] = useState<string[]>([])

  async function reload() {
    const [nextStatuses, { order: nextOrder }] = await Promise.all([listLlmProviderStatus(), readGatewayOrder()])
    setStatuses(nextStatuses)
    setOrder(nextOrder)
    onStatusChanged()
  }
  useEffect(() => { reload() }, [])

  const byProvider = (name: string): LlmProviderStatus => statuses.find((s) => s.provider === name) ?? { provider: name, configured: false, base_url: null, in_fallback_order: order.includes(name) }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>LLM Providers</h2><p>API keys are write-only. Fallback priority here is the exact same infra.llmProviders.order the Agent Gateway routes on — this is a UI over that config, not a separate store. Agents ask for model &quot;auto&quot;, which runs each provider&apos;s default model.</p></div></div>
      <div className="set-llm-grid">{LLM_PROVIDERS.map((p) => <LlmProviderCard key={p} status={byProvider(p)} canEdit={canEdit} onChanged={reload} />)}</div>

      <p className="set-group-label">Fallback priority</p>
      <FallbackPriorityList canEdit={canEdit} order={order} statuses={statuses} onChanged={reload} />
    </div>
  )
}

/* ------------------------------- Broker Config ----------------------------- */

const BROKER_CONSOLES: Record<string, { name: string; url: string }> = {
  zerodha: { name: 'Kite Connect developer console', url: 'https://developers.kite.trade/apps' },
  upstox: { name: 'Upstox developer apps page', url: 'https://account.upstox.com/developer/apps' },
}

function BrokerRedirectUri({ status, canEdit, pending, onPendingChange, onChanged }: {
  status: BrokerCredentialStatus
  canEdit: boolean
  // Before any key exists there's nothing to PUT onto yet, so an edit is
  // held here and sent with the first key save.
  pending: string | null
  onPendingChange: (value: string | null) => void
  onChanged: () => void
}) {
  const defaultUri = status.default_redirect_uri ?? fallbackBrokerRedirectUri(status.broker)
  const current = pending ?? status.redirect_uri ?? defaultUri
  const isCustom = pending !== null || Boolean(status.redirect_uri_is_custom)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(current)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const brokerConsole = BROKER_CONSOLES[status.broker]

  async function apply(value: string | null) {
    setError(null)
    if (!status.configured) {
      onPendingChange(value === null || value === defaultUri ? null : value)
      setEditing(false)
      return
    }
    setBusy(true)
    try {
      await writeBrokerRedirectUri(status.broker, value)
      onPendingChange(null)
      setEditing(false)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save the redirect URL')
    } finally {
      setBusy(false)
    }
  }

  function copy() {
    copyToClipboard(current)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="set-redirect-block">
      <div className="set-redirect-label">
        <span>Redirect / callback URL</span>
        <em className={isCustom ? 'custom' : ''}>{isCustom ? 'Custom' : 'Default'}</em>
      </div>
      {editing ? (
        <>
          <div className="set-cred-field">
            <input value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} aria-label="Redirect URL" />
          </div>
          <div className="set-cred-actions">
            <button className="set-btn set-btn-ghost" disabled={busy || !draft.trim()} onClick={() => apply(draft.trim())}>
              {status.configured ? 'Save URL' : 'Use this URL'}
            </button>
            <button className="set-btn set-btn-secondary" disabled={busy} onClick={() => { setEditing(false); setError(null) }}>Cancel</button>
          </div>
        </>
      ) : (
        <div className="set-redirect-field">
          <code title={current}>{current}</code>
          <button className="set-copy-btn" onClick={copy} aria-label="Copy redirect URL">
            {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
          </button>
          {canEdit && (
            <button className="set-copy-btn" onClick={() => { setDraft(current); setEditing(true) }} aria-label="Edit redirect URL" title="Edit">
              <Pencil className="size-3" />
            </button>
          )}
          {canEdit && isCustom && (
            <button className="set-copy-btn" disabled={busy} onClick={() => apply(null)} aria-label="Reset redirect URL to default" title="Reset to default">
              <RotateCcw className="size-3" />
            </button>
          )}
        </div>
      )}
      {error && <p className="text-xs text-rose-300">{error}</p>}
      {pending !== null && !status.configured && <p className="set-redirect-hint">Saved together with your API key below.</p>}
      <p className="set-redirect-hint">
        Step 1: register this exact URL as the Redirect URL in the{' '}
        <a href={brokerConsole?.url} target="_blank" rel="noreferrer">{brokerConsole?.name ?? 'broker developer console'}</a>
        {' '}when you create the app. The broker then issues the API key and secret you enter below. Only the host can change; the path must stay the same.
      </p>
    </div>
  )
}

function BrokerConfigCard({ status, canEdit, onChanged }: { status: BrokerCredentialStatus; canEdit: boolean; onChanged: () => void }) {
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [pendingRedirect, setPendingRedirect] = useState<string | null>(null)
  const [duration, setDuration] = useState<'standard' | 'extended'>('standard')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    setBusy(true)
    setError(null)
    try {
      await writeBrokerCredentials(status.broker, apiKey, apiSecret || undefined, undefined, pendingRedirect ?? undefined)
      setApiKey(''); setApiSecret(''); setPendingRedirect(null)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    setBusy(true)
    setError(null)
    try {
      await deleteBrokerCredentials(status.broker)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Delete failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleConnect() {
    setBusy(true)
    setError(null)
    try {
      const { login_url } = await getBrokerLoginUrl(status.broker, status.broker === 'upstox' ? duration : undefined)
      window.location.href = login_url
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not start login')
      setBusy(false)
    }
  }

  const tokenLabel =
    status.token_status === 'valid' && status.token_expires_at
      ? `Connected — expires ${formatIstTime(status.token_expires_at)}`
      : status.token_status === 'expired'
        ? 'Expired — click to reconnect'
        : 'Not connected'

  return (
    <div className={`set-broker-card ${status.token_status === 'valid' ? 'connected' : ''}`}>
      <div className="set-broker-head"><strong className="capitalize">{status.broker}</strong></div>
      <div className={`set-token-strip ${status.token_status}`}><Timer className="size-3.5" />{tokenLabel}</div>
      {error && <p className="text-xs text-rose-300">{error}</p>}

      {status.broker === 'zerodha' && (
        <div className="set-reminder"><AlertTriangle className="size-3.5 shrink-0" /><span>Zerodha access tokens expire daily — there's no way around this with their API. Reconnect each trading morning before the first order.</span></div>
      )}

      <BrokerRedirectUri status={status} canEdit={canEdit} pending={pendingRedirect} onPendingChange={setPendingRedirect} onChanged={onChanged} />

      {canEdit && <>
        <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder="API key" value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>
        <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder="API secret" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} /></div>
        <div className="set-cred-actions">
          <button className="set-btn set-btn-ghost" disabled={!apiKey || busy} onClick={handleSave}>Save (write-only)</button>
          {status.configured && <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleDelete}>Remove</button>}
        </div>

        {status.broker === 'upstox' && (
          <label className="set-field"><span>Token duration</span>
            <select value={duration} onChange={(e) => setDuration(e.target.value as 'standard' | 'extended')}>
              <option value="standard">Standard</option>
              <option value="extended">Extended (account/consent-gated by Upstox)</option>
            </select>
          </label>
        )}

        <button className="set-connect-btn" disabled={!status.configured || busy} onClick={handleConnect}>
          <ExternalLink className="size-3.5" /> {status.token_status === 'valid' ? 'Re-authenticate' : 'Connect'}
        </button>
        {!status.configured && <p className="set-redirect-hint">Save an API key and secret above first.</p>}
      </>}
    </div>
  )
}

function BrokerConfigSection({ oauthBanner, onStatusChanged }: { oauthBanner: { broker: string; ok: boolean; error?: string } | null; onStatusChanged: () => void }) {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [statuses, setStatuses] = useState<BrokerCredentialStatus[]>([])

  async function reload() {
    setStatuses(await listBrokerCredentialStatus())
    onStatusChanged()
  }
  useEffect(() => { reload() }, [])

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Broker Config</h2><p>Secrets are write-only. Connect completes the real broker OAuth login — you're redirected to Zerodha/Upstox and back.</p></div></div>
      {oauthBanner && (
        <div className={`mb-3 rounded-lg border p-3 text-xs ${oauthBanner.ok ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : 'border-rose-400/30 bg-rose-400/10 text-rose-200'}`}>
          {oauthBanner.ok ? `${oauthBanner.broker} connected successfully.` : `${oauthBanner.broker} connection failed: ${oauthBanner.error ?? 'unknown error'}`}
        </div>
      )}
      <div className="set-cred-grid">{statuses.length > 0 ? statuses.map((c) => <BrokerConfigCard key={c.broker} status={c} canEdit={canEdit} onChanged={reload} />) : KNOWN_BROKERS.map((b) => <BrokerConfigCard key={b} status={{ broker: b, configured: false, token_status: 'never-connected', token_expires_at: null, redirect_uri: fallbackBrokerRedirectUri(b), default_redirect_uri: fallbackBrokerRedirectUri(b), redirect_uri_is_custom: false, token_duration: null }} canEdit={canEdit} onChanged={reload} />)}</div>
    </div>
  )
}

/* ------------------------------- Section 2 -------------------------------- */

function initials(name: string) {
  return name.replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase()
}

async function patchAgentSkills(agentId: string, skills: string[]) {
  const config = await getGatewayConfig()
  const parsed = JSON.parse(JSON.stringify(config.parsed)) as Record<string, any>
  const agentsSection = parsed.agents ?? { entries: {} }
  const existing = agentsSection.entries[agentId] ?? {}
  agentsSection.entries[agentId] = { ...existing, skills }
  parsed.agents = agentsSection
  await putGatewayConfig(JSON.stringify(parsed))
}

function SkillsSection() {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [query, setQuery] = useState('')
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null)
  const [newSkillId, setNewSkillId] = useState('')
  const [busy, setBusy] = useState(false)

  async function reload() { setAgents(await getAgents()) }
  useEffect(() => { reload() }, [])

  const skills = useMemo(() => Array.from(new Set(agents.flatMap((a) => a.skills))).sort(), [agents])
  const filteredSkills = skills.filter((s) => s.toLowerCase().includes(query.toLowerCase()))

  async function toggleGrant(skillId: string, agent: AgentSummary) {
    setBusy(true)
    try {
      const has = agent.skills.includes(skillId)
      await patchAgentSkills(agent.agent_id, has ? agent.skills.filter((s) => s !== skillId) : [...agent.skills, skillId])
      await reload()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div><h2>Skill Marketplace</h2><p>Skills granted to agents via the Agent Gateway config — the same source Agent Fleet's per-agent Skills tab edits.</p></div>
        <label className="set-search"><Search className="size-3.5" /><input placeholder="Search skills..." value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      </div>
      <div className="set-skill-grid">
        {filteredSkills.map((s) => {
          const granted = agents.filter((a) => a.skills.includes(s))
          return (
            <button key={s} className="set-skill-card" onClick={() => setSelectedSkill(s)}>
              <div className="set-skill-card-top"><code>{s}</code></div>
              <div className="set-skill-card-foot"><div className="avatar-stack">{granted.slice(0, 4).map((a) => <span key={a.agent_id} title={a.display_name}>{initials(a.display_name)}</span>)}</div><small>{granted.length} agent{granted.length === 1 ? '' : 's'} granted</small></div>
            </button>
          )
        })}
        {filteredSkills.length === 0 && <p className="text-xs text-muted-foreground">No skills granted yet.</p>}
      </div>

      {canEdit && <div className="mt-3 flex gap-2"><input value={newSkillId} onChange={(e) => setNewSkillId(e.target.value)} placeholder="new-skill-id" className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /><button disabled={!newSkillId.trim()} onClick={() => setSelectedSkill(newSkillId.trim())} className="text-button">Open grant matrix</button></div>}

      {selectedSkill && (
        <div className="set-modal-scrim" onClick={() => setSelectedSkill(null)}>
          <div className="set-detail" onClick={(e) => e.stopPropagation()}>
            <div className="set-detail-head"><div><code>{selectedSkill}</code></div><button className="set-icon-btn" onClick={() => setSelectedSkill(null)} aria-label="Close"><X className="size-4" /></button></div>
            <p className="set-matrix-title">Per-agent grant matrix</p>
            <div className="set-matrix"><table><thead><tr><th>Agent</th><th>{selectedSkill}</th></tr></thead><tbody>{agents.map((a) => <tr key={a.agent_id}><td>{a.display_name}</td><td><button className={`set-check ${a.skills.includes(selectedSkill) ? 'on' : ''}`} disabled={!canEdit || busy} onClick={() => toggleGrant(selectedSkill, a)} aria-label={`${a.skills.includes(selectedSkill) ? 'Revoke' : 'Grant'} ${selectedSkill} for ${a.display_name}`}>{a.skills.includes(selectedSkill) && <Check className="size-3.5" />}</button></td></tr>)}</tbody></table></div>
          </div>
        </div>
      )}
    </div>
  )
}

/* --------------------------- Notification wizard --------------------------- */

const ALERT_LABELS: Record<AlertLevel, string> = { 'kill-switch': 'Kill-switch trips', 'sign-off': 'Sign-off items', 'go-live': 'Go-live gate passes', daily: 'Daily summary' }

function TelegramWizard({ status, canEdit, onChanged }: { status: NotificationChannelStatus; canEdit: boolean; onChanged: () => void }) {
  const [botToken, setBotToken] = useState('')
  const [chatId, setChatId] = useState('')
  const [detecting, setDetecting] = useState(false)
  const [detectMsg, setDetectMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; at: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleDetect() {
    setDetecting(true)
    setDetectMsg(null)
    try {
      const res = await detectTelegramChatId(botToken)
      if (res.ok && res.chat_id) {
        setChatId(res.chat_id)
        setDetectMsg(`Found: ${res.chat_label ?? res.chat_id}`)
      } else {
        setDetectMsg(res.detail)
      }
    } catch (err) {
      setDetectMsg(err instanceof ApiError ? err.message : 'Detection failed')
    } finally {
      setDetecting(false)
    }
  }

  async function handleSave() {
    setBusy(true)
    setError(null)
    try {
      await writeNotificationChannel('telegram', { enabled: true, bot_token: botToken || undefined, chat_id: chatId || undefined, alert_levels: status.alert_levels.length ? status.alert_levels : ALERT_LEVELS })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleTest() {
    setBusy(true)
    setError(null)
    try {
      const res = await testNotificationChannel('telegram', botToken || chatId ? { bot_token: botToken || undefined, chat_id: chatId || undefined } : undefined)
      setTestResult({ ok: res.ok, at: res.tested_at })
      if (!res.ok) setError(res.error ?? 'Test message failed')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Test failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-wizard">
      <div className="set-wizard-step"><span className="set-wizard-num">1</span><div className="set-wizard-body"><p>Create a bot with @BotFather and copy the token it gives you.</p><a href="https://t.me/BotFather" target="_blank" rel="noreferrer" className="set-btn set-btn-ghost w-fit"><Send className="size-3.5" /> Open @BotFather</a></div></div>
      <div className="set-wizard-step"><span className="set-wizard-num">2</span><div className="set-wizard-body"><p>Paste the bot token here.</p>{canEdit && <div className="set-wizard-inline"><input type="password" placeholder="123456:ABC-DEF..." value={botToken} onChange={(e) => setBotToken(e.target.value)} /></div>}</div></div>
      <div className="set-wizard-step"><span className="set-wizard-num">3</span><div className="set-wizard-body"><p>Send your bot any message (or add it to a group/channel), then detect its chat ID automatically.</p>{canEdit && <div className="set-wizard-inline"><button className="set-btn set-btn-ghost" disabled={!botToken || detecting} onClick={handleDetect}><Wand2 className="size-3.5" /> {detecting ? 'Detecting…' : 'Detect chat ID'}</button><input placeholder="chat ID" value={chatId} onChange={(e) => setChatId(e.target.value)} /></div>}{detectMsg && <p className="text-xs text-muted-foreground">{detectMsg}</p>}</div></div>
      <div className="set-wizard-step"><span className="set-wizard-num">4</span><div className="set-wizard-body"><p>Save, then send a real test message to confirm it lands.</p>
        {error && <p className="text-xs text-rose-300">{error}</p>}
        {canEdit && <div className="set-wizard-inline">
          <button className="set-btn set-btn-primary" disabled={busy || !botToken || !chatId} onClick={handleSave}><Check className="size-3.5" /> Save channel</button>
          <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleTest}><Zap className="size-3.5" /> Send test message</button>
        </div>}
        {testResult && <div className={`set-test-result ${testResult.ok ? 'ok' : 'fail'}`}>{testResult.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}{testResult.ok ? `Delivered — ${new Date(testResult.at).toLocaleTimeString()}` : 'Delivery failed'}</div>}
      </div></div>
    </div>
  )
}

function WebhookWizard({ channel, status, canEdit, onChanged }: { channel: 'discord' | 'slack'; status: NotificationChannelStatus; canEdit: boolean; onChanged: () => void }) {
  const [webhookUrl, setWebhookUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; at: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const helpUrl = channel === 'discord'
    ? 'https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks'
    : 'https://api.slack.com/messaging/webhooks'

  async function handleSave() {
    setBusy(true)
    setError(null)
    try {
      await writeNotificationChannel(channel, { enabled: true, webhook_url: webhookUrl || undefined, alert_levels: status.alert_levels.length ? status.alert_levels : ALERT_LEVELS })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleTest() {
    setBusy(true)
    setError(null)
    try {
      const res = await testNotificationChannel(channel, webhookUrl ? { webhook_url: webhookUrl } : undefined)
      setTestResult({ ok: res.ok, at: res.tested_at })
      if (!res.ok) setError(res.error ?? 'Test message failed')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Test failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-wizard">
      <div className="set-wizard-step"><span className="set-wizard-num">1</span><div className="set-wizard-body"><p>Create an Incoming Webhook in your {channel === 'discord' ? 'Discord channel' : 'Slack app'} settings and copy its URL.</p><a href={helpUrl} target="_blank" rel="noreferrer" className="set-btn set-btn-ghost w-fit"><Webhook className="size-3.5" /> {channel === 'discord' ? 'Discord webhook guide' : 'Slack webhook guide'}</a></div></div>
      <div className="set-wizard-step"><span className="set-wizard-num">2</span><div className="set-wizard-body"><p>Paste the webhook URL here.</p>{canEdit && <div className="set-wizard-inline"><input type="password" placeholder="https://..." value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} /></div>}</div></div>
      <div className="set-wizard-step"><span className="set-wizard-num">3</span><div className="set-wizard-body"><p>Save, then send a real test message to confirm it lands.</p>
        {error && <p className="text-xs text-rose-300">{error}</p>}
        {canEdit && <div className="set-wizard-inline">
          <button className="set-btn set-btn-primary" disabled={busy || !webhookUrl} onClick={handleSave}><Check className="size-3.5" /> Save channel</button>
          <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleTest}><Zap className="size-3.5" /> Send test message</button>
        </div>}
        {testResult && <div className={`set-test-result ${testResult.ok ? 'ok' : 'fail'}`}>{testResult.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}{testResult.ok ? `Delivered — ${new Date(testResult.at).toLocaleTimeString()}` : 'Delivery failed'}</div>}
      </div></div>
      {channel === 'slack' && (
        <div className="set-wizard-step">
          <span className="set-wizard-num"><Lock className="size-3" /></span>
          <div className="set-wizard-body">
            <button className="text-button text-xs" onClick={() => setAdvancedOpen((v) => !v)}>{advancedOpen ? 'Hide' : 'Show'} advanced: Bot Mode</button>
            {advancedOpen && <p>Full bot-token / OAuth install is off by default and not available in this build — Incoming Webhook mode above covers alerting. Ask for Bot Mode if you need slash-command style two-way interaction.</p>}
          </div>
        </div>
      )}
    </div>
  )
}

function ChannelWizardCard({ channel, status, canEdit, onChanged }: { channel: 'telegram' | 'discord' | 'slack'; status: NotificationChannelStatus; canEdit: boolean; onChanged: () => void }) {
  const [prefsOpen, setPrefsOpen] = useState(false)
  const [prefs, setPrefs] = useState<Record<string, boolean>>(() => Object.fromEntries(ALERT_LEVELS.map((l) => [l, status.alert_levels.includes(l)])))
  const [busy, setBusy] = useState(false)

  async function handleDisconnect() {
    setBusy(true)
    try {
      await deleteNotificationChannel(channel)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  async function toggleAlertLevel(level: AlertLevel) {
    const next = { ...prefs, [level]: !prefs[level] }
    setPrefs(next)
    setBusy(true)
    try {
      await writeNotificationChannel(channel, { enabled: status.enabled, alert_levels: ALERT_LEVELS.filter((l) => next[l]) })
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`set-channel ${status.configured && status.enabled ? 'connected' : ''}`}>
      <div className="set-channel-head">
        <div><strong className="capitalize">{channel}</strong><small>{status.configured ? (status.enabled ? 'active' : 'saved, disabled') : 'not configured'}</small></div>
        {canEdit && status.configured && <button className="set-btn set-btn-danger" disabled={busy} onClick={handleDisconnect}><Plug className="size-3.5" /> Disconnect</button>}
      </div>

      <div className="px-0 pb-1">
        {channel === 'telegram' && <TelegramWizard status={status} canEdit={canEdit} onChanged={onChanged} />}
        {channel === 'discord' && <WebhookWizard channel="discord" status={status} canEdit={canEdit} onChanged={onChanged} />}
        {channel === 'slack' && <WebhookWizard channel="slack" status={status} canEdit={canEdit} onChanged={onChanged} />}
      </div>

      <fieldset className="set-alert-prefs" disabled={!status.configured || !canEdit}>
        <legend>Alert levels</legend>
        {ALERT_LEVELS.map((l) => <label key={l} className="set-alert-pref"><button type="button" className={`set-check ${prefs[l] ? 'on' : ''}`} onClick={() => toggleAlertLevel(l)} aria-label={`${prefs[l] ? 'Disable' : 'Enable'} ${ALERT_LABELS[l]} on ${channel}`}>{prefs[l] && <Check className="size-3.5" />}</button><span>{ALERT_LABELS[l]}</span></label>)}
      </fieldset>
    </div>
  )
}

function NotificationsSection({ onStatusChanged }: { onStatusChanged: () => void }) {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [statuses, setStatuses] = useState<NotificationChannelStatus[]>([])

  async function reload() {
    setStatuses(await listNotificationChannels())
    onStatusChanged()
  }
  useEffect(() => { reload() }, [])

  const byChannel = (name: string) => statuses.find((s) => s.channel === name) ?? { channel: name, configured: false, enabled: false, allowed_sender_ids: [], alert_levels: [] }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Notification Channels</h2><p>A guided setup per channel, ending in a real test message you can watch land in your chat.</p></div></div>
      <div className="set-channel-grid">{NOTIFICATION_CHANNELS.map((c) => <ChannelWizardCard key={c} channel={c as 'telegram' | 'discord' | 'slack'} status={byChannel(c)} canEdit={canEdit} onChanged={reload} />)}</div>
    </div>
  )
}

/* ------------------------------- Risk Limits -------------------------------- */

function RiskSection() {
  const { role, user } = useAuth()
  const canOperate = role === 'SystemAdministrator' || role === 'RiskManager'
  const [limits, setLimits] = useState<RiskLimit[]>([])
  const [changes, setChanges] = useState<RiskLimitChange[]>([])
  const [limitName, setLimitName] = useState('')
  const [proposedValue, setProposedValue] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    const [l, c] = await Promise.all([listRiskLimits(), listRiskLimitChanges()])
    setLimits(l)
    setChanges(c.filter((x) => x.status === 'staged' || x.status === 'confirmed'))
  }
  useEffect(() => { reload() }, [])

  async function handleStage() {
    if (!limitName.trim() || !proposedValue) return
    setBusy(true)
    setError(null)
    try {
      await stageRiskLimitChange(limitName.trim(), Number(proposedValue), reason || undefined)
      setLimitName(''); setProposedValue(''); setReason('')
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to stage change')
    } finally {
      setBusy(false)
    }
  }

  async function handleConfirm(changeId: string) {
    setBusy(true)
    setError(null)
    try {
      await confirmRiskLimitChange(changeId)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Confirmation failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleApply(changeId: string) {
    setBusy(true)
    setError(null)
    try {
      await applyRiskLimitChange(changeId)
      await reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Apply failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Risk Limits</h2><p>Thresholds change only through dual control &mdash; stage &rarr; a different user confirms &rarr; apply.</p></div></div>
      <div className="set-dualcontrol-banner"><Lock className="size-4" /><div><strong>Dual-control enforced</strong><p>Confirming your own staged change is rejected server-side — a genuinely different user must confirm.</p></div></div>
      {error && <div className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}

      <p className="set-group-label">Current effective limits</p>
      <div className="set-risk-list">{limits.map((l) => <div key={l.name} className="set-risk-row"><div className="set-risk-meta"><span className="set-risk-label">{l.name}</span><strong className="mono">{l.value}</strong></div></div>)}{limits.length === 0 && <p className="text-xs text-muted-foreground">No risk limits set yet.</p>}</div>

      {canOperate && <div className="mt-4 flex flex-wrap items-end gap-2 rounded-xl border border-white/10 p-3">
        <label className="set-field"><span>Limit name</span><input value={limitName} onChange={(e) => setLimitName(e.target.value)} placeholder="max_drawdown_pct" /></label>
        <label className="set-field"><span>Proposed value</span><input value={proposedValue} onChange={(e) => setProposedValue(e.target.value)} inputMode="decimal" /></label>
        <label className="set-field"><span>Reason</span><input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <button className="set-btn set-btn-primary" disabled={busy || !limitName.trim() || !proposedValue} onClick={handleStage}>Stage change</button>
      </div>}

      <p className="set-group-label mt-4">Pending changes</p>
      <div className="set-risk-list">
        {changes.map((c) => (
          <div key={c.id} className={`set-risk-row set-risk-${c.status === 'staged' ? 'staged' : 'awaiting'}`}>
            <div className="set-risk-meta"><span className="set-risk-label">{c.limit_name}</span><strong className="mono">→ {c.proposed_value}</strong><small className="block text-muted-foreground">staged by {c.staged_by === user?.id ? 'you' : c.staged_by.slice(0, 8)}{c.reason ? ` · ${c.reason}` : ''}</small></div>
            {c.status === 'staged' && canOperate && <button className="set-btn set-btn-primary" disabled={busy || c.staged_by === user?.id} onClick={() => handleConfirm(c.id)}>{c.staged_by === user?.id ? 'Needs a different confirmer' : 'Confirm as second user'}</button>}
            {c.status === 'confirmed' && canOperate && <button className="set-btn set-btn-primary" disabled={busy} onClick={() => handleApply(c.id)}><CheckCircle2 className="size-3.5" /> Apply</button>}
          </div>
        ))}
        {changes.length === 0 && <p className="text-xs text-muted-foreground">No changes staged or awaiting confirmation.</p>}
      </div>
    </div>
  )
}

/* --------------------------------- Page ----------------------------------- */

export default function SettingsPage() {
  const [section, setSection] = useState<SectionId>('gateway')
  const [oauthBanner, setOauthBanner] = useState<{ broker: string; ok: boolean; error?: string } | null>(null)
  // Bumped by whichever of the three status-strip-backing sections
  // (broker/LLM/notifications) just reloaded its own data, so the strip
  // re-fetches immediately instead of only on the next full page load.
  const [statusRefreshKey, setStatusRefreshKey] = useState(0)
  const bumpStatus = useCallback(() => setStatusRefreshKey((k) => k + 1), [])

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const oauth = params.get('oauth')
    const broker = params.get('broker')
    const sectionParam = params.get('section') as SectionId | null
    if (sectionParam && SECTIONS.some((s) => s.id === sectionParam)) setSection(sectionParam)
    if (oauth && broker) {
      setOauthBanner({ broker, ok: oauth === 'success', error: params.get('error') ?? undefined })
      window.history.replaceState(null, '', '/settings')
    }
  }, [])

  return (
    <ShellLayout>
      <main className="set-page mx-auto w-full max-w-[1500px] pb-10">
        <header className="set-header">
          <p className="eyebrow">TRADINGOS // CONTROL PLANE</p>
          <h1>Settings</h1>
          <p>Gateway configuration, LLM routing, broker connections, notifications, skill grants, and dual-controlled risk limits.</p>
        </header>

        <StatusStrip onNavigate={setSection} refreshKey={statusRefreshKey} />

        <div className="set-shell">
          <nav className="set-subnav" aria-label="Settings sections">
            {SECTIONS.map((s) => {
              const Icon = s.icon
              const active = section === s.id
              return (
                <button key={s.id} className={active ? 'active' : ''} onClick={() => setSection(s.id)}>
                  <Icon className="size-4" />
                  <span className="set-subnav-text"><strong>{s.label}</strong><small>{s.desc}</small></span>
                  <ChevronRight className="set-subnav-caret size-3.5" />
                </button>
              )
            })}
          </nav>

          <section className="set-content">
            {section === 'gateway' && <GatewaySection />}
            {section === 'llm' && <LlmProvidersSection onStatusChanged={bumpStatus} />}
            {section === 'broker' && <BrokerConfigSection oauthBanner={oauthBanner} onStatusChanged={bumpStatus} />}
            {section === 'notifications' && <NotificationsSection onStatusChanged={bumpStatus} />}
            {section === 'skills' && <SkillsSection />}
            {section === 'risk' && <RiskSection />}
          </section>
        </div>
      </main>
    </ShellLayout>
  )
}
