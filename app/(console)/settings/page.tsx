'use client'

import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Bell,
  Braces,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Copy,
  Cpu,
  DatabaseBackup,
  Users,
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
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import { TabList } from '@/components/ui/tab-list'
import { AlertmanagerStatus } from '@/components/settings/alertmanager-status'
import { BackupsSection } from '@/components/settings/backups-section'
import { SignInPolicyCard } from '@/components/settings/sign-in-policy'
import { UsersSection } from '@/components/settings/users-section'
import { describeQuoteRefusals } from '@/lib/broker-quotes'
import { editFailure, skillTags, skillsAfter } from '@/lib/gateway-edit'
import { indiaStamp } from '@/lib/time'
import { InboundSettings } from '@/components/settings/inbound-settings'
import { RiskLimitsSection } from '@/components/settings/risk-limits-section'
import { SigningKeyCard } from '@/components/settings/signing-key-card'
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
  type SkillCatalogEntry,
  ALERT_LEVELS,
  ApiError,
  LLM_PROVIDERS,
  LLM_PROVIDER_LABELS,
  NOTIFICATION_CHANNELS,
  deleteBrokerCredentials,
  fallbackBrokerRedirectUri,
  deleteLlmProviderCredentials,
  setLlmProviderDefaultModel,
  deleteNotificationChannel,
  detectTelegramChatId,
  getAgents,
  getBrokerLoginUrl,
  getGatewayConfig,
  getSkillCatalog,
  listBrokerCredentialStatus,
  listGatewayConfigVersions,
  listLlmProviderModels,
  listLlmProviderStatus,
  listNotificationChannels,
  patchAgentSettings,
  putGatewayConfig,
  putLlmProviderOrder,
  rollbackGatewayConfig,
  testLlmProvider,
  testNotificationChannel,
  validateGatewayConfig,
  writeBrokerCredentials,
  writeBrokerRedirectUri,
  writeLlmProviderCredentials,
  updateNotificationChannel,
  writeNotificationChannel,
} from '@/lib/api'
import { can } from '@/lib/permissions'
import { type SectionId, sectionFromQuery, sectionToUrl } from '@/lib/settings-view'

const SECTIONS: { id: SectionId; label: string; icon: typeof Braces; desc: string }[] = [
  { id: 'gateway', label: 'Agent Gateway Config', icon: Braces, desc: 'Infra & agent defaults' },
  { id: 'llm', label: 'LLM Providers', icon: Cpu, desc: 'Keys, test, fallback order' },
  { id: 'broker', label: 'Broker Config', icon: KeyRound, desc: 'Zerodha / Upstox OAuth' },
  { id: 'notifications', label: 'Notification Channels', icon: Bell, desc: 'Telegram / Discord / Slack' },
  { id: 'skills', label: 'Skill Marketplace', icon: Sparkles, desc: 'Grant tools to agents' },
  { id: 'risk', label: 'Risk Limits', icon: SlidersHorizontal, desc: 'Dual-control thresholds' },
  { id: 'users', label: 'Users', icon: Users, desc: 'Accounts, roles, locks' },
  { id: 'security', label: 'Security', icon: Lock, desc: 'Sign-in rules, signing key' },
  { id: 'backups', label: 'System and backups', icon: DatabaseBackup, desc: 'Database and data lake backups' },
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

/** Tracks whether a section's data has loaded and why not, so a failed read shows an error rather than
 * empty cards that read as "nothing is configured". */
function useSectionLoad() {
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  async function track(work: () => Promise<void>) {
    try {
      await work()
      setLoaded(true)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error && err.message ? err.message : 'Could not load this section')
    }
  }
  return { loaded, loadError, track }
}

