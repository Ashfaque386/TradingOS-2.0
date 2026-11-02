'use client'

import { useState } from 'react'
import { Field } from '@/components/ui/field'
import { Modal } from '@/components/ui/modal'
import { type BacktestRun, runWalkForward } from '@/lib/api'
import { WALK_FORWARD_DEFAULTS, type WalkForwardForm, approximateBars, describeConfig, walkForwardProblems, walkForwardRequest } from '@/lib/backtests'
import { messageOf } from '@/lib/errors'

/** Tests whether a run holds up out of sample: the history is cut into windows, each trained on the
 * bars before it and judged on the bars after. It reads the run's own prices from the data lake and
 * uses the settings the run was made with. */
export function WalkForwardDialog({
  run,
  onClose,
  onDone,
}: {
  run: BacktestRun
  onClose: () => void
  onDone: (run: BacktestRun) => void
}) {
  const [form, setForm] = useState<WalkForwardForm>(WALK_FORWARD_DEFAULTS)
  const [tried, setTried] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const problems = walkForwardProblems(form, run)
  const shown = (field: keyof WalkForwardForm) => (tried ? problems[field] : undefined)
  const set = (field: keyof WalkForwardForm, value: string) => setForm((current) => ({ ...current, [field]: value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setTried(true)
    if (Object.keys(problems).length > 0) return
    setBusy(true)
    setError(null)
    try {
      onDone(await runWalkForward(run.id, walkForwardRequest(form)))
    } catch (err) {
      setError(messageOf(err, 'The walk-forward could not be run'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Walk-forward on ${run.symbol}`}
      description={`Reads this run’s prices (${run.start_date} to ${run.end_date}, about ${approximateBars(run)} bars) from the data lake and tests each window on bars it was not trained on.`}
      onClose={onClose}
      busy={busy}
    >
      <form className="ui-form" onSubmit={submit} noValidate>
        <p className="bt-config">{describeConfig(run.signal_config)}</p>
        <div className="ui-row">
          <Field label="Training window (bars)" error={shown('train')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.train} onChange={(e) => set('train', e.target.value)} />}
          </Field>
          <Field label="Test window (bars)" error={shown('test')}>
            {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.test} onChange={(e) => set('test', e.target.value)} />}
          </Field>
        </div>
        <Field label="Step (bars)" hint="How far each window moves on. Empty steps by the test window." error={shown('step')}>
          {(id) => <input id={id} className="ui-input" inputMode="numeric" value={form.step} onChange={(e) => set('step', e.target.value)} />}
        </Field>
        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy}>{busy ? 'Running…' : 'Run walk-forward'}</button>
        </div>
      </form>
    </Modal>
  )
}
