'use client'

import { useState } from 'react'
import { Field } from '@/components/ui/field'
import { Modal } from '@/components/ui/modal'
import { type OptimizationResult, type StrategySummary, optimizeBacktest } from '@/lib/api'
import {
  OBJECTIVE_LABEL,
  type OptimizeForm,
  bestWindow,
  describeOptimization,
  emptyOptimizeForm,
  optimizeProblems,
  optimizeRequest,
  runnableVersionId,
} from '@/lib/backtests'
import { messageOf } from '@/lib/errors'

/** Searches for the moving-average length that scored best on a share's history in the data lake.
 * The best of many tries on one history is the one that fitted it best, so the result says so and offers
 * to run the length as an ordinary backtest (which a walk-forward can then check). */
export function OptimizeDialog({
  strategies,
  onClose,
  onUse,
}: {
  strategies: StrategySummary[]
  onClose: () => void
  /** Open a backtest with this share, window and average length already filled in. */
  onUse: (form: Partial<OptimizeForm> & { smaWindow: number }) => void
}) {
  const [form, setForm] = useState<OptimizeForm>(() => emptyOptimizeForm(new Date()))
  const [tried, setTried] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ found: OptimizationResult; objective: OptimizeForm['objective']; form: OptimizeForm } | null>(null)
  const problems = optimizeProblems(form, new Date())
  const shown = (field: keyof OptimizeForm) => (tried ? problems[field] : undefined)
  const set = <K extends keyof OptimizeForm>(field: K, value: OptimizeForm[K]) => setForm((current) => ({ ...current, [field]: value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setTried(true)
    if (Object.keys(problems).length > 0) return
    const strategy = strategies.find((s) => s.id === form.strategyId)
    const versionId = strategy ? runnableVersionId(strategy) : null
    if (!versionId) return setError('That strategy has no version to run.')
    setBusy(true)
    setError(null)
    try {
      setResult({ found: await optimizeBacktest(optimizeRequest(form, versionId)), objective: form.objective, form })
    } catch (err) {
      setError(messageOf(err, 'The search could not be run'))
    } finally {
      setBusy(false)
    }
  }

  const window = result ? bestWindow(result.found) : null

  return (
    <Modal
      title="Find the best average length"
      description="Tries moving-average lengths on a share’s daily prices from the data lake and scores each one. Nothing is saved as a backtest until you run the length you choose."
      onClose={onClose}
      busy={busy}
      wide
    >
      <form className="ui-form" onSubmit={submit} noValidate>
        <Field label="Strategy" error={shown('strategyId')}>
          {(id) => (
            <select id={id} className="ui-input" value={form.strategyId} onChange={(e) => set('strategyId', e.target.value)}>
              <option value="">Pick a strategy…</option>
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Share" error={shown('symbol')}>
          {(id) => <input id={id} className="ui-input" value={form.symbol} onChange={(e) => set('symbol', e.target.value.toUpperCase())} autoComplete="off" />}
        </Field>
        <div className="ui-row">
          <Field label="First day" error={shown('start')}>
            {(id) => <input id={id} type="date" className="ui-input" value={form.start} onChange={(e) => set('start', e.target.value)} />}
          </Field>
          <Field label="Last day" error={shown('end')}>
            {(id) => <input id={id} type="date" className="ui-input" value={form.end} onChange={(e) => set('end', e.target.value)} />}
          </Field>
        </div>
        <div className="ui-row">
          <Field label="Shortest average (days)" error={shown('windowMin')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.windowMin} onChange={(e) => set('windowMin', e.target.value)} />}
          </Field>
          <Field label="Longest average (days)" error={shown('windowMax')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.windowMax} onChange={(e) => set('windowMax', e.target.value)} />}
          </Field>
        </div>
        <div className="ui-row">
          <Field label="Score by">
            {(id) => (
              <select id={id} className="ui-input" value={form.objective} onChange={(e) => set('objective', e.target.value as OptimizeForm['objective'])}>
                {Object.entries(OBJECTIVE_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Tries (1 to 200)" error={shown('trials')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.trials} onChange={(e) => set('trials', e.target.value)} />}
          </Field>
        </div>
        {error && <div className="ui-error" role="alert">{error}</div>}
        {result && (
          <div className="bt-result" role="status">
            <p>{describeOptimization(result.found, result.objective)}</p>
            {result.found.param_importance && Object.keys(result.found.param_importance).length > 0 && (
              <small>
                Importance of each setting: {Object.entries(result.found.param_importance).map(([name, value]) => `${name} ${(value * 100).toFixed(0)}%`).join(', ')}.
              </small>
            )}
          </div>
        )}
        <div className="ui-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Close</button>
          {result && window !== null && (
            <button
              type="button"
              className="button-secondary"
              onClick={() => onUse({ strategyId: result.form.strategyId, symbol: result.form.symbol, start: result.form.start, end: result.form.end, smaWindow: window })}
            >
              Run a backtest with {window} days
            </button>
          )}
          <button type="submit" className="button-primary" disabled={busy}>{busy ? 'Searching…' : result ? 'Search again' : 'Search'}</button>
        </div>
      </form>
    </Modal>
  )
}