function SectionStatus({ load, what, onRetry }: { load: { loaded: boolean; loadError: string | null }; what: string; onRetry: () => void }) {
  if (load.loadError) return <ErrorNotice className="mb-3" message={`Could not load ${what}: ${load.loadError}`} onRetry={onRetry} />
  if (!load.loaded) return <p className="text-xs text-muted-foreground">Loading {what}…</p>
  return null
}

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
          // The broker's own word beats the clock: a token that "should" be valid but is being refused is not connected.
          detail: b.feed_rejected_at ? 'token rejected' : b.token_status === 'valid' ? 'connected' : b.token_status === 'expired' ? 'expired' : 'not connected',
          color: b.feed_rejected_at ? 'red' : b.token_status === 'valid' ? 'green' : b.token_status === 'expired' ? 'amber' : 'gray',
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
  const canEdit = can(role, 'PUT /api/v1/gateway/config')
  const [tab, setTab] = useState<'form' | 'raw'>('form')
  const [config, setConfig] = useState<GatewayConfigResponse | null>(null)
  const [rawText, setRawText] = useState('')
  const [versions, setVersions] = useState<ConfigVersionSummary[]>([])
  const [validateErrors, setValidateErrors] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rollingBack, setRollingBack] = useState<ConfigVersionSummary | null>(null)

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
      if (config?.version_id == null) throw new Error('The configuration has not loaded yet, so nothing can be saved.')
      const result = await putGatewayConfig(rawText, config.version_id)
      await reload()
      setValidateErrors(result.status === 'rejected' ? result.errors : null)
      // A text the app cannot run on is recorded as a rejected version and the file is left as it was.
      if (result.status === 'rejected') setError('That text was not applied: it is not a configuration the app can run on. The file and the running configuration are unchanged.')
    } catch (err) {
      const failure = editFailure(err, 'Save failed')
      setError(failure.message)
      // The editor still holds what the person typed over the version they read: show the current one.
      if (failure.stale) await reload().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  // Confirmed in the dialog below, which reports a failure by the error this throws.
  async function handleRollback(versionId: number) {
    await rollbackGatewayConfig(versionId)
    await reload()
  }

  const parsed = config?.parsed as Record<string, any> | undefined

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div><h2>Agent Gateway Config</h2><p>Resolved configuration across infra defaults, agent defaults, per-agent overrides, and channel bindings.</p></div>
        <TabList label="Gateway config view" className="set-tabgroup">
          <button role="tab" aria-selected={tab === 'form'} tabIndex={tab === 'form' ? 0 : -1} className={tab === 'form' ? 'active' : ''} onClick={() => setTab('form')}><SlidersHorizontal className="size-3.5" /> Summary</button>
          <button role="tab" aria-selected={tab === 'raw'} tabIndex={tab === 'raw' ? 0 : -1} className={tab === 'raw' ? 'active' : ''} onClick={() => setTab('raw')}><Braces className="size-3.5" /> Raw JSON</button>
        </TabList>
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
              <span className="set-version-when mono">{indiaStamp(cv.created_at)}</span>
              <span className="set-version-author">{cv.source}</span>
              <span className="set-version-note">{cv.status}</span>
              {cv.id === config?.version_id ? <span className="set-version-current">Current</span> : canEdit ? <button className="set-rollback" disabled={busy} onClick={() => setRollingBack(cv)}><RotateCcw className="size-3" /> Rollback</button> : null}
            </li>
          ))}
        </ul>
      </div>
      {rollingBack && (
        <ConfirmDialog
          title={`Roll back to v${rollingBack.id}?`}
          description={`The configuration written as v${rollingBack.id} (${rollingBack.source}, ${indiaStamp(rollingBack.created_at)} India time) is applied again as a new version, in place of the one running now. Every agent, binding and provider order in it takes effect.`}
          confirmLabel={`Roll back to v${rollingBack.id}`}
          busyLabel="Rolling back…"
          onConfirm={() => handleRollback(rollingBack.id)}
          onClose={() => setRollingBack(null)}
        />
      )}
    </div>
  )
}

/* ------------------------------- LLM Providers ----------------------------- */

const LOCAL_PROVIDERS: LlmProviderName[] = ['ollama', 'custom']

async function readGatewayOrder(): Promise<{ versionId: number | null; order: string[] }> {
  const config = await getGatewayConfig()
  const infra = config.parsed.infra as { llmProviders?: { order?: string[] } } | undefined
  return { versionId: config.version_id, order: infra?.llmProviders?.order ?? [] }
}

