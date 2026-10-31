'use client'

import { useState } from 'react'
import { ErrorNotice } from '@/components/ui/error-notice'
import { ApiError, KNOWN_BROKERS, getGoLiveEvidence, listShadowModeRuns, runShadowModeCheck } from '@/lib/api'
import { usePolling } from '@/lib/use-polling'

const CONFIDENCE_TEXT = {
  real_sandbox_dry_run: "Sent to the broker's own sandbox, which answered",
  local_payload_only: 'The order was built and checked locally; this broker has no sandbox, so nothing was sent',
} as const

/**
 * A strategy's Shadow Mode dry runs: what was tried each day, whether it worked, and how long the
 * clean streak the Go-Live gate asks for has run. A run that failed is shown with its reason: a
 * day on which the check broke is not a clean day. "Run dry check" makes one now.
 */
export function ShadowModePanel({
  strategyId,
  defaultSymbol,
  canRun,
}: {
  strategyId: string
  defaultSymbol: string
  canRun: boolean
}) {
  const { data: runs, error, refresh } = usePolling(() => listShadowModeRuns(strategyId), { intervalMs: 60_000 })
  const { data: evidence, refresh: refreshEvidence } = usePolling(() => getGoLiveEvidence(strategyId), { intervalMs: 60_000 })
  const [broker, setBroker] = useState<string>(KNOWN_BROKERS[0])
  const [symbol, setSymbol] = useState(defaultSymbol)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  async function runCheck() {
    setBusy(true)
    setMessage(null)
    try {
      const run = await runShadowModeCheck({ broker, symbol: symbol.trim().toUpperCase(), side: 'buy', quantity: 1, strategy_id: strategyId })
      setMessage(
        run.outcome === 'ok'
          ? { ok: true, text: `${CONFIDENCE_TEXT[run.confidence]}.` }
          : { ok: false, text: `The check failed: ${run.failure_reason ?? 'no reason was given'}. It is recorded and ends the clean streak.` },
      )
      await Promise.all([refresh(), refreshEvidence()])
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : 'The check could not be run' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="review-content" aria-label="Shadow Mode">
      <p className="eyebrow">SHADOW MODE · DRY-RUN ORDER CHECKS</p>
      <h2>Clean streak: {evidence ? `${evidence.inputs.clean_shadow_mode_streak_days} of ${evidence.thresholds.min_clean_shadow_days} trading days` : '…'}</h2>
      <p className="mt-2 text-xs text-muted-foreground">
        Each trading day a connected broker is asked to check a one-share order for this strategy. A day counts when a check worked and none failed;
        a trading day with no check ends the streak. With no broker connected nothing is tried, so nothing counts.
      </p>

      {canRun && (
        <div className="mt-4 flex flex-wrap items-end gap-2 rounded-xl border border-white/10 p-3 text-xs">
          <label className="flex flex-col gap-1">
            Broker
            <select value={broker} onChange={(e) => setBroker(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1 capitalize">
              {KNOWN_BROKERS.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            Symbol
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)} className="rounded border border-white/10 bg-black/20 px-2 py-1" />
          </label>
          <button type="button" className="button-primary" disabled={busy || !symbol.trim()} onClick={() => void runCheck()}>
            {busy ? 'Checking…' : 'Run dry check'}
          </button>
          <span className="text-muted-foreground">Buys one share in the sandbox, or builds the order without sending it where there is none.</span>
        </div>
      )}
      {message && <p className={`mt-2 text-xs ${message.ok ? 'text-emerald-200' : 'text-rose-300'}`} role={message.ok ? 'status' : 'alert'}>{message.text}</p>}
      {error && <ErrorNotice className="mt-3" message={`Could not load the dry runs: ${error}`} onRetry={() => void refresh()} />}

      <table className="mt-4 w-full text-left text-xs">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-1">When</th>
            <th>Broker</th>
            <th>Order</th>
            <th>How it was checked</th>
            <th>Result</th>
          </tr>
        </thead>
        <tbody>
          {(runs ?? []).map((run) => (
            <tr key={run.id} className="border-t border-white/5 align-top">
              <td className="py-1 font-mono">{new Date(run.created_at).toLocaleString()}</td>
              <td className="capitalize">{run.broker_name}</td>
              <td>{run.side} {run.quantity} {run.symbol}</td>
              <td>{run.confidence === 'real_sandbox_dry_run' ? 'Broker sandbox' : 'Built locally only'}</td>
              <td className={run.outcome === 'ok' ? 'text-emerald-200' : 'text-rose-300'}>
                {run.outcome === 'ok' ? 'Worked' : `Failed: ${run.failure_reason ?? 'no reason given'}`}
              </td>
            </tr>
          ))}
          {runs && runs.length === 0 && (
            <tr><td colSpan={5} className="py-3 text-muted-foreground">No dry run has been made for this strategy yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
