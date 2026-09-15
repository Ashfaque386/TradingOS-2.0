'use client'

import { useMemo, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Download,
  FileJson,
  Lock,
  Search,
  ShieldCheck,
  ShieldOff,
} from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth, roleLabel, ROLES, type MockRole } from '@/components/auth/auth-provider'

type ActionType =
  | 'Order Placed'
  | 'Risk Limit Changed'
  | 'Agent Disabled'
  | 'Config Updated'
  | 'Strategy Approved'
  | 'Agent Enabled'
  | 'Sign-off Approved'
  | 'Sign-off Rejected'

type EntityType = 'Order' | 'Risk Limit' | 'Agent' | 'System Config' | 'Strategy' | 'Live Intent'

interface AuditEntry {
  id: string
  timestamp: string
  actor: string
  action: ActionType
  entityType: EntityType
  entityId: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  verified: boolean
}

const ACTORS = [
  'S. Rao (PM)', 'A. Mehta (Risk)', 'RiskGuardian Agent', 'ExecutionAgent', 'N. Iyer (Admin)',
  'StrategyOps Agent', 'K. Sharma (PM)', 'ComplianceBot', 'System Scheduler',
]

const ACTIONS: ActionType[] = [
  'Order Placed', 'Risk Limit Changed', 'Agent Disabled', 'Config Updated', 'Strategy Approved',
  'Agent Enabled', 'Sign-off Approved', 'Sign-off Rejected',
]

const ENTITY_TYPES: EntityType[] = ['Order', 'Risk Limit', 'Agent', 'System Config', 'Strategy', 'Live Intent']

function pad(n: number) {
  return n.toString().padStart(2, '0')
}

function buildEntries(): AuditEntry[] {
  const entries: AuditEntry[] = []
  const symbols = ['NIFTY24DECFUT', 'BANKNIFTY24DECFUT', 'RELIANCE', 'HDFCBANK', 'TCS', 'INFY']
  const strategies = ['NIFTY-Weekly-IronCondor', 'BankNifty-Momentum-Scalper', 'Delta-Neutral-Straddle', 'Sector-Rotation-Alpha']
  const agents = ['RiskGuardian', 'ExecutionAgent-02', 'SignalScout', 'StrategyOps', 'ComplianceBot']

  let hour = 9
  let minute = 18
  for (let i = 0; i < 37; i++) {
    minute += 7 + (i % 5)
    if (minute >= 60) {
      minute -= 60
      hour += 1
    }
    const action = ACTIONS[i % ACTIONS.length]
    const actor = ACTORS[i % ACTORS.length]
    let entityType: EntityType = 'Order'
    let entityId = ''
    let before: Record<string, unknown> | null = null
    let after: Record<string, unknown> | null = null

    switch (action) {
      case 'Order Placed':
        entityType = 'Order'
        entityId = `ORD-${8800 + i}`
        before = null
        after = { symbol: symbols[i % symbols.length], side: i % 2 === 0 ? 'BUY' : 'SELL', qty: 25 * ((i % 4) + 1), mode: i % 3 === 0 ? 'LIVE' : 'PAPER' }
        break
      case 'Risk Limit Changed':
        entityType = 'Risk Limit'
        entityId = `RISK-MAX-DD-${(i % 3) + 1}`
        before = { maxDrawdownPct: 12, maxPositionSize: 500000 }
        after = { maxDrawdownPct: 10, maxPositionSize: 450000 }
        break
      case 'Agent Disabled':
        entityType = 'Agent'
        entityId = agents[i % agents.length]
        before = { status: 'active' }
        after = { status: 'disabled', reason: 'anomalous latency detected' }
        break
      case 'Agent Enabled':
        entityType = 'Agent'
        entityId = agents[(i + 2) % agents.length]
        before = { status: 'disabled' }
        after = { status: 'active' }
        break
      case 'Config Updated':
        entityType = 'System Config'
        entityId = 'gateway-latency-budget'
        before = { budgetMs: 60 }
        after = { budgetMs: 50 }
        break
      case 'Strategy Approved':
        entityType = 'Strategy'
        entityId = strategies[i % strategies.length]
        before = { stage: 'Review' }
        after = { stage: 'Approved', approvedBy: actor }
        break
      case 'Sign-off Approved':
        entityType = 'Live Intent'
        entityId = `INTENT-${4400 + i}`
        before = { status: 'pending' }
        after = { status: 'approved', approvedBy: actor }
        break
      case 'Sign-off Rejected':
        entityType = 'Live Intent'
        entityId = `INTENT-${4400 + i}`
        before = { status: 'pending' }
        after = { status: 'rejected', reason: 'exceeds notional threshold' }
        break
    }

    entries.push({
      id: `AUD-${100000 + i}`,
      timestamp: `2025-06-11 ${pad(hour)}:${pad(minute)}:${pad((i * 11) % 60)}`,
      actor,
      action,
      entityType,
      entityId,
      before,
      after,
      verified: i !== 17,
    })
  }
  return entries.reverse()
}

