'use client'

import { useState } from 'react'
import { Field, Segmented } from '@/components/ui/field'
import { Modal } from '@/components/ui/modal'
import { type BacktestRun, type Strategy, createBacktestFromLake } from '@/lib/api'
import { type BacktestForm, STRATEGY_LABEL, emptyForm, formProblems, lakeRequest, runnableVersionId } from '@/lib/backtests'
import { messageOf } from '@/lib/errors'

/** Starts a backtest on the daily prices the data lake already holds: a strategy, a share and two dates.
 * No prices are typed or posted. A window the lake cannot cover is recorded as a refused run with its
 * reason (the run page shows it); a symbol it holds too little of is refused here, with the count. */
export function NewBacktestDialog({
  strategies,
  initial,
  onClose,
  onDone,
}: {
  strategies: Strategy[]
  initial?: Partial<BacktestForm>
  onClose: () => void
  onDone: (run: BacktestRun) => void
}) {
  const [form, setForm] = useState<BacktestForm>(() => ({ ...emptyForm(new Date()), ...initial }))
  const [tried, setTried] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const problems = formProblems(form, new Date())
  const shown = (field: keyof BacktestForm) => (tried ? problems[field] : undefined)
  const set = <K extends keyof BacktestForm>(field: K, value: BacktestForm[K]) => setForm((current) => ({ ...current, [field]: value }))

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
      onDone(await createBacktestFromLake(lakeRequest(form, versionId)))
    } catch (err) {
      setError(messageOf(err, 'The backtest could not be started'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title="New backtest"
      description="Runs the strategy over the daily prices the data lake holds, with real Indian trading costs. Only prices with a recorded real source are used."
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
        <Field label="Share" hint="The symbol as the data lake holds it, for example RELIANCE." error={shown('symbol')}>
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
        <Segmented
          label="Signal"
          value={form.strategy}
          options={[
            { value: 'sma_crossover', label: STRATEGY_LABEL.sma_crossover },
            { value: 'always_long', label: STRATEGY_LABEL.always_long },
          ]}
          onChange={(value) => set('strategy', value)}
        />
        {form.strategy === 'sma_crossover' && (
          <Field label="Average length (days)" hint="Buys when the price closes above its average, sells when it closes below." error={shown('smaWindow')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.smaWindow} onChange={(e) => set('smaWindow', e.target.value)} />}
          </Field>
        )}
        <div className="ui-row">
          <Segmented
            label="Trading"
            value={form.delivery ? 'delivery' : 'intraday'}
            options={[
              { value: 'delivery', label: 'Delivery' },
              { value: 'intraday', label: 'Intraday' },
            ]}
            onChange={(value) => set('delivery', value === 'delivery')}
          />
          <Field label="Capital (₹)" error={shown('capital')}>
            {(id) => <input id={id} className="ui-input" inputMode="decimal" value={form.capital} onChange={(e) => set('capital', e.target.value)} />}
          </Field>
        </div>
        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy}>{busy ? 'Running…' : 'Run backtest'}</button>
        </div>
      </form>
    </Modal>
  )
}
