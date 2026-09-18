'use client'

import { useEffect, useMemo, useState } from 'react'
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
  RefreshCw,
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
import { useAuth } from '@/components/auth/auth-provider'
import {
  type AgentSummary,
  type AlertLevel,
  type BrokerCredentialStatus,
  type ConfigVersionSummary,
  type GatewayConfigResponse,
  type NotificationChannelStatus,
  type RiskLimit,
  type RiskLimitChange,
  ALERT_LEVELS,
  ApiError,
  KNOWN_BROKERS,
  NOTIFICATION_CHANNELS,
  applyRiskLimitChange,
  confirmRiskLimitChange,
  deleteBrokerCredentials,
  deleteNotificationChannel,
  getAgents,
  getGatewayConfig,
  listBrokerCredentialStatus,
  listGatewayConfigVersions,
  listNotificationChannels,
  listRiskLimitChanges,
  listRiskLimits,
  putGatewayConfig,
  rollbackGatewayConfig,
  stageRiskLimitChange,
  validateGatewayConfig,
  writeBrokerCredentials,
  writeNotificationChannel,
} from '@/lib/api'

type SectionId = 'gateway' | 'skills' | 'credentials' | 'notifications' | 'risk'

const SECTIONS: { id: SectionId; label: string; icon: typeof Braces; desc: string }[] = [
  { id: 'gateway', label: 'Agent Gateway Config', icon: Braces, desc: 'Infra & agent defaults' },
  { id: 'skills', label: 'Skill Marketplace', icon: Sparkles, desc: 'Grant tools to agents' },
  { id: 'credentials', label: 'Broker Credentials', icon: KeyRound, desc: 'Write-only secrets' },
  { id: 'notifications', label: 'Notification Channels', icon: Bell, desc: 'Telegram / Discord / Slack' },
  { id: 'risk', label: 'Risk Limits', icon: SlidersHorizontal, desc: 'Dual-control thresholds' },
]

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
            <p className="text-xs text-muted-foreground">{parsed?.llmProviders?.order?.join(' → ') ?? '—'}</p>
          </fieldset>
          <fieldset className="set-fieldset">
            <legend>Broker failover</legend>
            <p className="text-xs text-muted-foreground">{parsed?.brokerFailover?.primary ?? '—'} → {parsed?.brokerFailover?.fallback ?? '—'}</p>
          </fieldset>
          <fieldset className="set-fieldset">
            <legend>Risk threshold pointers (read-only)</legend>
            <p className="text-xs text-muted-foreground">Max drawdown {parsed?.riskThresholdRefs?.maxDrawdownPct ?? '—'}% · WS latency {parsed?.riskThresholdRefs?.wsLatencyMs ?? '—'}ms — set via the Risk Limits section, not here.</p>
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

/* ------------------------------- Section 3 -------------------------------- */

function BrokerCredentialCard({ status, canEdit, onChanged }: { status: BrokerCredentialStatus; canEdit: boolean; onChanged: () => void }) {
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    setBusy(true)
    setError(null)
    try {
      await writeBrokerCredentials(status.broker, apiKey, apiSecret || undefined, accessToken || undefined)
      setApiKey(''); setApiSecret(''); setAccessToken('')
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

  return (
    <div className="set-cred-card">
      <div className="set-cred-head"><strong>{status.broker}</strong><span className={`set-conn set-conn-${status.configured ? 'connected' : 'not-connected'}`}>{status.configured ? <CheckCircle2 className="size-3" /> : <Circle className="size-3" />}{status.configured ? 'Configured' : 'Not configured'}</span></div>
      {error && <p className="text-xs text-rose-300">{error}</p>}
      {canEdit && <>
        <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder="API key" value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>
        <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder="API secret (optional)" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} /></div>
        <div className="set-cred-field"><Lock className="size-3.5" /><input type="password" placeholder="Access token (optional)" value={accessToken} onChange={(e) => setAccessToken(e.target.value)} /></div>
        <div className="set-cred-actions">
          <button className="set-btn set-btn-ghost" disabled={!apiKey || busy} onClick={handleSave}>Save (write-only)</button>
          {status.configured && <button className="set-btn set-btn-secondary" disabled={busy} onClick={handleDelete}>Remove</button>}
        </div>
      </>}
    </div>
  )
}

