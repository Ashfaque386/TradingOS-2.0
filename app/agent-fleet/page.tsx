'use client'

import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Bot, LockKeyhole, Pause, Plus, RotateCcw, Save, Search, ShieldCheck, SlidersHorizontal, X, Zap } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth } from '@/components/auth/auth-provider'
import { type AgentSummary, type PromptVersion, ApiError, activatePromptVersion, createPromptVersion, getAgents, getGatewayConfig, listPromptVersions, putAgentIdentity, putGatewayConfig } from '@/lib/api'

type AgentStatus = 'active' | 'degraded' | 'disabled' | 'executing'
type Department = 'Executive' | 'Market Intelligence' | 'Research' | 'Quant' | 'Risk & Governance' | 'Portfolio' | 'Operations'

const departments: { name: Department; color: string; short: string }[] = [
  { name: 'Executive', color: '#c084fc', short: 'EXE' }, { name: 'Market Intelligence', color: '#38bdf8', short: 'MKT' }, { name: 'Research', color: '#818cf8', short: 'RES' }, { name: 'Quant', color: '#22d3ee', short: 'QNT' }, { name: 'Risk & Governance', color: '#fb7185', short: 'RSK' }, { name: 'Portfolio', color: '#34d399', short: 'PFM' }, { name: 'Operations', color: '#fbbf24', short: 'OPS' },
]

const tabs = ['Identity', 'Prompt Versions', 'Skills', 'Heartbeat', 'Enable / Disable']

function statusOf(agent: AgentSummary): AgentStatus {
  return agent.enabled ? 'active' : 'disabled'
}