const ENTRIES = buildEntries()

function DiffBlock({ before, after }: { before: Record<string, unknown> | null; after: Record<string, unknown> | null }) {
  const keys = Array.from(new Set([...(before ? Object.keys(before) : []), ...(after ? Object.keys(after) : [])]))
  return (
    <div className="diff-grid">
      <div className="diff-col">
        <p className="diff-col-label">Before</p>
        <pre className="diff-pre diff-pre-before">
          {before === null ? '// no prior state (new record)' : JSON.stringify(before, null, 2)}
        </pre>
      </div>
      <div className="diff-col">
        <p className="diff-col-label">After</p>
        <pre className="diff-pre diff-pre-after">
          {keys.map((k) => {
            const changed = before ? JSON.stringify(before[k]) !== JSON.stringify(after?.[k]) : true
            return (
              <span key={k} className={changed ? 'diff-line-changed' : 'diff-line'}>
                {`  "${k}": ${JSON.stringify(after?.[k])}`}
                {'\n'}
              </span>
            )
          })}
        </pre>
      </div>
    </div>
  )
}

function AccessRestricted({ role }: { role: MockRole }) {
  return (
    <ShellLayout>
      <main className="audit-page mx-auto w-full max-w-[1000px] pb-10">
        <div className="restricted-state">
          <div className="restricted-icon">
            <Lock className="size-6" />
          </div>
          <p className="eyebrow">TRADINGOS // AUDIT LOG</p>
          <h1>Access restricted</h1>
          <p className="restricted-copy">
            The Audit Log is limited to roles with independent oversight responsibility. Your current
            mock role, <strong>{roleLabel(role)}</strong>, does not include audit visibility.
          </p>
          <div className="restricted-roles">
            <span>Visible to:</span>
            <span className="restricted-role-chip"><ShieldCheck className="size-3.5" /> Read-Only Auditor</span>
            <span className="restricted-role-chip"><ShieldCheck className="size-3.5" /> System Administrator</span>
          </div>
          <p className="restricted-hint">Switch your mock role from the account menu in the top bar to preview this page.</p>
        </div>
      </main>
    </ShellLayout>
  )
}

const PAGE_SIZE = 10