/** Replaces the fallback order and nothing else in the config, on the version the section read. */
async function changeProviderOrder(next: string[], versionId: number | null) {
  if (versionId === null) throw new Error('The configuration has not loaded yet, so the order cannot be changed.')
  await putLlmProviderOrder(next, versionId)
}

// Providers with no universal default model: an agent's "auto" can't run on
// them until the operator picks one (otherwise the router skips them).
const NEEDS_DEFAULT_MODEL: LlmProviderName[] = ['ollama', 'custom', 'huggingface']

const API_KEY_PLACEHOLDERS: Partial<Record<LlmProviderName, string>> = {
  huggingface: 'Access token (hf_…)',
}

async function setProviderInOrder(provider: string, include: boolean, order: string[], versionId: number | null) {
  if (order.includes(provider) === include) return
  await changeProviderOrder(include ? [...order, provider] : order.filter((p) => p !== provider), versionId)
}

function effectiveDefaultModel(status: LlmProviderStatus): string | null {
  return status.default_model ?? status.builtin_default_model ?? null
}

function LlmProviderCard({ status, canEdit, order, versionId, onChanged }: { status: LlmProviderStatus; canEdit: boolean; order: string[]; versionId: number | null; onChanged: () => void }) {
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
  const [removing, setRemoving] = useState(false)
  const defaultModel = effectiveDefaultModel(status)
  const missingDefault = status.configured && !defaultModel && NEEDS_DEFAULT_MODEL.includes(provider)

  async function run(action: () => Promise<void>, failure: string) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      const failed = editFailure(err, failure)
      setError(failed.message)
      if (failed.stale) onChanged()
    } finally {
      setBusy(false)
    }
  }

  const handleSave = () => run(async () => {
    await writeLlmProviderCredentials(provider, isLocal ? undefined : apiKey || undefined, baseUrl || undefined)
    setApiKey('')
    // A newly configured provider joins the fallback chain; the switch
    // above can take it back out.
    if (!status.configured) await setProviderInOrder(provider, true, order, versionId)
  }, 'Save failed')

  // Out of the chain first: if that is refused (it is the last one), the key is still there.
  const removeKey = async () => {
    await setProviderInOrder(provider, false, order, versionId)
    await deleteLlmProviderCredentials(provider)
    onChanged()
  }

  const handleToggleOrder = () => run(() => setProviderInOrder(provider, !status.in_fallback_order, order, versionId), 'Failed to update fallback order')

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
            {status.configured && <button className="set-btn set-btn-secondary" disabled={busy} onClick={() => setRemoving(true)}>Remove</button>}
          </div>
        </>
      )}

      {removing && (
        <ConfirmDialog
          title={`Remove the ${LLM_PROVIDER_LABELS[provider] ?? provider} key?`}
          description={`${LLM_PROVIDER_LABELS[provider] ?? provider} is taken out of the fallback chain and its saved key or address is deleted. Agents stop using it from their next call, and the key cannot be read back, so it has to be entered again to use it.`}
          tone="danger"
          confirmLabel="Remove"
          busyLabel="Removing…"
          onConfirm={removeKey}
          onClose={() => setRemoving(false)}
        />
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
        {canEdit && <button className="set-btn set-btn-secondary" disabled={busy || !status.configured || (!selectedModel && !defaultModel)} onClick={handleTest}>Test connection</button>}
        {testResult && (
          <span className={`set-llm-test-result ${testResult.ok ? 'ok' : 'fail'}`}>{testResult.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}{testResult.detail}</span>
        )}
      </div>
    </div>
  )
}

