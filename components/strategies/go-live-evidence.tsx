'use client'

import { useState } from 'react'
import { CheckCircle2, XCircle } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import {
  ApiError,
  type GoLiveEvidence,
  type GoLiveReadinessInput,
  type GoLiveReadinessResult,
  approveLiveEligibility,
  checkGoLiveReadiness,
  getGoLiveEvidence,
} from '@/lib/api'
import { usePolling } from '@/lib/use-polling'

const BLANK: GoLiveReadinessInput = {
  num_trades: 0,
  calendar_days_running: 0,
  clean_shadow_mode_streak_days: 0,
  live_win_rate: null,
  backtest_win_rate: null,
}

const INPUT_LABELS: Record<string, string> = {
  num_trades: 'Trades',
  calendar_days_running: 'Calendar days running',
  clean_shadow_mode_streak_days: 'Clean Shadow Mode streak',
  live_win_rate: 'Paper win rate',
  backtest_win_rate: 'Backtest win rate',
}

const pct = (value: number | null) => (value === null ? 'unknown' : `${(value * 100).toFixed(1)}%`)

function Row({ ok, title, value, threshold, detail }: { ok: boolean; title: string; value: string; threshold: string; detail: string }) {
  return (
    <div className="readiness-row">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-xs font-medium">
          {ok ? <CheckCircle2 className="size-4 text-emerald-300" aria-hidden="true" /> : <XCircle className="size-4 text-rose-300" aria-hidden="true" />}
          {title}
          <span className="sr-only">{ok ? 'met' : 'not met'}</span>
        </span>
        <span className="font-mono text-xs">
          {value} <span className="text-muted-foreground">/ {threshold}</span>
        </span>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">{detail}</p>
    </div>
  )
}

function detailFor(e: GoLiveEvidence) {
  const setAside = [
    e.excluded_unreal_fills ? `${e.excluded_unreal_fills} on generated or typed prices` : null,
    e.excluded_administrative_fills ? `${e.excluded_administrative_fills} hand-made cleanups` : null,
  ].filter(Boolean)
  return {
    trades:
      `Closing fills on real prices: ${e.counted_closing_fills} of ${e.counted_fills} real-price fills.` +
      (setAside.length ? ` Set aside: ${setAside.join(', ')}.` : ''),
    days: e.first_counted_fill_at
      ? `Since the first real-price fill, ${new Date(e.first_counted_fill_at).toLocaleDateString()}.`
      : 'Counted from the first fill on real prices, and there is none yet.',
    streak:
      'Consecutive trading days with a dry run that worked and none that failed.' +
      ` ${e.shadow_clean_days_real_sandbox} of the counted days were checked against a broker's own sandbox.` +
      (e.shadow_runs_failed_recently ? ` ${e.shadow_runs_failed_recently} failed in the last 14 days.` : ''),
    win:
      e.inputs.live_win_rate === null || e.inputs.backtest_win_rate === null
        ? 'Undetermined: it needs both a paper win rate and a backtest on real data.'
        : `Paper ${pct(e.inputs.live_win_rate)} against backtest ${pct(e.inputs.backtest_win_rate)} (${e.backtest_data_source} data): ${(Math.abs(e.inputs.live_win_rate - e.inputs.backtest_win_rate) * 100).toFixed(1)} points apart.`,
  }
}

/**
 * Go-Live readiness, worked out by the system from its own records: closing fills on real prices, the
 * days they span, the win rate against a real-data backtest, and the clean Shadow Mode days. Nothing is
 * typed in to sign off. A person can override with numbers of their own, but only with a reason, and the
 * audit trail then holds those beside what the records said.
 */
