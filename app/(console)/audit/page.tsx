'use client'

import { Fragment, useEffect, useState } from 'react'
import { CheckCircle2, ChevronDown, ChevronRight, Download, FileJson, Lock, RefreshCw, Search, ShieldCheck, ShieldOff, XCircle } from 'lucide-react'
import { useAuth, roleLabel } from '@/components/auth/auth-provider'
import { ActorSummaryDialog, EntityTraceDialog } from '@/components/audit/drilldowns'
import { EntryDetail } from '@/components/audit/entry-detail'
import { ErrorNotice } from '@/components/ui/error-notice'
import type { Role } from '@/lib/api'
import { type AuditChainVerifyResult, ApiError, downloadAuditExport, getAuditFacets, listAuditEntries, verifyAuditChain } from '@/lib/api'
import {
  type AuditFilterState,
  NO_FILTER,
  filterFromUrl,
  filterToUrl,
  indiaStamp,
  isFiltered,
  pageWindow,
  queryFor,
  rangeProblem,
  verifyChecks,
  verifyFailed,
} from '@/lib/audit'
import { can } from '@/lib/permissions'
import { useQuery } from '@/lib/use-query'

const PAGE_SIZES = [25, 50, 100]
/** The first page follows the log as it grows; a later page stays where the person put it. */
const LIVE_REFRESH_MS = 15_000

function AccessRestricted({ role }: { role: Role }) {
  return (
    <>
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
    </>
  )
}