function FallbackPriorityList({ canEdit, order, versionId, statuses, onChanged }: { canEdit: boolean; order: string[]; versionId: number | null; statuses: LlmProviderStatus[]; onChanged: () => void }) {
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
      await changeProviderOrder(next, versionId)
    } catch (err) {
      setError(editFailure(err, 'Failed to reorder').message)
    } finally {
      setBusy(false)
      onChanged()
    }
  }

  function move(from: number, to: number) {
    if (from === to || to < 0 || to >= order.length) return
    const next = [...order]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    persist(next)
  }

  function handleDrop(targetIndex: number) {
    const from = dragIndex
    setDragIndex(null)
    if (from !== null) move(from, targetIndex)
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
            {canEdit && (
              <span className="set-llm-priority-move">
                <button type="button" className="set-icon-btn" disabled={busy || i === 0} onClick={() => move(i, i - 1)} aria-label={`Move ${LLM_PROVIDER_LABELS[provider as LlmProviderName] ?? provider} up`}><ArrowUp className="size-3" /></button>
                <button type="button" className="set-icon-btn" disabled={busy || i === order.length - 1} onClick={() => move(i, i + 1)} aria-label={`Move ${LLM_PROVIDER_LABELS[provider as LlmProviderName] ?? provider} down`}><ArrowDown className="size-3" /></button>
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function LlmProvidersSection({ onStatusChanged }: { onStatusChanged: () => void }) {
  const { role } = useAuth()
  const canEdit = can(role, 'POST /api/v1/settings/llm-providers/{provider}')
  const [statuses, setStatuses] = useState<LlmProviderStatus[]>([])
  const [order, setOrder] = useState<string[]>([])
  const [versionId, setVersionId] = useState<number | null>(null)
  const load = useSectionLoad()

  async function reload() {
    await load.track(async () => {
      const [nextStatuses, { order: nextOrder, versionId: nextVersion }] = await Promise.all([listLlmProviderStatus(), readGatewayOrder()])
      setStatuses(nextStatuses)
      setOrder(nextOrder)
      setVersionId(nextVersion)
      onStatusChanged()
    })
  }
  useEffect(() => { reload() }, [])

  const byProvider = (name: string): LlmProviderStatus => statuses.find((s) => s.provider === name) ?? { provider: name, configured: false, base_url: null, in_fallback_order: order.includes(name) }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>LLM Providers</h2><p>API keys are write-only. Fallback priority here is the exact same infra.llmProviders.order the Agent Gateway routes on — this is a UI over that config, not a separate store. Agents ask for model &quot;auto&quot;, which runs each provider&apos;s default model.</p></div></div>
      <SectionStatus load={load} what="the LLM providers" onRetry={reload} />
      {load.loaded && <><div className="set-llm-grid">{LLM_PROVIDERS.map((p) => <LlmProviderCard key={p} status={byProvider(p)} canEdit={canEdit} order={order} versionId={versionId} onChanged={reload} />)}</div>

      <p className="set-group-label">Fallback priority</p>
      <FallbackPriorityList canEdit={canEdit} order={order} versionId={versionId} statuses={statuses} onChanged={reload} /></>}
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
  const [removing, setRemoving] = useState(false)

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

  // Confirmed in the dialog below, which reports a failure by the error this throws.
  async function removeCredentials() {
    await deleteBrokerCredentials(status.broker)
    onChanged()
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

  const rejected = Boolean(status.feed_rejected_at)
  const tokenLabel =
    rejected
      ? 'Rejected by the broker — reconnect'
      : status.token_status === 'valid' && status.token_expires_at
      ? `Connected — expires ${formatIstTime(status.token_expires_at)}`
      : status.token_status === 'expired'
        ? 'Expired — click to reconnect'
        : 'Not connected'

  return (
    <div className={`set-broker-card ${status.token_status === 'valid' && !rejected ? 'connected' : ''}`}>
      <div className="set-broker-head"><strong className="capitalize">{status.broker}</strong></div>
      <div className={`set-token-strip ${rejected ? 'rejected' : status.token_status}`}><Timer className="size-3.5" />{tokenLabel}</div>
      {rejected && status.feed_rejected_at && (
        <div className="set-reminder" role="alert">
          <AlertTriangle className="size-3.5 shrink-0" />
          <span>
            {status.broker} refused the stored token (HTTP {status.feed_rejection_status}) at {formatIstTime(status.feed_rejected_at)}, so live quotes are not flowing. The expiry shown elsewhere is only an estimate. Reconnect to fix it.
          </span>
        </div>
      )}
      {describeQuoteRefusals(status) && (
        <div className="set-reminder" data-testid={`quote-refusals-${status.broker}`}>
          <AlertTriangle className="size-3.5 shrink-0" />
          <span>{describeQuoteRefusals(status)}</span>
        </div>
      )}
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
          {status.configured && <button className="set-btn set-btn-secondary" disabled={busy} onClick={() => setRemoving(true)}>Remove</button>}
        </div>
        {removing && (
          <ConfirmDialog
            title={`Remove the ${status.broker} credentials?`}
            description={`The saved key, secret and token for ${status.broker} are deleted and cannot be read back. Live quotes and orders through it stop until the key and secret are entered again and the broker is reconnected.`}
            tone="danger"
            confirmLabel="Remove"
            busyLabel="Removing…"
            onConfirm={removeCredentials}
            onClose={() => setRemoving(false)}
          />
        )}

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
  const canEdit = can(role, 'POST /api/v1/broker-credentials/{broker}')
  const [statuses, setStatuses] = useState<BrokerCredentialStatus[]>([])
  const load = useSectionLoad()

  async function reload() {
    await load.track(async () => {
      setStatuses(await listBrokerCredentialStatus())
      onStatusChanged()
    })
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
      <SectionStatus load={load} what="the broker credentials" onRetry={reload} />
      {load.loaded && <div className="set-cred-grid">{statuses.map((c) => <BrokerConfigCard key={c.broker} status={c} canEdit={canEdit} onChanged={reload} />)}</div>}
    </div>
  )
}

/* ------------------------------- Section 2 -------------------------------- */

function initials(name: string) {
  return name.replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase()
}

function SkillsSection() {
  const { role } = useAuth()
  const canEdit = can(role, 'PATCH /api/v1/agents/{agent_id}')
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [catalog, setCatalog] = useState<SkillCatalogEntry[]>([])
  const [query, setQuery] = useState('')
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useSectionLoad()
  async function reload() { await load.track(async () => { const [nextAgents, nextCatalog] = await Promise.all([getAgents(), getSkillCatalog()]); setAgents(nextAgents); setCatalog(nextCatalog) }) }
  useEffect(() => { reload() }, [])

  const filteredSkills = catalog.filter((s) => s.skill_id.toLowerCase().includes(query.toLowerCase()) || s.summary.toLowerCase().includes(query.toLowerCase()))
  const selected = catalog.find((s) => s.skill_id === selectedSkill) ?? null

  async function toggleGrant(skillId: string, agent: AgentSummary) {
    setBusy(true)
    setError(null)
    try {
      if (agent.config_version_id === null) throw new Error('The configuration has not loaded yet, so nothing can be changed.')
      await patchAgentSettings(agent.agent_id, { skills: skillsAfter(agent.skills, skillId, !agent.skills.includes(skillId)) }, agent.config_version_id)
      await reload()
    } catch (err) {
      const failure = editFailure(err, 'Could not change that grant')
      setError(failure.message)
      if (failure.stale) await reload()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div><h2>Skill Marketplace</h2><p>The skills the app ships with, who holds each, and whether it does anything yet. Granting is a choice from this fixed set, on the same configuration Agent Fleet&apos;s per-agent Skills tab edits.</p></div>
        <label className="set-search"><Search className="size-3.5" /><input placeholder="Search skills..." aria-label="Search skills" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      </div>
      <SectionStatus load={load} what="the skills" onRetry={reload} />
      <div className="set-skill-grid">
        {filteredSkills.map((s) => {
          const granted = agents.filter((a) => a.skills.includes(s.skill_id))
          return (
            <button key={s.skill_id} className="set-skill-card" onClick={() => { setError(null); setSelectedSkill(s.skill_id) }}>
              <div className="set-skill-card-top"><code>{s.skill_id}</code>{skillTags(s).map((tag) => <small key={tag} className="skill-tag">{tag}</small>)}</div>
              <p className="set-skill-summary">{s.summary}</p>
              <div className="set-skill-card-foot"><div className="avatar-stack">{granted.slice(0, 4).map((a) => <span key={a.agent_id} title={a.display_name}>{initials(a.display_name)}</span>)}</div><small>{granted.length} agent{granted.length === 1 ? '' : 's'} granted</small></div>
            </button>
          )
        })}
        {load.loaded && filteredSkills.length === 0 && <p className="text-xs text-muted-foreground">{catalog.length === 0 ? 'The app lists no skills.' : 'No skill matches that search.'}</p>}
      </div>

      {selected && (
        <div className="set-modal-scrim" onClick={() => setSelectedSkill(null)}>
          <div className="set-detail" role="dialog" aria-modal="true" aria-label={`Grants of ${selected.skill_id}`} onClick={(e) => e.stopPropagation()}>
            <div className="set-detail-head"><div><code>{selected.skill_id}</code></div><button className="set-icon-btn" onClick={() => setSelectedSkill(null)} aria-label="Close"><X className="size-4" /></button></div>
            <p className="set-matrix-title">{selected.summary}</p>
            {error && <ErrorNotice className="mb-2" message={error} />}
            <div className="set-matrix"><table><thead><tr><th>Agent</th><th>{selected.skill_id}</th></tr></thead><tbody>{agents.map((a) => <tr key={a.agent_id}><td>{a.display_name}</td><td><button className={`set-check ${a.skills.includes(selected.skill_id) ? 'on' : ''}`} disabled={!canEdit || busy} onClick={() => toggleGrant(selected.skill_id, a)} aria-label={`${a.skills.includes(selected.skill_id) ? 'Revoke' : 'Grant'} ${selected.skill_id} for ${a.display_name}`}>{a.skills.includes(selected.skill_id) && <Check className="size-3.5" />}</button></td></tr>)}</tbody></table></div>
          </div>
        </div>
      )}
    </div>
  )
}

/* --------------------------- Notification wizard --------------------------- */

const ALERT_LABELS: Record<AlertLevel, string> = { 'kill-switch': 'Kill-switch trips', 'sign-off': 'Sign-off items', 'go-live': 'Go-live gate passes', daily: 'Daily summary', engine: 'Engine alerts' }

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
      <div className="set-wizard-step"><span className="set-wizard-num">4</span><div className="set-wizard-body"><p>Save, then send a real test message to confirm it lands.{!status.configured && ' Saving turns on every alert level; change them under Alert levels below.'}</p>
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
      <div className="set-wizard-step"><span className="set-wizard-num">3</span><div className="set-wizard-body"><p>Save, then send a real test message to confirm it lands.{!status.configured && ' Saving turns on every alert level; change them under Alert levels below.'}</p>
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
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState(false)

  // Confirmed in the dialog below, which reports a failure by the error this throws.
  async function handleDisconnect() {
    try {
      await deleteNotificationChannel(channel)
    } catch (err) {
      // 404 means it was already disconnected (another tab, a double click): the dialog says so,
      // and the card refreshes to show the real state.
      onChanged()
      throw err
    }
    onChanged()
  }

  async function toggleAlertLevel(level: AlertLevel) {
    const previous = prefs
    const next = { ...prefs, [level]: !prefs[level] }
    setPrefs(next)
    setBusy(true)
    setError(null)
    try {
      await updateNotificationChannel(channel, { alert_levels: ALERT_LEVELS.filter((l) => next[l]) })
      onChanged()
    } catch (err) {
      setPrefs(previous) // the change was not saved: do not leave the toggle showing it
      setError(err instanceof ApiError ? err.message : 'Could not save the alert levels')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`set-channel ${status.configured && status.enabled ? 'connected' : ''}`}>
      <div className="set-channel-head">
        <div><strong className="capitalize">{channel}</strong><small>{status.configured ? (status.enabled ? 'active' : 'saved, disabled') : 'not configured'}</small></div>
        {canEdit && status.configured && <button className="set-btn set-btn-danger" disabled={busy} onClick={() => setDisconnecting(true)}><Plug className="size-3.5" /> Disconnect</button>}
      </div>
      {disconnecting && (
        <ConfirmDialog
          title={`Disconnect ${channel}?`}
          description={`The saved credentials and settings for ${channel} are deleted. Alerts stop going to it, including kill-switch trips, until it is set up again.`}
          tone="danger"
          confirmLabel="Disconnect"
          busyLabel="Disconnecting…"
          onConfirm={handleDisconnect}
          onClose={() => setDisconnecting(false)}
        />
      )}
      {error && <p className="text-xs text-rose-300" role="alert">{error}</p>}

      <div className="px-0 pb-1">
        {channel === 'telegram' && <TelegramWizard status={status} canEdit={canEdit} onChanged={onChanged} />}
        {channel === 'discord' && <WebhookWizard channel="discord" status={status} canEdit={canEdit} onChanged={onChanged} />}
        {channel === 'slack' && <WebhookWizard channel="slack" status={status} canEdit={canEdit} onChanged={onChanged} />}
      </div>

      {status.configured && <InboundSettings channel={channel} status={status} canEdit={canEdit} onChanged={onChanged} />}

      <fieldset className="set-alert-prefs" disabled={!status.configured || !canEdit}>
        <legend>Alert levels</legend>
        {ALERT_LEVELS.map((l) => <label key={l} className="set-alert-pref"><button type="button" className={`set-check ${prefs[l] ? 'on' : ''}`} onClick={() => toggleAlertLevel(l)} aria-label={`${prefs[l] ? 'Disable' : 'Enable'} ${ALERT_LABELS[l]} on ${channel}`}>{prefs[l] && <Check className="size-3.5" />}</button><span>{ALERT_LABELS[l]}</span></label>)}
      </fieldset>
    </div>
  )
}

function NotificationsSection({ onStatusChanged }: { onStatusChanged: () => void }) {
  const { role } = useAuth()
  const canEdit = can(role, 'POST /api/v1/notification-channels/{channel}')
  const [statuses, setStatuses] = useState<NotificationChannelStatus[]>([])
  const load = useSectionLoad()

  async function reload() {
    await load.track(async () => {
      setStatuses(await listNotificationChannels())
      onStatusChanged()
    })
  }
  useEffect(() => { reload() }, [])

  const byChannel = (name: string) => statuses.find((s) => s.channel === name) ?? { channel: name, configured: false, enabled: false, allowed_sender_ids: [], alert_levels: [] }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Notification Channels</h2><p>A guided setup per channel, ending in a real test message you can watch land in your chat.</p></div></div>
      <SectionStatus load={load} what="the notification channels" onRetry={reload} />
      {load.loaded && <div className="set-channel-grid">{NOTIFICATION_CHANNELS.map((c) => <ChannelWizardCard key={c} channel={c as 'telegram' | 'discord' | 'slack'} status={byChannel(c)} canEdit={canEdit} onChanged={reload} />)}</div>}
      {load.loaded && <AlertmanagerStatus />}
    </div>
  )
}

/* --------------------------------- Page ----------------------------------- */

export default function SettingsPage() {
  const [section, setSection] = useState<SectionId>('gateway')
  const [urlRead, setUrlRead] = useState(false)
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
    const fromAddress = sectionFromQuery(window.location.search)
    if (fromAddress) setSection(fromAddress)
    if (oauth && broker) {
      setOauthBanner({ broker, ok: oauth === 'success', error: params.get('error') ?? undefined })
      window.history.replaceState(null, '', '/settings')
    }
    setUrlRead(true)
  }, [])

  // The address follows the section, so a reload or a shared link opens the same one.
  useEffect(() => {
    if (!urlRead) return
    window.history.replaceState(null, '', sectionToUrl(section))
  }, [section, urlRead])

  return (
    <>
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
                <button key={s.id} className={active ? 'active' : ''} aria-current={active ? 'page' : undefined} onClick={() => setSection(s.id)}>
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
            {section === 'risk' && <RiskLimitsSection />}
            {section === 'users' && <UsersSection />}
            {section === 'security' && (
              <>
                <SignInPolicyCard />
                <SigningKeyCard />
              </>
            )}
            {section === 'backups' && <BackupsSection />}
          </section>
        </div>
      </main>
    </>
  )
}
