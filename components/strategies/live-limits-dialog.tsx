'use client'

import { useState } from 'react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Field } from '@/components/ui/field'
import { Modal } from '@/components/ui/modal'
import { type LiveTradingSubscriptionDto, updateLiveSubscription } from '@/lib/api'
import {
  type Held,
  LIMITS,
  LIMIT_FIELDS,
  type LimitField,
  type LimitsForm,
  changedLimits,
  describeLimitChange,
  limitProblems,
  limitsForm,
  limitsRequest,
  switchOffBlocked,
} from '@/lib/live-limits'
import { messageOf } from '@/lib/errors'

/** Makes a live subscription's limits stricter. Each box shows what it is now and which way it may move;
 * a limit may only get stricter here, and an order cap below what the strategy holds is refused, because it
 * would block the order that closes the position. What will change is listed before it is saved, and the
 * reason is recorded in the audit log with the old and new values. */
export function LiveLimitsDialog({
  sub,
  held,
  onClose,
  onDone,
}: {
  sub: LiveTradingSubscriptionDto
  held: Held
  onClose: () => void
  onDone: (updated: LiveTradingSubscriptionDto, summary: string) => void
}) {
  const [form, setForm] = useState<LimitsForm>(() => limitsForm(sub))
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const problems = limitProblems(sub, form, held)
  const changed = changedLimits(sub, form)
  const blocker = Object.keys(problems).length > 0 ? 'Fix the highlighted values.' : changed.length === 0 ? 'Change at least one limit.' : reason.trim().length < 3 ? 'Give a reason of at least 3 characters.' : null

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (blocker) return setError(blocker)
    setBusy(true)
    setError(null)
    try {
      const result = await updateLiveSubscription(sub.id, limitsRequest(sub, form, reason))
      onDone(result.subscription, result.changes.map((c) => describeLimitChange(c.field as LimitField, c.before, c.after)).join('; '))
    } catch (err) {
      setError(messageOf(err, 'The limits could not be changed'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Tighten the limits on ${sub.symbol}`}
      description="These stand between the strategy and the broker. They can be made stricter here; making one looser means enrolling the strategy again."
      onClose={onClose}
      busy={busy}
      wide
    >
      <form className="ui-form" onSubmit={submit} noValidate>
        {held && (
          <p className="ui-foot-hint" style={{ maxWidth: 'none' }}>
            The strategy holds {Math.abs(held.quantity)} shares of {sub.symbol}, so the largest order cannot go below what they cost.
          </p>
        )}
        <div className="ui-row">
          {LIMIT_FIELDS.map((field) => (
            <Field key={field} label={LIMITS[field].label} hint={`${LIMITS[field].hint} Now ${sub[field]}; can only be ${LIMITS[field].direction === 'lower' ? 'lowered' : 'raised'}.`} error={problems[field]}>
              {(id) => <input id={id} className="ui-input" inputMode="decimal" value={form[field]} onChange={(e) => setForm((current) => ({ ...current, [field]: e.target.value }))} />}
            </Field>
          ))}
        </div>
        {changed.length > 0 && !Object.keys(problems).length && (
          <p className="bt-result" role="status">
            Will change: {changed.map((field) => describeLimitChange(field, sub[field], Number(form[field]))).join('; ')}.
          </p>
        )}
        <Field label="Reason" hint="Recorded in the audit log with the old and new values.">
          {(id) => <textarea id={id} className="ui-input" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />}
        </Field>
        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          {blocker && !error && <span className="ui-foot-hint">{blocker}</span>}
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="submit" className="button-primary" disabled={busy || blocker !== null}>{busy ? 'Saving…' : 'Tighten the limits'}</button>
        </div>
      </form>
    </Modal>
  )
}

/** Switches a live subscription off for good. It says what that means before it is typed through: the
 * strategy stops trading this symbol, and it cannot be switched back on here. A strategy that holds
 * shares cannot be switched off, so the button is not offered then. */
export function SwitchOffDialog({
  sub,
  held,
  onClose,
  onDone,
}: {
  sub: LiveTradingSubscriptionDto
  held: Held
  onClose: () => void
  onDone: (updated: LiveTradingSubscriptionDto) => void
}) {
  const blocked = switchOffBlocked(sub, held)
  if (blocked) {
    return (
      <Modal title={`${sub.symbol} cannot be switched off`} description={blocked} onClose={onClose}>
        <div className="ui-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose}>Close</button>
        </div>
      </Modal>
    )
  }
  return (
    <ConfirmDialog
      title={`Switch off the ${sub.symbol} subscription`}
      description="The strategy stops trading this symbol, and autonomous trading for it ends. This cannot be undone here: to trade it again, enrol the strategy again. Type the symbol to confirm."
      confirmLabel="Switch it off"
      busyLabel="Switching off…"
      tone="danger"
      phrase={sub.symbol}
      reason={{ label: 'Reason', hint: 'Recorded in the audit log.', minLength: 3 }}
      onConfirm={async (reason) => {
        try {
          onDone((await updateLiveSubscription(sub.id, { deactivate: true, reason })).subscription)
        } catch (err) {
          throw new Error(messageOf(err, 'It could not be switched off'))
        }
      }}
      onClose={onClose}
    />
  )
}
