'use client'

import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, Bot, LockKeyhole, Pause, Plus, RotateCcw, Save, Search, ShieldCheck, X, Zap } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { type AgentActivity, type AgentSummary, type PromptVersion, ApiError, activatePromptVersion, createPromptVersion, getAgentActivity, getAgents, getGatewayConfig, listPromptVersions, putAgentIdentity, putGatewayConfig } from '@/lib/api'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import { TabList } from '@/components/ui/tab-list'
import { usePolling } from '@/lib/use-polling'
import { can } from '@/lib/permissions'

type AgentStatus = 'active' | 'degraded' | 'disabled' | 'executing'
type Department = 'Executive' | 'Market Intelligence' | 'Research' | 'Fundamental Research' | 'Quant' | 'Risk & Governance' | 'Portfolio' | 'Operations'

// Phase 19 (docs/phase19-audit.md Part 3.2): this list was found to be a
// hardcoded copy of src.gateway.roster.Department that had drifted --
// adding a department there (Fundamental Research, for the Screener/
// Fundamentals/Valuation agents) silently dropped those agents from the
// org chart entirely, since the department-column filter below never
// matched any entry here. Every value must match that enum exactly.
const departments: { name: Department; color: string; short: string }[] = [
  { name: 'Executive', color: '#c084fc', short: 'EXE' }, { name: 'Market Intelligence', color: '#38bdf8', short: 'MKT' }, { name: 'Research', color: '#818cf8', short: 'RES' }, { name: 'Fundamental Research', color: '#f472b6', short: 'FND' }, { name: 'Quant', color: '#22d3ee', short: 'QNT' }, { name: 'Risk & Governance', color: '#fb7185', short: 'RSK' }, { name: 'Portfolio', color: '#34d399', short: 'PFM' }, { name: 'Operations', color: '#fbbf24', short: 'OPS' },
]

const tabs = ['Identity', 'Prompt Versions', 'Skills', 'Heartbeat', 'Activity', 'Enable / Disable']

function statusOf(agent: AgentSummary): AgentStatus {
  return agent.enabled ? 'active' : 'disabled'
}

function StatusDot({ status }: { status: AgentStatus }) { return <span className={`agent-status-dot status-${status}`} role="img" aria-label={`Status: ${status}`} title={status} /> }
function Node({ agent, onClick, selected }: { agent: AgentSummary; onClick: () => void; selected: boolean }) { return <button onClick={onClick} className={`fleet-node ${selected ? 'fleet-node-selected' : ''}`} style={{ '--dept-color': departments.find((d) => d.name === agent.department)?.color } as React.CSSProperties}><span className="node-icon"><Bot className="size-4" /></span><span className="min-w-0 text-left"><strong>{agent.display_name}</strong><small><StatusDot status={statusOf(agent)} />{statusOf(agent)}</small></span></button> }

type AgentEntry = { enabled?: boolean | null; heartbeatEnabled?: boolean | null; heartbeatIntervalMinutes?: number | null; skills?: string[] | null }

async function patchAgentEntry(agentId: string, patch: Partial<AgentEntry>) {
  const config = await getGatewayConfig()
  const parsed = JSON.parse(JSON.stringify(config.parsed)) as Record<string, unknown>
  const agentsSection = (parsed.agents ?? { entries: {} }) as { entries: Record<string, AgentEntry> }
  const existing = agentsSection.entries[agentId] ?? {}
  agentsSection.entries[agentId] = { ...existing, ...patch }
  parsed.agents = agentsSection
  await putGatewayConfig(JSON.stringify(parsed))
}

