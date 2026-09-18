'use client'

import { Fragment, useEffect, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight, Download, FileJson, Lock, RefreshCw, Search, ShieldCheck, ShieldOff } from 'lucide-react'
import { ShellLayout } from '@/components/shell/shell-layout'
import { useAuth, roleLabel } from '@/components/auth/auth-provider'
import type { Role } from '@/lib/api'
import { type AuditChainVerifyResult, type AuditLogEntry, ApiError, downloadAuditExport, listAuditEntries, verifyAuditChain } from '@/lib/api'

const PAGE_SIZE = 10

function DetailsBlock({ details, correlationId }: { details: Record<string, unknown> | null; correlationId: string | null }) {
  return (
    <div className="diff-grid">
      <div className="diff-col">
        <p className="diff-col-label">Details</p>
        <pre className="diff-pre diff-pre-after">{details === null ? '// no details recorded' : JSON.stringify(details, null, 2)}</pre>
      </div>
      {correlationId && <div className="diff-col"><p className="diff-col-label">Correlation ID</p><pre className="diff-pre diff-pre-before mono">{correlationId}</pre></div>}
    </div>
  )
}

function AccessRestricted({ role }: { role: Role }) {
  return (
    <ShellLayout>
      <main className="audit-page mx-auto w-full max-w-[1000px] pb-10">
        <div className="restricted-state">
          <div className="restricted-icon"><Lock className="size-6" /></div>
          <p className="eyebrow">TRADINGOS // AUDIT LOG</p>
          <h1>Access restricted</h1>
          <p className="restricted-copy">The Audit Log is limited to roles with independent oversight responsibility. Your current role, <strong>{roleLabel(role)}</strong>, does not include audit visibility.</p>
          <div className="restricted-roles">
            <span>Visible to:</span>
            <span className="restricted-role-chip"><ShieldCheck className="size-3.5" /> Read-Only Auditor</span>
            <span className="restricted-role-chip"><ShieldCheck className="size-3.5" /> System Administrator</span>
          </div>
        </div>
      </main>
    </ShellLayout>
  )
}