/** A drop-down of the values the log holds. The current choice stays listed even if the server did not offer it (a link can set any value). */
function FacetSelect({ label, value, values, allLabel, onChange }: { label: string; value: string; values: string[]; allLabel: string; onChange: (value: string) => void }) {
  const options = value && !values.includes(value) ? [value, ...values] : values
  return (
    <label className="audit-filter-field">
      <span>{label}</span>
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{allLabel}</option>
        {options.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    </label>
  )
}

function VerifyResult({ result }: { result: AuditChainVerifyResult }) {
  return (
    <ul className="audit-verify" aria-label="Verification result">
      {verifyChecks(result).map((check) => (
        <li key={check.label} data-ok={check.ok}>
          {check.ok ? <CheckCircle2 className="size-4" aria-hidden="true" /> : <XCircle className="size-4" aria-hidden="true" />}
          <div>
            <strong>{check.label}: {check.ok ? 'holds' : 'does not hold'}</strong>
            <span>{check.detail}</span>
          </div>
        </li>
      ))}
    </ul>
  )
}

export default function AuditLogPage() {
  const { role } = useAuth()
  const canView = can(role, 'GET /api/v1/audit/entries')

  const [filter, setFilter] = useState<AuditFilterState>(NO_FILTER)
  const [typedSearch, setTypedSearch] = useState('')
  const [urlRead, setUrlRead] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [trace, setTrace] = useState<string | null>(null)
  const [actorSummary, setActorSummary] = useState<string | null>(null)
  const [verifyResult, setVerifyResult] = useState<AuditChainVerifyResult | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [exporting, setExporting] = useState<'csv' | 'ndjson' | null>(null)
  const [error, setError] = useState<string | null>(null)

  // A link from another page (or a shared address) opens the log already narrowed.
  useEffect(() => {
    // The address bar only exists in the browser, so it is read once here rather than while rendering.
    const fromUrl = filterFromUrl(window.location.search)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFilter(fromUrl)
    setTypedSearch(fromUrl.search)
    setUrlRead(true)
  }, [])

  // Keep the address in step with the filters, so the page can be shared or reloaded as it is.
  useEffect(() => {
    if (!urlRead) return
    window.history.replaceState(null, '', `${window.location.pathname}${filterToUrl(filter)}`)
  }, [filter, urlRead])

  // The search box applies a moment after typing stops, so each keystroke is not a request.
  useEffect(() => {
    const id = setTimeout(() => {
      setFilter((current) => (current.search === typedSearch ? current : { ...current, search: typedSearch }))
      setPage(1)
    }, 300)
    return () => clearTimeout(id)
  }, [typedSearch])

  const problem = rangeProblem(filter)
  const query = queryFor(filter)
  const offset = (page - 1) * pageSize
  const key = JSON.stringify([query, page, pageSize])
  const {
    data: loaded,
    error: loadError,
    loading,
    lastUpdated,
    refresh: reload,
  } = useQuery(key, () => listAuditEntries({ ...query, limit: pageSize, offset }), {
    intervalMs: page === 1 ? LIVE_REFRESH_MS : undefined,
    enabled: canView && urlRead && !problem,
  })
  const { data: facets, error: facetsError } = useQuery('facets', getAuditFacets, { enabled: canView })

  const entries = loaded?.items ?? []
  const total = loaded?.total ?? 0
  const shown = pageWindow(total, page, pageSize)
  const filtered = isFiltered(filter)

  function change(patch: Partial<AuditFilterState>) {
    setFilter((current) => ({ ...current, ...patch }))
    setPage(1)
    setExpanded(null)
  }

  function clearFilters() {
    setFilter(NO_FILTER)
    setTypedSearch('')
    setPage(1)
    setExpanded(null)
  }

  function showOnly(patch: Partial<AuditFilterState>) {
    setTrace(null)
    setActorSummary(null)
    change(patch)
  }

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
      await downloadAuditExport(format, query)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Export failed')
    } finally {
      setExporting(null)
    }
  }

  if (!canView) return <AccessRestricted role={role} />

  const failed = verifyResult !== null && verifyFailed(verifyResult)

  return (
    <>
      <main className="audit-page mx-auto w-full max-w-[1700px] pb-10">
        <header className="audit-header">
          <div><p className="eyebrow">TRADINGOS // COMPLIANCE</p><h1>Audit Log</h1><p>Immutable, hash-chained record of every human and agent action across the platform.</p></div>
          <div className="audit-export-group">
            <button className="audit-export-btn" onClick={() => handleExport('csv')} disabled={exporting !== null || !!problem} title={filtered ? 'Exports the entries the filters show' : 'Exports the whole log'}>{exporting === 'csv' ? <RefreshCw className="size-3.5 animate-spin" /> : <Download className="size-3.5" />} Export CSV</button>
            <button className="audit-export-btn" onClick={() => handleExport('ndjson')} disabled={exporting !== null || !!problem} title={filtered ? 'Exports the entries the filters show' : 'Exports the whole log'}>{exporting === 'ndjson' ? <RefreshCw className="size-3.5 animate-spin" /> : <FileJson className="size-3.5" />} Export NDJSON</button>
          </div>
        </header>

        {error && <ErrorNotice className="mb-3" message={error} />}
        {loadError && <ErrorNotice className="mb-3" message={`Could not load the audit log: ${loadError}`} onRetry={() => void reload()} />}

        <div className="chain-integrity-strip" data-failed={failed}>
          {failed ? <ShieldOff className="size-4 text-rose-300" /> : <ShieldCheck className="size-4" />}
          <span>{verifyResult === null ? 'Chain not yet verified this session' : failed ? `Divergence detected: ${verifyResult.reason ?? verifyResult.db_chain_reason ?? 'unknown'}` : 'Hash chain verified — no divergence detected'}</span>
          <span className="chain-integrity-sep">&middot;</span>
          {role === 'SystemAdministrator' && <button className="audit-export-btn" onClick={handleVerify} disabled={verifying}>{verifying ? <RefreshCw className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />} Run verification</button>}
          <span className="chain-integrity-badge">{total.toLocaleString('en-IN')} {total === 1 ? 'entry' : 'entries'}{filtered ? ' match' : ''}</span>
        </div>
        {verifyResult && <VerifyResult result={verifyResult} />}

        <div className="audit-filters">
          <label className="audit-search-box"><Search className="size-3.5" /><input placeholder="Search entity id, actor, action..." aria-label="Search the log" value={typedSearch} onChange={(e) => setTypedSearch(e.target.value)} /></label>
          <FacetSelect label="Actor" value={filter.actor} values={facets?.actors ?? []} allLabel="All actors" onChange={(actor) => change({ actor })} />
          <FacetSelect label="Entity type" value={filter.entityType} values={facets?.entity_types ?? []} allLabel="All entities" onChange={(entityType) => change({ entityType })} />
          <FacetSelect label="Action" value={filter.action} values={facets?.actions ?? []} allLabel="All actions" onChange={(action) => change({ action })} />
        </div>
        <div className="audit-filters">
          <label className="audit-filter-field"><span>Entity id</span><input aria-label="Entity id" value={filter.entityId} onChange={(e) => change({ entityId: e.target.value })} placeholder="exact id" /></label>
          <label className="audit-filter-field"><span>From (India time)</span><input aria-label="From (India time)" type="datetime-local" value={filter.since} onChange={(e) => change({ since: e.target.value })} /></label>
          <label className="audit-filter-field"><span>To (India time)</span><input aria-label="To (India time)" type="datetime-local" value={filter.until} onChange={(e) => change({ until: e.target.value })} /></label>
          <label className="audit-filter-field"><span>Rows per page</span><select aria-label="Rows per page" value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1) }}>{PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}</select></label>
          {filtered && <button type="button" className="button-secondary" onClick={clearFilters}>Clear filters</button>}
        </div>
        {problem && <ErrorNotice className="mb-3" message={problem} />}
        {facetsError && <p className="audit-muted">The filter choices could not be loaded ({facetsError}); the boxes still work when typed into.</p>}
        {facets?.truncated && <p className="audit-muted">The server lists only the first 500 values of each filter; search finds the rest.</p>}

        <section className="audit-panel" aria-busy={loading}>
          <div className="overflow-x-auto">
            <table className="audit-table">
              <thead><tr><th></th><th>Time (India)</th><th>Actor</th><th>Action</th><th>Entity type</th><th>Entity id</th><th>Sequence</th></tr></thead>
              <tbody>
                {entries.map((e) => {
                  const isOpen = expanded === e.id
                  return (
                    <Fragment key={e.id}>
                      <tr className={isOpen ? 'audit-row-open' : ''} onClick={() => setExpanded(isOpen ? null : e.id)}>
                        <td className="audit-expand-cell"><button type="button" className="audit-expand-btn" aria-expanded={isOpen} aria-label={`${isOpen ? 'Hide' : 'Show'} details of entry ${e.sequence}`} onClick={(event) => { event.stopPropagation(); setExpanded(isOpen ? null : e.id) }}>{isOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}</button></td>
                        <td className="mono">{indiaStamp(e.created_at)}</td>
                        <td><button type="button" className="audit-link" onClick={(event) => { event.stopPropagation(); setActorSummary(e.actor) }} aria-label={`Summarise ${e.actor}`}>{e.actor}</button></td>
                        <td><span className="audit-action-chip">{e.action}</span></td>
                        <td>{e.entity_type ?? '—'}</td>
                        <td className="mono">{e.entity_id ? <button type="button" className="audit-link mono" onClick={(event) => { event.stopPropagation(); setTrace(e.entity_id) }} aria-label={`Trace ${e.entity_id}`}>{e.entity_id}</button> : '—'}</td>
                        <td className="mono">#{e.sequence}</td>
                      </tr>
                      {isOpen && <tr className="audit-diff-row"><td colSpan={7}><EntryDetail entry={e} /></td></tr>}
                    </Fragment>
                  )
                })}
                {entries.length === 0 && (
                  <tr><td colSpan={7} className="audit-empty">
                    {loading ? 'Loading the log…' : filtered ? <>No audit entries match these filters. <button type="button" className="audit-link" onClick={clearFilters}>Clear the filters</button></> : 'The audit log is empty.'}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="audit-pagination">
            <span>Showing {shown.from.toLocaleString('en-IN')}&ndash;{shown.to.toLocaleString('en-IN')} of {total.toLocaleString('en-IN')}</span>
            <span className="audit-muted">{page === 1 ? 'Refreshes every 15 seconds' : 'Not refreshing on this page, so the rows stay put'}{lastUpdated ? ` · updated ${indiaStamp(lastUpdated.toISOString())}` : ''}</span>
            <div className="audit-pagination-btns">
              <button disabled={shown.current === 1} onClick={() => setPage(1)}>First</button>
              <button disabled={shown.current === 1} onClick={() => setPage(shown.current - 1)}>Prev</button>
              <span>{shown.current} / {shown.pages}</span>
              <button disabled={shown.current === shown.pages} onClick={() => setPage(shown.current + 1)}>Next</button>
              <button disabled={shown.current === shown.pages} onClick={() => setPage(shown.pages)}>Last</button>
              <button onClick={() => void reload()} aria-label="Refresh the log"><RefreshCw className="size-3" /></button>
            </div>
          </div>
        </section>
      </main>

      {trace && <EntityTraceDialog entityId={trace} onClose={() => setTrace(null)} onShowOnly={() => showOnly({ entityId: trace })} />}
      {actorSummary && <ActorSummaryDialog actor={actorSummary} onClose={() => setActorSummary(null)} onShowOnly={() => showOnly({ actor: actorSummary })} />}
    </>
  )
}
