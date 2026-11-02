'use client'

import { type AuditActorSummary, type AuditTrace, getAuditActorSummary, getAuditTrace } from '@/lib/api'
import { actionShares, indiaStamp, shortHash } from '@/lib/audit'
import { useQuery } from '@/lib/use-query'
import { ErrorNotice } from '@/components/ui/error-notice'
import { Modal } from '@/components/ui/modal'
import { EntryDetail } from './entry-detail'

function Waiting({ what }: { what: string }) {
  return <p className="audit-drill-wait">Loading {what}…</p>
}

/** Everything the log holds about one thing (an order, a strategy, a subscription), oldest first. */
export function EntityTraceDialog({
  entityId,
  onClose,
  onShowOnly,
}: {
  entityId: string
  onClose: () => void
  /** Narrow the log itself to this entity and close. */
  onShowOnly: () => void
}) {
  const { data, error, refresh } = useQuery<AuditTrace>(`trace:${entityId}`, () => getAuditTrace(entityId))

  return (
    <Modal
      wide
      title={`Everything recorded for ${entityId}`}
      description={data ? `${data.entry_count} ${data.entry_count === 1 ? 'entry' : 'entries'}, oldest first.` : undefined}
      onClose={onClose}
    >
      {error && !data && <ErrorNotice className="mt-3" message={`Could not load this trace: ${error}`} onRetry={() => void refresh()} />}
      {!data && !error && <Waiting what="the trace" />}
      {data && (
        <ol className="audit-trace" aria-label={`Entries for ${entityId}`}>
          {data.entries.map((entry) => (
            <li key={entry.id} className="audit-trace-item">
              <div className="audit-trace-head">
                <span className="mono audit-trace-seq">#{entry.sequence}</span>
                <span className="audit-action-chip">{entry.action}</span>
                <span>{entry.actor}</span>
                <time className="mono" dateTime={entry.created_at}>
                  {indiaStamp(entry.created_at)}
                </time>
                <span className="mono audit-trace-hash" title={entry.hash}>
                  {shortHash(entry.hash)}
                </span>
              </div>
              <details>
                <summary>Details and hashes</summary>
                <EntryDetail entry={entry} />
              </details>
            </li>
          ))}
        </ol>
      )}
      <div className="ui-modal-foot">
        <button type="button" className="button-secondary" onClick={onClose}>
          Close
        </button>
        <button type="button" className="button-primary" onClick={onShowOnly}>
          Show only these in the log
        </button>
      </div>
    </Modal>
  )
}

/** What one actor has done: how much, of what kind, and since when. */
export function ActorSummaryDialog({
  actor,
  onClose,
  onShowOnly,
}: {
  actor: string
  onClose: () => void
  onShowOnly: () => void
}) {
  const { data, error, refresh } = useQuery<AuditActorSummary>(`actor:${actor}`, () => getAuditActorSummary(actor))
  const shares = data ? actionShares(data) : []

  return (
    <Modal wide title={actor} description="What this person or agent has done, from the audit log." onClose={onClose}>
      {error && !data && <ErrorNotice className="mt-3" message={`Could not load this summary: ${error}`} onRetry={() => void refresh()} />}
      {!data && !error && <Waiting what="the summary" />}
      {data && (
        <>
          <dl className="audit-facts">
            <div>
              <dt>Entries</dt>
              <dd className="mono">{data.action_count.toLocaleString('en-IN')}</dd>
            </div>
            <div>
              <dt>First seen</dt>
              <dd className="mono">{indiaStamp(data.first_seen)}</dd>
            </div>
            <div>
              <dt>Last seen</dt>
              <dd className="mono">{indiaStamp(data.last_seen)}</dd>
            </div>
          </dl>
          <table className="audit-share-table">
            <caption>Actions, most frequent first</caption>
            <thead>
              <tr>
                <th scope="col">Action</th>
                <th scope="col">Count</th>
                <th scope="col">Share</th>
              </tr>
            </thead>
            <tbody>
              {shares.map((row) => (
                <tr key={row.action}>
                  <td>
                    <span className="audit-action-chip">{row.action}</span>
                  </td>
                  <td className="mono">{row.count.toLocaleString('en-IN')}</td>
                  <td>
                    <span className="audit-share-bar" aria-hidden="true">
                      <span style={{ width: `${Math.max(2, Math.round(row.share * 100))}%` }} />
                    </span>
                    <span className="mono audit-share-pct">{Math.round(row.share * 100)}%</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <div className="ui-modal-foot">
        <button type="button" className="button-secondary" onClick={onClose}>
          Close
        </button>
        <button type="button" className="button-primary" onClick={onShowOnly}>
          Show this actor’s entries
        </button>
      </div>
    </Modal>
  )
}