function StatusDot({ status }: { status: AgentStatus }) { return <span className={`agent-status-dot status-${status}`} title={status} /> }
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
  const dept = departments.find((d) => d.name === agent.department) ?? departments[0]

  useEffect(() => setHeartbeat(agent.heartbeat_enabled), [agent.heartbeat_enabled])
  useEffect(() => setIdentityForm({ name: agent.identity_name, emoji: agent.emoji ?? '', avatar: agent.avatar ?? '', theme: agent.theme ?? '', voice: agent.voice ?? '' }), [agent.agent_id, agent.identity_name, agent.emoji, agent.avatar, agent.theme, agent.voice])

  async function loadPromptVersions() {
    setPromptVersions(await listPromptVersions(agent.agent_id))
  }

  useEffect(() => {
    if (tab === 'Prompt Versions') loadPromptVersions()
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
        emoji: identityForm.emoji.trim() || undefined,
        avatar: identityForm.avatar.trim() || undefined,
        theme: identityForm.theme.trim() || undefined,
        voice: identityForm.voice.trim() || undefined,
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
    setBusy(true)
    setError(null)
    try {
      await activatePromptVersion(agent.agent_id, versionId)
      await loadPromptVersions()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to activate version')
    } finally {
      setBusy(false)
    }
  }

  return <aside className="fleet-workspace"><div className="workspace-header"><div><p className="eyebrow">AGENT WORKSPACE</p><h2>{agent.display_name}</h2><p className="workspace-sub"><StatusDot status={statusOf(agent)} /> {agent.department}</p></div><button className="icon-button" onClick={onClose} aria-label="Close workspace"><X /></button></div><div className="workspace-tabs">{tabs.map((item) => <button key={item} className={tab === item ? 'workspace-tab-active' : ''} onClick={() => setTab(item)}>{item}</button>)}</div><div className="workspace-body">
    {error && <div className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}
    {tab === 'Identity' && <div className="tab-stack"><div className="agent-preview" style={{ '--dept-color': dept.color } as React.CSSProperties}><span className="preview-orb"><Bot className="size-5" /></span><div><p className="eyebrow">AGENT ID</p><strong>{agent.display_name}</strong><small>{agent.agent_id} · {agent.department}</small></div></div><div className="section-label"><span>CAPABILITIES</span></div><div className="skill-grid">{agent.capabilities.map((c) => <span className="skill-chip" key={c}>{c}</span>)}</div><div className="section-label"><span>IDENTITY</span></div><label>Name<input disabled={!canEdit || busy} value={identityForm.name} onChange={(e) => setIdentityForm({ ...identityForm, name: e.target.value })} /></label><label>Emoji<input disabled={!canEdit || busy} value={identityForm.emoji} onChange={(e) => setIdentityForm({ ...identityForm, emoji: e.target.value })} placeholder="e.g. 👑" /></label><label>Avatar URL<input disabled={!canEdit || busy} value={identityForm.avatar} onChange={(e) => setIdentityForm({ ...identityForm, avatar: e.target.value })} placeholder="https://..." /></label><label>Theme<input disabled={!canEdit || busy} value={identityForm.theme} onChange={(e) => setIdentityForm({ ...identityForm, theme: e.target.value })} placeholder="e.g. crimson" /></label><label>Voice<input disabled={!canEdit || busy} value={identityForm.voice} onChange={(e) => setIdentityForm({ ...identityForm, voice: e.target.value })} placeholder="e.g. formal" /></label>{canEdit ? <button disabled={busy} onClick={saveIdentity} className="primary-action"><Save className="size-3.5" /> Save identity</button> : <p className="muted-copy">Identity fields are editable by a System Administrator only.</p>}</div>}
    {tab === 'Prompt Versions' && <div className="tab-stack">{canEdit && <><label>New draft<textarea disabled={busy} value={newPromptContent} onChange={(e) => setNewPromptContent(e.target.value)} placeholder="System prompt content..." /></label><button disabled={busy || !newPromptContent.trim()} onClick={submitDraft} className="text-button"><Plus /> Create draft</button></>}<div className="section-label"><span>VERSION HISTORY · {promptVersions.length}</span></div>{promptVersions.length === 0 && <p className="muted-copy">No prompt versions yet.</p>}{promptVersions.map((v) => <div key={v.id} className={`version-row ${v.status === 'active' ? 'version-active' : ''}`}><div><strong>v{v.version_number} · {v.status}</strong><small>{v.created_by ?? 'unknown'} · {new Date(v.created_at).toLocaleString()}</small></div>{v.status !== 'active' && canEdit && <button className="outline-action" disabled={busy} onClick={() => activate(v.id)}>Activate</button>}</div>)}{promptVersions.find((v) => v.status === 'active')?.diff_from_previous && <div className="diff-box"><p className="section-label"><span>DIFF FROM PREVIOUS ACTIVE VERSION</span></p><code>{promptVersions.find((v) => v.status === 'active')?.diff_from_previous}</code></div>}</div>}
    {tab === 'Skills' && <div className="tab-stack"><div className="section-label"><span>GRANTED SKILLS · {agent.skills.length}</span></div><div className="skill-grid">{agent.skills.map((skill) => <span className="skill-chip" key={skill}>{skill}{canEdit && <button disabled={busy} onClick={() => run({ skills: agent.skills.filter((s) => s !== skill) })}><X /></button>}</span>)}</div>{canEdit && <div className="mt-3 flex gap-2"><input value={newSkill} onChange={(e) => setNewSkill(e.target.value)} placeholder="skill-id" className="min-w-0 flex-1 rounded border border-white/10 bg-black/20 px-2 py-1 text-xs" /><button disabled={busy || !newSkill.trim()} onClick={() => { run({ skills: [...agent.skills, newSkill.trim()] }); setNewSkill('') }} className="text-button"><Plus /> Add skill</button></div>}<div className="callout"><LockKeyhole /><span>Skills are governed by role policy — every change writes through the Agent Gateway config and is captured in the audit trail.</span></div></div>}
    {tab === 'Heartbeat' && <div className="tab-stack"><div className="setting-row"><div><strong>Heartbeat observer</strong><small>Periodic health and signal check</small></div><button disabled={!canEdit || busy} className={`toggle ${heartbeat ? 'toggle-on' : ''}`} onClick={() => { const next = !heartbeat; setHeartbeat(next); run({ heartbeatEnabled: next }) }} aria-label="Toggle heartbeat"><span /></button></div><div className="callout callout-cyan"><Zap /><span>Heartbeat can only observe and raise alerts — it cannot place, modify, or cancel any order.</span></div></div>}
    {tab === 'Enable / Disable' && <div className="tab-stack"><div className="danger-banner"><AlertTriangle /><div><strong>{agent.enabled ? 'Agent is enabled' : 'Agent is disabled'}</strong><small>Changes are recorded in the immutable audit trail.</small></div></div><div className="setting-row"><div><strong>Operational status</strong><small>{!agent.can_disable ? 'Protected system agent' : 'Allow this agent to run tasks'}</small></div><button disabled={!agent.can_disable || !canEdit || busy} className={`toggle ${agent.enabled ? 'toggle-on' : ''}`} onClick={() => run({ enabled: !agent.enabled })} aria-label="Toggle agent status"><span /></button></div>{!agent.can_disable && <div className="callout"><LockKeyhole /><span>Cannot be disabled. {agent.display_name} is required for governance and safety of the trading system.</span></div>}</div>}
  </div></aside>
}