export default function AuditLogPage() {
  const { role } = useAuth()
  const canView = role === 'ReadOnlyAuditor' || role === 'SystemAdministrator'

  const [search, setSearch] = useState('')
  const [actorFilter, setActorFilter] = useState('all')
  const [entityFilter, setEntityFilter] = useState<'all' | EntityType>('all')
  const [actionFilter, setActionFilter] = useState<'all' | ActionType>('all')
  const [dateFrom, setDateFrom] = useState('2025-06-11')
  const [dateTo, setDateTo] = useState('2025-06-11')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [page, setPage] = useState(1)

  const filtered = useMemo(() => {
    return ENTRIES.filter((e) => {
      if (actorFilter !== 'all' && e.actor !== actorFilter) return false
      if (entityFilter !== 'all' && e.entityType !== entityFilter) return false
      if (actionFilter !== 'all' && e.action !== actionFilter) return false
      const day = e.timestamp.slice(0, 10)
      if (day < dateFrom || day > dateTo) return false
      if (search && !`${e.entityId} ${e.actor} ${e.action}`.toLowerCase().includes(search.toLowerCase())) return false
      return true
    })
  }, [actorFilter, entityFilter, actionFilter, dateFrom, dateTo, search])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  if (!canView) return <AccessRestricted role={role} />

  return (
    <ShellLayout>
      <main className="audit-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="audit-header">
          <div>
            <p className="eyebrow">TRADINGOS // COMPLIANCE</p>
            <h1>Audit Log</h1>
            <p>Immutable, hash-chained record of every human and agent action across the platform.</p>
          </div>
          <div className="audit-export-group">
            <button className="audit-export-btn" onClick={() => {}}>
              <Download className="size-3.5" /> Export CSV
            </button>
            <button className="audit-export-btn" onClick={() => {}}>
              <FileJson className="size-3.5" /> Export NDJSON
            </button>
          </div>
        </header>

        <div className="chain-integrity-strip">
          <ShieldCheck className="size-4" />
          <span>Hash chain verified &mdash; no divergence detected</span>
          <span className="chain-integrity-sep">&middot;</span>
          <span className="mono">Last verification run: 2025-06-11 11:42:07 IST</span>
          <span className="chain-integrity-badge">{ENTRIES.length} entries in chain</span>
        </div>

        <div className="audit-filters">
          <label className="audit-search-box">
            <Search className="size-3.5" />
            <input placeholder="Search entity id, actor, action..." value={search} onChange={(e) => { setSearch(e.target.value); setPage(1) }} />
          </label>
          <label className="audit-filter-field">
            <span>From</span>
            <input type="date" value={dateFrom} onChange={(e) => { setDateFrom(e.target.value); setPage(1) }} />
          </label>
          <label className="audit-filter-field">
            <span>To</span>
            <input type="date" value={dateTo} onChange={(e) => { setDateTo(e.target.value); setPage(1) }} />
          </label>
          <label className="audit-filter-field">
            <span>Actor</span>
            <select value={actorFilter} onChange={(e) => { setActorFilter(e.target.value); setPage(1) }}>
              <option value="all">All actors</option>
              {ACTORS.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
          <label className="audit-filter-field">
            <span>Entity type</span>
            <select value={entityFilter} onChange={(e) => { setEntityFilter(e.target.value as any); setPage(1) }}>
              <option value="all">All entities</option>
              {ENTITY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className="audit-filter-field">
            <span>Action</span>
            <select value={actionFilter} onChange={(e) => { setActionFilter(e.target.value as any); setPage(1) }}>
              <option value="all">All actions</option>
              {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
        </div>

        <section className="audit-panel">
          <div className="overflow-x-auto">
            <table className="audit-table">
              <thead>
                <tr>
                  <th></th>
                  <th>Timestamp</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Entity type</th>
                  <th>Entity id</th>
                  <th>Chain</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((e) => {
                  const isOpen = expanded === e.id
                  return (
                    <>
                      <tr key={e.id} className={isOpen ? 'audit-row-open' : ''} onClick={() => setExpanded(isOpen ? null : e.id)}>
                        <td className="audit-expand-cell">
                          {isOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                        </td>
                        <td className="mono">{e.timestamp}</td>
                        <td>{e.actor}</td>
                        <td><span className="audit-action-chip">{e.action}</span></td>
                        <td>{e.entityType}</td>
                        <td className="mono">{e.entityId}</td>
                        <td>
                          {e.verified ? (
                            <span className="chain-ok"><ShieldCheck className="size-3.5" /> Verified</span>
                          ) : (
                            <span className="chain-warn"><ShieldOff className="size-3.5" /> Recheck</span>
                          )}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="audit-diff-row" key={`${e.id}-diff`}>
                          <td colSpan={7}>
                            <DiffBlock before={e.before} after={e.after} />
                          </td>
                        </tr>
                      )}
                    </>
                  )
                })}
                {pageRows.length === 0 && (
                  <tr><td colSpan={7} className="audit-empty">No audit entries match these filters.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="audit-pagination">
            <span>Showing {pageRows.length ? (page - 1) * PAGE_SIZE + 1 : 0}&ndash;{(page - 1) * PAGE_SIZE + pageRows.length} of {filtered.length}</span>
            <div className="audit-pagination-btns">
              <button disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
              <span>{page} / {totalPages}</span>
              <button disabled={page === totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
            </div>
          </div>
        </section>
      </main>
    </ShellLayout>
  )
}