export default function AuditLogPage() {
  const { role } = useAuth()
  const canView = role === 'ReadOnlyAuditor' || role === 'SystemAdministrator'

  const [entries, setEntries] = useState<AuditLogEntry[]>([])
  const [search, setSearch] = useState('')
  const [actorFilter, setActorFilter] = useState('all')
  const [entityFilter, setEntityFilter] = useState('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [verifyResult, setVerifyResult] = useState<AuditChainVerifyResult | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [exporting, setExporting] = useState<'csv' | 'ndjson' | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function reload() {
    setEntries(await listAuditEntries({ limit: 200 }))
  }

  useEffect(() => {
    if (!canView) return
    reload()
    const interval = setInterval(reload, 15000)
    return () => clearInterval(interval)
  }, [canView])

  const actors = useMemo(() => Array.from(new Set(entries.map((e) => e.actor))).sort(), [entries])
  const entityTypes = useMemo(() => Array.from(new Set(entries.map((e) => e.entity_type))).sort(), [entries])

  const filtered = useMemo(() => entries.filter((e) => {
    if (actorFilter !== 'all' && e.actor !== actorFilter) return false
    if (entityFilter !== 'all' && e.entity_type !== entityFilter) return false
    if (search && !`${e.entity_id} ${e.actor} ${e.action}`.toLowerCase().includes(search.toLowerCase())) return false
    return true
  }), [entries, actorFilter, entityFilter, search])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  async function handleVerify() {
    setVerifying(true)
    setError(null)
    try {
      setVerifyResult(await verifyAuditChain())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Verification failed')
    } finally {
      setVerifying(false)
    }
  }

  async function handleExport(format: 'csv' | 'ndjson') {
    setExporting(format)
    setError(null)
    try {
      await downloadAuditExport(format)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Export failed')
    } finally {
      setExporting(null)
    }
  }

  if (!canView) return <AccessRestricted role={role} />

  return (
    <ShellLayout>
      <main className="audit-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="audit-header">
          <div><p className="eyebrow">TRADINGOS // COMPLIANCE</p><h1>Audit Log</h1><p>Immutable, hash-chained record of every human and agent action across the platform.</p></div>
          <div className="audit-export-group">
            <button className="audit-export-btn" onClick={() => handleExport('csv')} disabled={exporting !== null}>{exporting === 'csv' ? <RefreshCw className="size-3.5 animate-spin" /> : <Download className="size-3.5" />} Export CSV</button>
            <button className="audit-export-btn" onClick={() => handleExport('ndjson')} disabled={exporting !== null}>{exporting === 'ndjson' ? <RefreshCw className="size-3.5 animate-spin" /> : <FileJson className="size-3.5" />} Export NDJSON</button>
          </div>
        </header>

        {error && <div className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}

        <div className="chain-integrity-strip">
          {verifyResult === null ? <ShieldCheck className="size-4" /> : verifyResult.diverged || !verifyResult.db_chain_valid ? <ShieldOff className="size-4 text-rose-300" /> : <ShieldCheck className="size-4" />}
          <span>{verifyResult === null ? 'Chain not yet verified this session' : verifyResult.diverged || !verifyResult.db_chain_valid ? `Divergence detected: ${verifyResult.reason ?? verifyResult.db_chain_reason ?? 'unknown'}` : 'Hash chain verified — no divergence detected'}</span>
          <span className="chain-integrity-sep">&middot;</span>
          {role === 'SystemAdministrator' && <button className="audit-export-btn" onClick={handleVerify} disabled={verifying}>{verifying ? <RefreshCw className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Run verification</button>}
          <span className="chain-integrity-badge">{entries.length} entries loaded</span>
        </div>

        <div className="audit-filters">
          <label className="audit-search-box"><Search className="size-3.5" /><input placeholder="Search entity id, actor, action..." value={search} onChange={(e) => { setSearch(e.target.value); setPage(1) }} /></label>
          <label className="audit-filter-field"><span>Actor</span><select value={actorFilter} onChange={(e) => { setActorFilter(e.target.value); setPage(1) }}><option value="all">All actors</option>{actors.map((a) => <option key={a} value={a}>{a}</option>)}</select></label>
          <label className="audit-filter-field"><span>Entity type</span><select value={entityFilter} onChange={(e) => { setEntityFilter(e.target.value); setPage(1) }}><option value="all">All entities</option>{entityTypes.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
        </div>

        <section className="audit-panel">
          <div className="overflow-x-auto">
            <table className="audit-table">
              <thead><tr><th></th><th>Timestamp</th><th>Actor</th><th>Action</th><th>Entity type</th><th>Entity id</th><th>Sequence</th></tr></thead>
              <tbody>
                {pageRows.map((e) => {
                  const isOpen = expanded === e.id
                  return (
                    <Fragment key={e.id}>
                      <tr className={isOpen ? 'audit-row-open' : ''} onClick={() => setExpanded(isOpen ? null : e.id)}>
                        <td className="audit-expand-cell">{isOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}</td>
                        <td className="mono">{new Date(e.created_at).toLocaleString()}</td>
                        <td>{e.actor}</td>
                        <td><span className="audit-action-chip">{e.action}</span></td>
                        <td>{e.entity_type}</td>
                        <td className="mono">{e.entity_id}</td>
                        <td className="mono">#{e.sequence}</td>
                      </tr>
                      {isOpen && <tr className="audit-diff-row"><td colSpan={7}><DetailsBlock details={e.details} correlationId={e.correlation_id} /></td></tr>}
                    </Fragment>
                  )
                })}
                {pageRows.length === 0 && <tr><td colSpan={7} className="audit-empty">No audit entries match these filters.</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="audit-pagination">
            <span>Showing {pageRows.length ? (page - 1) * PAGE_SIZE + 1 : 0}&ndash;{(page - 1) * PAGE_SIZE + pageRows.length} of {filtered.length}</span>
            <div className="audit-pagination-btns"><button disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button><span>{page} / {totalPages}</span><button disabled={page === totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button></div>
          </div>
        </section>
      </main>
    </ShellLayout>
  )
}