function AgentWorkspace({ agent, onClose, canEdit, onChanged }: { agent: AgentSummary; onClose: () => void; canEdit: boolean; onChanged: () => void }) {
  const [tab, setTab] = useState('Identity')
  const [heartbeat, setHeartbeat] = useState(agent.heartbeat_enabled)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [newSkill, setNewSkill] = useState('')
  const [identityForm, setIdentityForm] = useState({ name: agent.identity_name, emoji: agent.emoji ?? '', avatar: agent.avatar ?? '', theme: agent.theme ?? '', voice: agent.voice ?? '' })
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([])
  const [newPromptContent, setNewPromptContent] = useState('')
  const [activating, setActivating] = useState<PromptVersion | null>(null)
  const [activity, setActivity] = useState<AgentActivity | null>(null)
  const [activityError, setActivityError] = useState<string | null>(null)
  const dept = departments.find((d) => d.name === agent.department) ?? departments[0]

  useEffect(() => setHeartbeat(agent.heartbeat_enabled), [agent.heartbeat_enabled])
  useEffect(() => setIdentityForm({ name: agent.identity_name, emoji: agent.emoji ?? '', avatar: agent.avatar ?? '', theme: agent.theme ?? '', voice: agent.voice ?? '' }), [agent.agent_id, agent.identity_name, agent.emoji, agent.avatar, agent.theme, agent.voice])

  async function loadPromptVersions() {
    setPromptVersions(await listPromptVersions(agent.agent_id))
  }

  useEffect(() => {
    if (tab === 'Prompt Versions') loadPromptVersions()
    if (tab === 'Activity') {
      setActivityError(null)
      getAgentActivity(agent.agent_id)
        .then(setActivity)
        .catch((err) => {
          setActivity(null)
          setActivityError(err instanceof Error && err.message ? err.message : 'Could not load the activity')
        })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, agent.agent_id])

  async function run(patch: Partial<AgentEntry>) {
    setBusy(true)
    setError(null)
    try {
      await patchAgentEntry(agent.agent_id, patch)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to apply change')
    } finally {
      setBusy(false)
    }
  }

  async function saveIdentity() {
    setBusy(true)
    setError(null)
    try {
      await putAgentIdentity(agent.agent_id, {
        name: identityForm.name.trim() || undefined,
        // "" clears a field; leaving one out would keep the old value
        emoji: identityForm.emoji.trim(),
        avatar: identityForm.avatar.trim(),
        theme: identityForm.theme.trim(),
        voice: identityForm.voice.trim(),
      })
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save identity')
    } finally {
      setBusy(false)
    }
  }

  async function submitDraft() {
    if (!newPromptContent.trim()) return
    setBusy(true)
    setError(null)
    try {
      await createPromptVersion(agent.agent_id, newPromptContent)
      setNewPromptContent('')
      await loadPromptVersions()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create draft')
    } finally {
      setBusy(false)
    }
  }

  async function activate(versionId: string) {
    await activatePromptVersion(agent.agent_id, versionId)
    await loadPromptVersions()
  }

  return <aside className="fleet-workspace"><div className="workspace-header"><div><p className="eyebrow">AGENT WORKSPACE</p><h2>{agent.display_name}</h2><p className="workspace-sub"><StatusDot status={statusOf(agent)} /> {agent.department}</p></div><button className="icon-button" onClick={onClose} aria-label="Close workspace"><X /></button></div><TabList label="Agent workspace" className="workspace-tabs">{tabs.map((item) => <button key={item} role="tab" aria-selected={tab === item} tabIndex={tab === item ? 0 : -1} className={tab === item ? 'workspace-tab-active' : ''} onClick={() => setTab(item)}>{item}</button>)}</TabList><div className="workspace-body">
    {error && <div className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}
    {tab === 'Identity' && <div className="tab-stack"><div className="agent-preview" style={{ '--dept-color': dept.color } as React.CSSProperties}><span className="preview-orb"><Bot className="size-5" /></span><div><p className="eyebrow">AGENT ID</p><strong>{agent.display_name}</strong><small>{agent.agent_id} · {agent.department}</small></div></div>{!agent.has_data_source && <div className="callout callout-cyan"><AlertTriangle /><span>Gap: this agent&apos;s subject matter has no real backing data source anywhere in this codebase yet. It never fabricates a figure -- see docs/phase19-audit.md.</span></div>}<div className="section-label"><span>CAPABILITIES</span></div><div className="skill-grid">{agent.capabilities.map((c) => <span className="skill-chip" key={c}>{c}</span>)}</div><div className="section-label"><span>IDENTITY</span></div><label>Name<input disabled={!canEdit || busy} value={identityForm.name} onChange={(e) => setIdentityForm({ ...identityForm, name: e.target.value })} /></label><label>Emoji<input disabled={!canEdit || busy} value={identityForm.emoji} onChange={(e) => setIdentityForm({ ...identityForm, emoji: e.target.value })} placeholder="e.g. 👑" /></label><label>Avatar URL<input disabled={!canEdit || busy} value={identityForm.avatar} onChange={(e) => setIdentityForm({ ...identityForm, avatar: e.target.value })} placeholder="https://..." /></label><label>Theme<input disabled={!canEdit || busy} value={identityForm.theme} onChange={(e) => setIdentityForm({ ...identityForm, theme: e.target.value })} placeholder="e.g. crimson" /></label><label>Voice<input disabled={!canEdit || busy} value={identityForm.voice} onChange={(e) => setIdentityForm({ ...identityForm, voice: e.target.value })} placeholder="e.g. formal" /></label>{canEdit ? <button disabled={busy} onClick={saveIdentity} className="primary-action"><Save className="size-3.5" /> Save identity</button> : <p className="muted-copy">Identity fields are editable by a System Administrator only.</p>}</div>}
    {tab === 'Prompt Versions' && <div className="tab-stack">{canEdit && <><label>New draft<textarea disabled={busy} value={newPromptContent} onChange={(e) => setNewPromptContent(e.target.value)} placeholder="System prompt content..." /></label><button disabled={busy || !newPromptContent.trim()} onClick={submitDraft} className="text-button"><Plus /> Create draft</button></>}<div className="section-label"><span>VERSION HISTORY · {promptVersions.length}</span></div>{promptVersions.length === 0 && <p className="muted-copy">No prompt versions yet.</p>}{promptVersions.map((v) => <div key={v.id} className={`version-row ${v.status === 'active' ? 'version-active' : ''}`}><div><strong>v{v.version_number} · {v.status}</strong><small>{v.created_by ?? 'unknown'} · {new Date(v.created_at).toLocaleString()}</small><details className="mt-1"><summary className="cursor-pointer text-[11px] text-muted-foreground">Show the prompt</summary><pre className="prompt-text">{v.content}</pre></details></div>{v.status !== 'active' && canEdit && <button className="outline-action" disabled={busy} onClick={() => setActivating(v)}>Review and activate</button>}</div>)}{promptVersions.find((v) => v.status === 'active')?.diff_from_previous && <div className="diff-box"><p className="section-label"><span>DIFF FROM PREVIOUS ACTIVE VERSION</span></p><code>{promptVersions.find((v) => v.status === 'active')?.diff_from_previous}</code></div>}</div>}
    {tab === 'Skills' && <div className="tab-stack"><div className="section-label"><span>GRANTED SKILLS · {agent.skills.length}</span></div><div className="skill-grid">{agent.skills.map((skill) => <span className="skill-chip" key={skill}>{skill}{canEdit && <button disabled={busy} onClick={() => run({ skills: agent.skills.filter((s) => s !== skill) })}><X /></button>}</span>)}</div>{canEdit && <div className="mt-3 flex gap-2"><input value={newSkill} onChange={(e) => setNewSkill(e.target.value)} placeholder="skill-id" className="min-w-0 flex-1 rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /><button disabled={busy || !newSkill.trim()} onClick={() => { run({ skills: [...agent.skills, newSkill.trim()] }); setNewSkill('') }} className="text-button"><Plus /> Add skill</button></div>}<div className="callout"><LockKeyhole /><span>Skills are governed by role policy — every change writes through the Agent Gateway config and is captured in the audit trail.</span></div></div>}
    {tab === 'Heartbeat' && <div className="tab-stack"><div className="setting-row"><div><strong>Heartbeat observer</strong><small>Periodic health and signal check</small></div><button disabled={!canEdit || busy} className={`toggle ${heartbeat ? 'toggle-on' : ''}`} onClick={() => { const next = !heartbeat; setHeartbeat(next); run({ heartbeatEnabled: next }) }} aria-label="Toggle heartbeat"><span /></button></div><div className="callout callout-cyan"><Zap /><span>Heartbeat can only observe and raise alerts — it cannot place, modify, or cancel any order.</span></div></div>}
    {tab === 'Activity' && <div className="tab-stack">
      <div className="section-label"><span>HEARTBEAT SELF-CHECKS · {activity?.heartbeats.length ?? 0}</span></div>
      {activityError && <ErrorNotice message={`Could not load the activity: ${activityError}`} />}
      {!activity && !activityError && <p className="muted-copy">Loading...</p>}
      {activity && activity.heartbeats.length === 0 && <p className="muted-copy">No heartbeat self-checks recorded. {agent.heartbeat_enabled ? 'The scheduler has not run one yet.' : 'Heartbeat is disabled for this agent -- see the Heartbeat tab.'}</p>}
      {activity?.heartbeats.map((h, i) => <div key={i} className="version-row"><div><strong className={h.status === 'alert' ? 'text-rose-300' : ''}>{h.status}</strong><small>{new Date(h.checked_at).toLocaleString()}</small></div></div>)}
      <div className="section-label"><span>CAPABILITY TASKS · {activity?.tasks.length ?? 0}</span></div>
      {activity && activity.tasks.length === 0 && <p className="muted-copy">No orchestration tasks have claimed this agent&apos;s capabilities yet.</p>}
      {activity?.tasks.map((t) => <div key={t.id} className={`version-row ${t.status === 'failed' ? 'version-active' : ''}`}><div><strong>{t.name}</strong><small>{t.status}{t.last_error ? ` · ${t.last_error}` : ''} · {new Date(t.created_at).toLocaleString()}</small></div></div>)}
      <div className="callout"><Activity /><span>Task has no per-agent column -- these are orchestration tasks whose capability this agent is registered for (src.agents.roster.capabilities_for), the real join key.</span></div>
    </div>}
    {tab === 'Enable / Disable' && <div className="tab-stack"><div className="danger-banner"><AlertTriangle /><div><strong>{agent.enabled ? 'Agent is enabled' : 'Agent is disabled'}</strong><small>Changes are recorded in the immutable audit trail.</small></div></div><div className="setting-row"><div><strong>Operational status</strong><small>{!agent.can_disable ? 'Protected system agent' : 'Allow this agent to run tasks'}</small></div><button disabled={!agent.can_disable || !canEdit || busy} className={`toggle ${agent.enabled ? 'toggle-on' : ''}`} onClick={() => run({ enabled: !agent.enabled })} aria-label="Toggle agent status"><span /></button></div>{!agent.can_disable && <div className="callout"><LockKeyhole /><span>Cannot be disabled. {agent.display_name} is required for governance and safety of the trading system.</span></div>}</div>}
  </div>{activating && (
      <ConfirmDialog
        title={`Activate prompt v${activating.version_number}`}
        description={`${agent.display_name} will use this prompt from its next run, in place of the active one. Read it first.`}
        confirmLabel={`Activate v${activating.version_number}`}
        busyLabel="Activating…"
        onConfirm={async () => activate(activating.id)}
        onClose={() => setActivating(null)}
      >
        <pre className="prompt-text">{activating.content}</pre>
      </ConfirmDialog>
    )}</aside>
}

export default function AgentFleetPage() {
  const { role } = useAuth()
  const canEdit = can(role, 'PUT /api/v1/agents/{agent_id}/identity')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [zoom, setZoom] = useState(1)

  const { data: loadedAgents, error: loadError, refresh: reload } = usePolling(getAgents, { intervalMs: 15_000 })
  const agents: AgentSummary[] = useMemo(() => loadedAgents ?? [], [loadedAgents])

  useEffect(() => {
    if (agents.length > 0 && selectedId === null) setSelectedId(agents[0].agent_id)
  }, [agents, selectedId])

  const filtered = useMemo(() => agents.filter((agent) => agent.display_name.toLowerCase().includes(query.toLowerCase())), [agents, query])
  const selected = agents.find((a) => a.agent_id === selectedId) ?? null
  const ceo = agents.find((a) => a.agent_id === 'ceo-agent') ?? agents[0]

  return <><div className="mx-auto flex w-full max-w-[1800px] flex-col gap-5"><header className="fleet-page-header"><div><p className="eyebrow">TRADINGOS // AGENT FLEET</p><h1>Organization intelligence map</h1><p>{agents.length ? `${agents.length} specialized agents` : 'Specialized agents'} · one governed execution layer</p></div><div className="fleet-header-actions"><div className="search-box"><Search /><input placeholder="Find an agent" value={query} onChange={(e) => setQuery(e.target.value)} /></div></div></header>{loadError && <ErrorNotice message={`Could not load the agents: ${loadError}`} onRetry={() => void reload()} />}<div className="fleet-layout"><section className="fleet-canvas"><div className="canvas-toolbar"><span className="live-pill"><span className="status-dot" /> {agents.length ? 'ROSTER LOADED' : 'NO ROSTER LOADED'}</span><div><button onClick={() => setZoom(Math.max(.75, zoom - .1))}>−</button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom(Math.min(1.25, zoom + .1))}>+</button><button onClick={() => setZoom(1)}><RotateCcw /></button></div></div><div className="tree-viewport"><div className="tree-content" style={{ transform: `scale(${zoom})` }}>{ceo && <div className="tree-root"><Node agent={ceo} selected={selectedId === ceo.agent_id} onClick={() => setSelectedId(ceo.agent_id)} /></div>}<div className="tree-connector vertical" /><div className="department-row">{departments.map((department) => <div className="department-column" key={department.name}><div className="connector-line" style={{ background: department.color }} /><div className="department-label" style={{ '--dept-color': department.color } as React.CSSProperties}><span>{department.short}</span>{department.name}</div><div className="agent-stack">{filtered.filter((a) => a.department === department.name && a.agent_id !== ceo?.agent_id).map((agent) => <Node key={agent.agent_id} agent={agent} selected={selectedId === agent.agent_id} onClick={() => setSelectedId(agent.agent_id)} />)}</div></div>)}</div></div></div><div className="tree-legend"><span><StatusDot status="active" /> Active / idle</span><span><StatusDot status="executing" /> Executing</span><span><StatusDot status="degraded" /> Degraded</span><span><StatusDot status="disabled" /> Disabled</span></div></section>{selected && <AgentWorkspace key={selected.agent_id} agent={selected} onClose={() => setSelectedId(null)} canEdit={canEdit} onChanged={reload} />}</div><div className="fleet-footer"><span><Bot /> {agents.length} agents registered</span><span><ShieldCheck /> Governance policy enforced</span></div></div></>
}
