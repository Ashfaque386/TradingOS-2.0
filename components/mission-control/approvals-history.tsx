'use client'

import { ErrorNotice } from '@/components/ui/error-notice'
import type { ApprovalRequestDto } from '@/lib/api'

const TRANSITIONS: Record<string, string> = {
  backtesting_to_papertrading: 'promotion to paper trading',
  papertrading_to_liveeligible: 'live eligibility',
}

export function describeApproval(approval: ApprovalRequestDto): string {
  const what = TRANSITIONS[approval.transition_type] ?? approval.transition_type
  const subject = approval.subject_label ?? `${approval.subject_type} ${approval.subject_id.slice(0, 8)}`
  return `${subject}: ${what}`
}

/**
 * Every sign-off that has been decided, newest first: which strategy, what for, who asked, who decided,
 * when, and the reason they gave. The queue above drops a request the moment it is decided; this is
 * where it stays. It shows the same list the queue uses, so a decision appears here as soon as it is made.
 */
export function ApprovalsHistory({
  approvals: data,
  error,
  onRetry,
}: {
  approvals: ApprovalRequestDto[] | null
  error: string | null
  onRetry: () => void
}) {
  const decided = (data ?? []).filter((a) => a.status !== 'pending')

  return (
    <section className="pulse-panel p-5 xl:col-span-2" aria-label="Sign-off history">
      <p className="eyebrow">SIGN-OFF HISTORY</p>
      <p className="mt-1 text-xs text-muted-foreground">Approved and rejected requests, with who decided and why.</p>
      {error && <ErrorNotice className="mt-3" message={`Could not load the history: ${error}`} onRetry={onRetry} />}
      <table className="mt-3 w-full text-left text-xs">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-1">Decided</th>
            <th>For</th>
            <th>Result</th>
            <th>Asked by</th>
            <th>Decided by</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {decided.map((a) => (
            <tr key={a.id} className="border-t border-white/5 align-top">
              <td className="py-1 font-mono">{a.decided_at ? new Date(a.decided_at).toLocaleString() : '—'}</td>
              <td>{describeApproval(a)}</td>
              <td className={a.status === 'approved' ? 'text-emerald-200' : 'text-rose-300'}>{a.status === 'approved' ? 'Approved' : 'Rejected'}</td>
              <td>{a.requested_by_label ?? a.requested_by ?? 'unknown'}</td>
              <td>{a.decided_by_label ?? a.decided_by ?? 'unknown'}</td>
              <td>{a.decision_reason ?? <span className="text-muted-foreground">none given</span>}</td>
            </tr>
          ))}
          {data && decided.length === 0 && (
            <tr><td colSpan={6} className="py-3 text-muted-foreground">Nothing has been decided yet.</td></tr>
          )}
        </tbody>
      </table>
    </section>
  )
}