function CredentialsSection() {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [statuses, setStatuses] = useState<BrokerCredentialStatus[]>([])

  async function reload() { setStatuses(await listBrokerCredentialStatus()) }
  useEffect(() => { reload() }, [])

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Broker Credentials</h2><p>Secrets are write-only. Saved values are never displayed &mdash; only a configured/not-configured status.</p></div></div>
      <p className="set-group-label">Brokers</p>
      <div className="set-cred-grid">{statuses.length > 0 ? statuses.map((c) => <BrokerCredentialCard key={c.broker} status={c} canEdit={canEdit} onChanged={reload} />) : KNOWN_BROKERS.map((b) => <BrokerCredentialCard key={b} status={{ broker: b, configured: false }} canEdit={canEdit} onChanged={reload} />)}</div>
      <p className="set-group-label">LLM Providers</p>
      <p className="p-4 text-xs text-muted-foreground">LLM provider API keys are configured via deployment environment variables, not through this console — there is no runtime credential-store endpoint for them (unlike brokers).</p>
    </div>
  )
}

/* ------------------------------- Section 4 -------------------------------- */

const ALERT_LABELS: Record<AlertLevel, string> = { 'kill-switch': 'Kill-switch trips', 'sign-off': 'Sign-off items', 'go-live': 'Go-live gate passes', daily: 'Daily summary' }

function ChannelCard({ status, canEdit, onChanged }: { status: NotificationChannelStatus; canEdit: boolean; onChanged: () => void }) {
  const [botToken, setBotToken] = useState('')
  const [chatId, setChatId] = useState('')
  const [webhookUrl, setWebhookUrl] = useState('')
  const [prefs, setPrefs] = useState<Record<string, boolean>>(() => Object.fromEntries(ALERT_LEVELS.map((l) => [l, status.alert_levels.includes(l)])))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave(enabled: boolean) {
    setBusy(true)
    setError(null)
    try {
      await writeNotificationChannel(status.channel, {
        enabled,
        bot_token: botToken || undefined,
        chat_id: chatId || undefined,
        webhook_url: webhookUrl || undefined,
        alert_levels: ALERT_LEVELS.filter((l) => prefs[l]),
      })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function handleDisconnect() {
    setBusy(true)
    setError(null)
    try {
      await deleteNotificationChannel(status.channel)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Disconnect failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`set-channel ${status.configured && status.enabled ? 'connected' : ''}`}>
      <div className="set-channel-head"><div><strong>{status.channel}</strong><small>{status.configured ? 'configured' : 'not configured'}</small></div>{canEdit && (status.configured ? <button className="set-btn set-btn-danger" disabled={busy} onClick={handleDisconnect}><Plug className="size-3.5" /> Disconnect</button> : <button className="set-btn set-btn-primary" disabled={busy || (!botToken && !webhookUrl)} onClick={() => handleSave(true)}><Plug className="size-3.5" /> Connect</button>)}</div>
      {error && <p className="px-4 text-xs text-rose-300">{error}</p>}
      {canEdit && !status.configured && <div className="px-4 pb-3 flex flex-col gap-2">
        {status.channel === 'telegram' && <><input placeholder="Bot token" value={botToken} onChange={(e) => setBotToken(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /><input placeholder="Chat ID" value={chatId} onChange={(e) => setChatId(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /></>}
        {(status.channel === 'discord' || status.channel === 'slack') && <input placeholder="Webhook URL" value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" />}
      </div>}
      <fieldset className="set-alert-prefs" disabled={!status.configured || !canEdit}>
        <legend>Alert levels</legend>
        {ALERT_LEVELS.map((l) => <label key={l} className="set-alert-pref"><button type="button" className={`set-check ${prefs[l] ? 'on' : ''}`} onClick={() => { const next = { ...prefs, [l]: !prefs[l] }; setPrefs(next); if (status.configured) handleSave(status.enabled) }} aria-label={`${prefs[l] ? 'Disable' : 'Enable'} ${ALERT_LABELS[l]} on ${status.channel}`}>{prefs[l] && <Check className="size-3.5" />}</button><span>{ALERT_LABELS[l]}</span></label>)}
      </fieldset>
    </div>
  )
}

function NotificationsSection() {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [statuses, setStatuses] = useState<NotificationChannelStatus[]>([])

  async function reload() { setStatuses(await listNotificationChannels()) }
  useEffect(() => { reload() }, [])

  const byChannel = (name: string) => statuses.find((s) => s.channel === name) ?? { channel: name, configured: false, enabled: false, allowed_sender_ids: [], alert_levels: [] }

  return (
    <div className="set-block">
      <div className="set-block-head"><div><h2>Notification Channels</h2><p>Connect messaging channels and choose which real alert levels each one receives.</p></div></div>
      <div className="set-channel-grid">{NOTIFICATION_CHANNELS.map((c) => <ChannelCard key={c} status={byChannel(c)} canEdit={canEdit} onChanged={reload} />)}</div>
    </div>
  )
}

/* ------------------------------- Section 5 -------------------------------- */

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
                  <span className="set-subnav-text"><strong>{s.label}</strong><small>{s.desc}</small></span>
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