export function GoLiveEvidencePanel({
  strategyId,
  stage,
  canSignOff,
  onChanged,
}: {
  strategyId: string
  stage: string
  canSignOff: boolean
  onChanged: () => Promise<void> | void
}) {
  const { data: evidence, error, refresh } = usePolling(() => getGoLiveEvidence(strategyId), { intervalMs: 30_000 })
  const [confirming, setConfirming] = useState<'derived' | 'override' | null>(null)
  const [override, setOverride] = useState<GoLiveReadinessInput>(BLANK)
  const [whatIf, setWhatIf] = useState<GoLiveReadinessResult | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const alreadyEligible = stage === 'Live Eligible' || stage === 'Live'
  const text = evidence ? detailFor(evidence) : null

  async function signOff(reason: string | null) {
    try {
      await approveLiveEligibility(strategyId, reason ? { ...override, reason } : undefined)
    } catch (err) {
      throw new Error(err instanceof ApiError ? err.message : 'The sign-off did not go through')
    }
    await Promise.all([onChanged(), refresh()])
  }

  async function checkNumbers() {
    setMessage(null)
    try {
      setWhatIf(await checkGoLiveReadiness(override))
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : 'Could not check those numbers')
    }
  }

  return (
    <div className="mt-6 rounded-xl border border-white/10 p-4" aria-label="Go-live evidence">
      <p className="eyebrow">GO-LIVE READINESS · WORKED OUT FROM THE RECORDS</p>
      {error && <ErrorNotice className="mt-3" message={`Could not load the evidence: ${error}`} onRetry={() => void refresh()} />}
      {!evidence && !error && <p className="mt-3 text-xs text-muted-foreground">Working out the evidence…</p>}

      {evidence && text && (
        <>
          <div className="readiness-list mt-3">
            <Row
              ok={evidence.checks.min_trades}
              title="Trades"
              value={String(evidence.inputs.num_trades)}
              threshold={`${evidence.thresholds.min_trades} needed`}
              detail={text.trades}
            />
            <Row
              ok={evidence.checks.min_calendar_days}
              title="Calendar days running"
              value={String(evidence.inputs.calendar_days_running)}
              threshold={`${evidence.thresholds.min_calendar_days} needed`}
              detail={text.days}
            />
            <Row
              ok={evidence.checks.clean_shadow_streak}
              title="Clean Shadow Mode streak"
              value={String(evidence.inputs.clean_shadow_mode_streak_days)}
              threshold={`${evidence.thresholds.min_clean_shadow_days} days needed`}
              detail={text.streak}
            />
            <Row
              ok={evidence.checks.win_rate_divergence}
              title="Win rate against the backtest"
              value={
                evidence.inputs.live_win_rate === null || evidence.inputs.backtest_win_rate === null
                  ? 'undetermined'
                  : `${(Math.abs(evidence.inputs.live_win_rate - evidence.inputs.backtest_win_rate) * 100).toFixed(1)} pts apart`
              }
              threshold={`${evidence.thresholds.max_win_rate_divergence_pp} pts at most`}
              detail={text.win}
            />
          </div>

          {evidence.not_derived.length > 0 && (
            <div className="mt-3 rounded-lg border border-amber-300/20 bg-amber-300/[.06] p-3 text-xs text-amber-100">
              <strong>Could not be worked out from the records</strong>
              <ul className="mt-1 list-disc pl-4">
                {evidence.not_derived.map((item) => (
                  <li key={item.input}>
                    <span className="font-medium">{INPUT_LABELS[item.input] ?? item.input}:</span> {item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            {alreadyEligible ? (
              <p className="text-xs text-emerald-200">This strategy is signed off as live eligible.</p>
            ) : canSignOff ? (
              <button
                type="button"
                className="button-primary"
                disabled={!evidence.eligible}
                onClick={() => setConfirming('derived')}
                title={evidence.eligible ? undefined : 'The evidence does not meet the gate yet'}
              >
                Sign off as live eligible
              </button>
            ) : (
              <p className="text-xs text-muted-foreground">Only an Administrator or Risk Manager can sign a strategy off.</p>
            )}
            {!alreadyEligible && canSignOff && !evidence.eligible && (
              <span className="text-xs text-muted-foreground">The gate is not met, so there is nothing to sign off yet.</span>
            )}
          </div>

          {!alreadyEligible && canSignOff && (
            <details className="mt-4 rounded-lg border border-white/10 p-3">
              <summary className="cursor-pointer text-xs text-muted-foreground">Override with numbers of your own (audited, needs a reason)</summary>
              <p className="mt-2 text-xs text-muted-foreground">
                Use this only when the records cannot show what you know, for example a backtest on data the system could not vouch for. The
                audit trail keeps what you typed beside what the records said, and your reason.
              </p>
              <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
                <label className="flex flex-col gap-1">
                  Trades
                  <input type="number" min={0} value={override.num_trades} onChange={(e) => setOverride({ ...override, num_trades: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
                </label>
                <label className="flex flex-col gap-1">
                  Calendar days running
                  <input type="number" min={0} value={override.calendar_days_running} onChange={(e) => setOverride({ ...override, calendar_days_running: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
                </label>
                <label className="flex flex-col gap-1">
                  Clean Shadow Mode streak (days)
                  <input type="number" min={0} value={override.clean_shadow_mode_streak_days} onChange={(e) => setOverride({ ...override, clean_shadow_mode_streak_days: Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
                </label>
                <label className="flex flex-col gap-1">
                  Paper win rate (0 to 1)
                  <input type="number" step="0.01" min={0} max={1} value={override.live_win_rate ?? ''} onChange={(e) => setOverride({ ...override, live_win_rate: e.target.value === '' ? null : Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
                </label>
                <label className="flex flex-col gap-1">
                  Backtest win rate (0 to 1)
                  <input type="number" step="0.01" min={0} max={1} value={override.backtest_win_rate ?? ''} onChange={(e) => setOverride({ ...override, backtest_win_rate: e.target.value === '' ? null : Number(e.target.value) })} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
                </label>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" className="button-secondary" onClick={() => void checkNumbers()}>
                  Check these numbers
                </button>
                <button type="button" className="button-danger" onClick={() => setConfirming('override')}>
                  Sign off with these numbers…
                </button>
              </div>
              {message && <p className="mt-2 text-xs text-rose-300" role="alert">{message}</p>}
              {whatIf && (
                <p className="mt-2 text-xs" role="status">
                  {whatIf.eligible ? 'These numbers meet the gate.' : `These numbers do not meet the gate: ${whatIf.reasons.join('; ')}.`}
                </p>
              )}
            </details>
          )}
        </>
      )}

      {confirming === 'derived' && (
        <ConfirmDialog
          title="Sign off as live eligible"
          description="This lets the strategy be enrolled in live trading. The gate was judged on the evidence above. You can still switch autonomy on or off separately."
          confirmLabel="Sign off"
          busyLabel="Signing off…"
          onConfirm={() => signOff(null)}
          onClose={() => setConfirming(null)}
        />
      )}
      {confirming === 'override' && (
        <ConfirmDialog
          title="Sign off with your own numbers"
          description={`You are replacing the recorded evidence with: ${override.num_trades} trades, ${override.calendar_days_running} days, a ${override.clean_shadow_mode_streak_days}-day streak, win rates ${pct(override.live_win_rate ?? null)} and ${pct(override.backtest_win_rate ?? null)}. The gate still has to pass, and this is recorded with your reason.`}
          confirmLabel="Sign off with an override"
          busyLabel="Signing off…"
          tone="danger"
          phrase="OVERRIDE"
          reason={{ label: 'Why are the recorded numbers not enough?', minLength: 10, hint: 'At least 10 characters. Kept in the audit log beside the recorded figures.' }}
          onConfirm={(reason) => signOff(reason)}
          onClose={() => setConfirming(null)}
        />
      )}
    </div>
  )
}