export default function AgentFleetPage() {
  const { role } = useAuth()
  const canEdit = role === 'SystemAdministrator'
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [zoom, setZoom] = useState(1)

  async function reload() {
    const data = await getAgents()
    setAgents(data)
  }

  useEffect(() => {
    reload()
    const interval = setInterval(reload, 15000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (agents.length > 0 && selectedId === null) setSelectedId(agents[0].agent_id)
  }, [agents, selectedId])

  const filtered = useMemo(() => agents.filter((agent) => agent.display_name.toLowerCase().includes(query.toLowerCase())), [agents, query])
  const selected = agents.find((a) => a.agent_id === selectedId) ?? null
  const ceo = agents.find((a) => a.agent_id === 'ceo-agent') ?? agents[0]

  return <ShellLayout><div className="mx-auto flex w-full max-w-[1800px] flex-col gap-5"><header className="fleet-page-header"><div><p className="eyebrow">TRADINGOS // AGENT FLEET</p><h1>Organization intelligence map</h1><p>{agents.length || 24} specialized agents · one governed execution layer</p></div><div className="fleet-header-actions"><div className="search-box"><Search /><input placeholder="Find an agent" value={query} onChange={(e) => setQuery(e.target.value)} /></div><button className="outline-action"><SlidersHorizontal /> Filter</button></div></header><div className="fleet-layout"><section className="fleet-canvas"><div className="canvas-toolbar"><span className="live-pill"><span className="status-dot" /> SYSTEM ONLINE</span><div><button onClick={() => setZoom(Math.max(.75, zoom - .1))}>−</button><span>{Math.round(zoom * 100)}%</span><button onClick={() => setZoom(Math.min(1.25, zoom + .1))}>+</button><button onClick={() => setZoom(1)}><RotateCcw /></button></div></div><div className="tree-viewport"><div className="tree-content" style={{ transform: `scale(${zoom})` }}>{ceo && <div className="tree-root"><Node agent={ceo} selected={selectedId === ceo.agent_id} onClick={() => setSelectedId(ceo.agent_id)} /></div>}<div className="tree-connector vertical" /><div className="department-row">{departments.map((department) => <div className="department-column" key={department.name}><div className="connector-line" style={{ background: department.color }} /><div className="department-label" style={{ '--dept-color': department.color } as React.CSSProperties}><span>{department.short}</span>{department.name}</div><div className="agent-stack">{filtered.filter((a) => a.department === department.name && a.agent_id !== ceo?.agent_id).map((agent) => <Node key={agent.agent_id} agent={agent} selected={selectedId === agent.agent_id} onClick={() => setSelectedId(agent.agent_id)} />)}</div></div>)}</div></div></div><div className="tree-legend"><span><StatusDot status="active" /> Active / idle</span><span><StatusDot status="executing" /> Executing</span><span><StatusDot status="degraded" /> Degraded</span><span><StatusDot status="disabled" /> Disabled</span></div></section>{selected && <AgentWorkspace key={selected.agent_id} agent={selected} onClose={() => setSelectedId(null)} canEdit={canEdit} onChanged={reload} />}</div><div className="fleet-footer"><span><Bot /> {agents.length} agents registered</span><span><ShieldCheck /> Governance policy enforced</span><span><Pause /> Paper environment only</span></div></div></ShellLayout>
}
