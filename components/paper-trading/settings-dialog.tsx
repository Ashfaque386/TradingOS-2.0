'use client'

import { useMemo, useState } from 'react'
import { type PaperSettingsRequest, type PaperSubscription, updatePaperSettings } from '@/lib/api'
import {
  type SettingField,
  type SettingsForm,
  editableSettings,
  hasTraded,
  planSettingsChange,
  settingsForm,
  subscriptionName,
} from '@/lib/paper-trading'
import { Field, Segmented, messageOf } from './dialog-parts'
import { Modal } from './modal'

const NUMBER_FIELD: Record<Exclude<SettingField, 'builtin_strategy'>, { label: string; hint?: string; step: string; min: number }> = {
  stop_loss_pct: { label: 'Stop-loss (%)', hint: 'Percent move against the position that closes it. For an option, percent of what it can lose at most.', step: 'any', min: 0.1 },
  position_size_pct: { label: 'Size per entry (%)', hint: 'Share of capital one entry may use.', step: 'any', min: 0.1 },
  lots: { label: 'Lots', hint: 'Whole lots per entry.', step: '1', min: 1 },
  spread_width: { label: 'Hedge distance', hint: 'Strikes between the sold and the bought option.', step: '1', min: 1 },
  sma_window: { label: 'Average length', hint: 'Price points in the moving average.', step: '1', min: 1 },
  initial_capital: { label: 'Capital (₹)', step: 'any', min: 1 },
}

/** Change what may change after a subscription is added. What it trades (symbol, kind, strategy,
 * option structure) does not: switch it off and add a new one for that. Shows exactly what will
 * change and when it takes effect; the backend audits the before and after with the reason. */
export function SettingsDialog({
  sub,
  onClose,
  onDone,
}: {
  sub: PaperSubscription
  onClose: () => void
  onDone: (summary: string) => void
}) {
  const [form, setForm] = useState<SettingsForm>(() => settingsForm(sub))
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fields = editableSettings(sub)
  const capitalLocked = hasTraded(sub)
  const { changes, errors } = useMemo(() => planSettingsChange(sub, form), [sub, form])

  const set = <K extends keyof SettingsForm>(field: K, value: SettingsForm[K]) =>
    setForm((current) => ({ ...current, [field]: value }))

  const missing =
    Object.keys(errors).length > 0
      ? 'Fix the highlighted values.'
      : changes.length === 0
        ? 'Change at least one value.'
        : reason.trim().length < 3
          ? 'Give a reason of at least 3 characters.'
          : null

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (missing) return setError(missing)
    const body: PaperSettingsRequest = { reason: reason.trim() }
    for (const change of changes) {
      ;(body as Record<string, unknown>)[change.field] = change.value
    }
    setBusy(true)
    setError(null)
    try {
      await updatePaperSettings(sub.id, body)
      onDone(`${changes.map((c) => `${c.label.toLowerCase()} ${c.before} → ${c.after}`).join(', ')}`)
    } catch (err) {
      setError(messageOf(err, 'Could not save the settings'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Settings for ${subscriptionName(sub)}`}
      description="Change how it trades from now on. What it trades stays fixed: to change that, switch it off and add a new subscription."
      onClose={onClose}
      busy={busy}
    >
      <form className="pt-form" onSubmit={submit}>
        {fields.map((field) => {
          if (field === 'builtin_strategy') {
            return (
              <Segmented
                key={field}
                label="Signal"
                value={form.builtin_strategy}
                options={[
                  { value: 'sma_crossover', label: 'Moving average' },
                  { value: 'always_long', label: 'Always long' },
                ]}
                onChange={(value) => set('builtin_strategy', value)}
              />
            )
          }
          if (field === 'sma_window' && form.builtin_strategy === 'always_long') return null
          const spec = NUMBER_FIELD[field]
          if (field === 'initial_capital' && capitalLocked) {
            return (
              <Field key={field} label={spec.label} hint="Fixed once it has traded: its returns and drawdown are measured against it.">
                {(id) => <input id={id} className="pt-input" value={form.initial_capital} readOnly aria-readonly="true" />}
              </Field>
            )
          }
          return (
            <Field key={field} label={spec.label} hint={spec.hint} error={errors[field]}>
              {(id) => (
                <input
                  id={id}
                  className="pt-input"
                  type="number"
                  min={spec.min}
                  step={spec.step}
                  value={form[field]}
                  onChange={(e) => set(field, e.target.value)}
                  aria-invalid={errors[field] ? true : undefined}
                />
              )}
            </Field>
          )
        })}

        {changes.length > 0 && (
          <section className="pt-changes" aria-label="What will change">
            <h3>What will change</h3>
            <ul>
              {changes.map((change) => (
                <li key={change.field}>
                  <div>
                    <b>{change.label}</b>
                    <span className="pt-num">{change.before} → {change.after}</span>
                  </div>
                  <small>{change.effect}</small>
                </li>
              ))}
            </ul>
          </section>
        )}

        <Field label="Reason" hint="Recorded in the audit log with the old and new values.">
          {(id) => <input id={id} className="pt-input" value={reason} onChange={(e) => setReason(e.target.value)} autoComplete="off" />}
        </Field>

        {error && <div className="pt-error" role="alert">{error}</div>}
        <div className="pt-modal-foot">
          {missing && <small className="pt-foot-hint">{missing}</small>}
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy || missing !== null}>{busy ? 'Saving…' : 'Save settings'}</button>
        </div>
      </form>
    </Modal>
  )
}
