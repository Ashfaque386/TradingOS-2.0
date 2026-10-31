'use client'

import { useCallback, useMemo, useState } from 'react'
import { CheckCircle2, Lock, X } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { useConsoleState } from '@/components/shell/console-state'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import {
  ApiError,
  type KillSwitchMode,
  type RiskLimitChange,
  applyRiskLimitChange,
  checkKillSwitchDrill,
  confirmRiskLimitChange,
  listRiskLimitCatalog,
  listRiskLimitChanges,
  rejectRiskLimitChange,
  stageRiskLimitChange,
} from '@/lib/api'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'

const STATUS_TEXT: Record<string, string> = {
  staged: 'Waiting for a second person to confirm',
  confirmed: 'Confirmed, waiting to be applied',
  applied: 'Applied',
  rejected: 'Rejected',
}

function who(label: string | null, id: string | null, me: string | undefined): string {
  if (!id) return 'unknown'
  if (id === me) return 'you'
  return label ?? `${id.slice(0, 8)}…`
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : ''
}

/**
 * Risk limits (dual control): the limits that exist and what is in force, a form that offers only
 * those limits and their allowed range, every change with who did what and when (rejected ones
 * included), and a drill that judges numbers you type without changing the limit.
 */
export function RiskLimitsSection() {
  const { role, user } = useAuth()
  const canOperate = can(role, 'POST /api/v1/risk-limits/stage')
  const { refresh: refreshConsole } = useConsoleState()

  const { data, error: loadError, refresh } = usePolling(
    async () => {
      const [catalog, changes] = await Promise.all([listRiskLimitCatalog(), listRiskLimitChanges()])
      return { catalog, changes }
    },
    { intervalMs: 30_000 },
  )
  const catalog = useMemo(() => data?.catalog ?? [], [data])
  const changes = useMemo(() => data?.changes ?? [], [data])

  const [limitName, setLimitName] = useState('')
  const [proposedValue, setProposedValue] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rejecting, setRejecting] = useState<RiskLimitChange | null>(null)
  const [drillMode, setDrillMode] = useState<KillSwitchMode | null>(null)
  const [drill, setDrill] = useState({ current: '', peak: '', threshold: '' })
  const [drillResult, setDrillResult] = useState<string | null>(null)

  const chosen = catalog.find((spec) => spec.name === limitName) ?? null
  const value = Number(proposedValue)
  const rangeProblem =
    chosen && proposedValue !== '' && !(value >= chosen.minimum && value <= chosen.maximum)
      ? `${chosen.label} must be between ${chosen.minimum} and ${chosen.maximum} ${chosen.unit}`
      : null

  const act = useCallback(
    async (work: () => Promise<unknown>, fallback: string) => {
      setBusy(true)
      setError(null)
      try {
        await work()
        await Promise.all([refresh(), refreshConsole()])
      } catch (err) {
        setError(err instanceof ApiError ? err.message : fallback)
      } finally {
        setBusy(false)
      }
    },
    [refresh, refreshConsole],
  )

  async function handleStage() {
    if (!chosen || proposedValue === '' || rangeProblem) return
    await act(async () => {
      await stageRiskLimitChange(chosen.name, value, reason.trim() || undefined)
      setLimitName('')
      setProposedValue('')
      setReason('')
    }, 'Could not stage that change')
  }

  async function runDrill(mode: KillSwitchMode) {
    const state = await checkKillSwitchDrill(mode, {
      current_equity: Number(drill.current),
      peak_equity: Number(drill.peak),
      ...(drill.threshold ? { threshold_pct: Number(drill.threshold) } : {}),
    }).catch((err) => {
      throw new Error(err instanceof ApiError ? err.message : 'The drill could not be run')
    })
    setDrillResult(
      state.tripped
        ? `The ${mode} kill switch is tripped: ${state.trip_reason ?? 'no reason recorded'}.`
        : `Within the limit: ${state.last_drawdown_pct?.toFixed(2) ?? 'unknown'}% drawdown. Nothing tripped.`,
    )
    await refreshConsole()
  }

  const drillReady = Number(drill.current) > 0 && Number(drill.peak) > 0

  return (
    <div className="set-block">
      <div className="set-block-head">
        <div>
          <h2>Risk Limits</h2>
          <p>Thresholds change only through dual control &mdash; stage &rarr; a different user confirms &rarr; apply.</p>
        </div>
      </div>
      <div className="set-dualcontrol-banner">
        <Lock className="size-4" />
        <div>
          <strong>Dual-control enforced</strong>
          <p>Confirming your own staged change is rejected server-side — a genuinely different user must confirm.</p>
        </div>
      </div>
      {error && <ErrorNotice className="mb-3" message={error} />}
      {loadError && <ErrorNotice className="mb-3" message={`Could not load the risk limits: ${loadError}`} onRetry={() => void refresh()} />}

      <p className="set-group-label">Limits in force</p>
      <div className="set-risk-list">
        {catalog.map((spec) => (
          <div key={spec.name} className="set-risk-row">
            <div className="set-risk-meta">
              <span className="set-risk-label">{spec.label}</span>
              <strong className="mono">{spec.effective}{spec.unit}</strong>
              <small className="block text-muted-foreground">
                {spec.applied ? 'applied through dual control' : `default, none applied yet`} · {spec.enforced ? 'read by the automatic kill switch' : 'not read by anything yet, so changing it changes nothing'}
              </small>
              <small className="block text-muted-foreground">{spec.description}</small>
            </div>
          </div>
        ))}
        {data && catalog.length === 0 && <p className="text-xs text-muted-foreground">No risk limits are defined.</p>}
      </div>

      {canOperate && (
        <div className="mt-4 flex flex-wrap items-end gap-2 rounded-xl border border-white/10 p-3">
          <label className="set-field">
            <span>Limit</span>
            <select value={limitName} onChange={(e) => setLimitName(e.target.value)}>
              <option value="">Choose a limit…</option>
              {catalog.map((spec) => <option key={spec.name} value={spec.name}>{spec.label}</option>)}
            </select>
          </label>
          <label className="set-field">
            <span>Proposed value{chosen ? ` (${chosen.unit})` : ''}</span>
            <input value={proposedValue} onChange={(e) => setProposedValue(e.target.value)} inputMode="decimal" disabled={!chosen} />
          </label>
          <label className="set-field">
            <span>Reason</span>
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button className="set-btn set-btn-primary" disabled={busy || !chosen || proposedValue === '' || rangeProblem !== null} onClick={handleStage}>
            Stage change
          </button>
          {rangeProblem && <p className="w-full text-xs text-rose-300" role="alert">{rangeProblem}</p>}
        </div>
      )}

      <p className="set-group-label mt-4">Changes</p>
      <div className="set-risk-list">
        {changes.map((c) => {
          const mine = c.staged_by === user?.id
          const open = c.status === 'staged' || c.status === 'confirmed'
          const spec = catalog.find((s) => s.name === c.limit_name)
          return (
            <div key={c.id} className={`set-risk-row set-risk-${c.status === 'staged' ? 'staged' : c.status === 'confirmed' ? 'awaiting' : c.status}`}>
              <div className="set-risk-meta">
                <span className="set-risk-label">{spec?.label ?? c.limit_name}</span>
                <strong className="mono">→ {c.proposed_value}{spec?.unit ?? ''}</strong>
                <small className="block text-muted-foreground">{STATUS_TEXT[c.status] ?? c.status}</small>
                <small className="block text-muted-foreground">
                  staged by {who(c.staged_by_label, c.staged_by, user?.id)} {when(c.staged_at)}
                  {c.reason ? ` — “${c.reason}”` : ''}
                </small>
                {c.confirmed_by && <small className="block text-muted-foreground">confirmed by {who(c.confirmed_by_label, c.confirmed_by, user?.id)} {when(c.confirmed_at)}</small>}
                {c.applied_by && <small className="block text-muted-foreground">applied by {who(c.applied_by_label, c.applied_by, user?.id)} {when(c.applied_at)}</small>}
                {c.rejected_by && (
                  <small className="block text-muted-foreground">
                    {c.rejected_by === c.staged_by ? 'cancelled' : 'rejected'} by {who(c.rejected_by_label, c.rejected_by, user?.id)} {when(c.rejected_at)}
                    {c.rejection_reason ? ` — “${c.rejection_reason}”` : ''}
                  </small>
                )}
              </div>
              {canOperate && c.status === 'staged' && (
                <button className="set-btn set-btn-primary" disabled={busy || mine} onClick={() => act(() => confirmRiskLimitChange(c.id), 'Confirmation failed')}>
                  {mine ? 'Needs a different confirmer' : 'Confirm as second user'}
                </button>
              )}
              {canOperate && c.status === 'confirmed' && (
                <button className="set-btn set-btn-primary" disabled={busy} onClick={() => act(() => applyRiskLimitChange(c.id), 'Apply failed')}>
                  <CheckCircle2 className="size-3.5" /> Apply
                </button>
              )}
              {canOperate && open && (
                <button className="set-btn set-btn-danger" disabled={busy} onClick={() => setRejecting(c)}>
                  <X className="size-3.5" /> {mine ? 'Cancel this change' : 'Reject'}
                </button>
              )}
            </div>
          )
        })}
        {data && changes.length === 0 && <p className="text-xs text-muted-foreground">No limit has ever been changed.</p>}
      </div>

      {canOperate && (
        <details className="mt-4 rounded-xl border border-white/10 p-3">
          <summary className="cursor-pointer text-xs text-muted-foreground">Drill: judge numbers you type (the limit is not changed)</summary>
          <p className="mt-2 text-xs text-muted-foreground">
            Runs the drawdown check on the equity and peak below. If they breach the threshold, the real kill switch for that mode trips and stays tripped until reset.
          </p>
          <div className="mt-2 flex flex-wrap items-end gap-2">
            <label className="set-field"><span>Equity now</span><input value={drill.current} onChange={(e) => setDrill({ ...drill, current: e.target.value })} inputMode="decimal" /></label>
            <label className="set-field"><span>Highest equity</span><input value={drill.peak} onChange={(e) => setDrill({ ...drill, peak: e.target.value })} inputMode="decimal" /></label>
            <label className="set-field"><span>Threshold % (optional, this drill only)</span><input value={drill.threshold} onChange={(e) => setDrill({ ...drill, threshold: e.target.value })} inputMode="decimal" /></label>
            {(['paper', 'live'] as const).map((mode) => (
              <button key={mode} className="set-btn set-btn-secondary" disabled={!drillReady} onClick={() => setDrillMode(mode)}>
                Run on {mode}
              </button>
            ))}
          </div>
          {drillResult && <p className="mt-2 text-xs text-cyan-200" role="status">{drillResult}</p>}
        </details>
      )}

      {rejecting && (
        <ConfirmDialog
          title={rejecting.staged_by === user?.id ? 'Cancel this change' : 'Reject this change'}
          description={`Nothing changes: ${rejecting.limit_name} stays as it is. Who did this and why is kept.`}
          confirmLabel={rejecting.staged_by === user?.id ? 'Cancel the change' : 'Reject the change'}
          tone="danger"
          reason={{ label: 'Reason (optional)', minLength: 0 }}
          onConfirm={async (why) => {
            try {
              await rejectRiskLimitChange(rejecting.id, why || undefined)
            } finally {
              await refresh()
            }
          }}
          onClose={() => setRejecting(null)}
        />
      )}
      {drillMode && (
        <ConfirmDialog
          title={`Run the drill on ${drillMode}`}
          description={`This judges equity ${drill.current} against a peak of ${drill.peak}. If that breaches the threshold, the real ${drillMode} kill switch trips.`}
          confirmLabel={`Run on ${drillMode}`}
          tone="danger"
          phrase={`DRILL ${drillMode.toUpperCase()}`}
          onConfirm={() => runDrill(drillMode)}
          onClose={() => setDrillMode(null)}
        />
      )}
    </div>
  )
}
