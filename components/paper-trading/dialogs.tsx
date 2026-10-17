'use client'

import { useState } from 'react'
import { type PaperSubscription, activatePaperSubscription, deactivatePaperSubscription } from '@/lib/api'
import { subscriptionName } from '@/lib/paper-trading'
import { Field, messageOf } from './dialog-parts'
import { Modal } from './modal'

// The desk's dialogs: adding, closing and the settings live in their own files (each has an
// estimate or a preview of its own); the page imports them all from here.
export { CloseDialog } from './close-dialog'
export { EnrolDialog } from './enrol-dialog'
export { SettingsDialog } from './settings-dialog'

// ---- Deactivate ----

export function DeactivateDialog({ sub, onClose, onDone }: { sub: PaperSubscription; onClose: () => void; onDone: () => void }) {
  const holding = sub.open_legs > 0
  const [choice, setChoice] = useState<'close' | 'leave_unmanaged'>('close')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (reason.trim().length < 3) return setError('Give a reason of at least 3 characters.')
    setBusy(true)
    setError(null)
    try {
      await deactivatePaperSubscription(sub.id, { reason: reason.trim(), ...(holding ? { on_open_position: choice } : {}) })
      onDone()
    } catch (err) {
      setError(messageOf(err, 'Could not switch it off'))
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Switch off ${subscriptionName(sub)}`}
      description={holding ? `It holds ${sub.open_legs} open ${sub.open_legs === 1 ? 'leg' : 'legs'}. Choose what happens to ${sub.open_legs === 1 ? 'it' : 'them'}.` : 'It holds no position, so nothing else changes.'}
      onClose={onClose}
      busy={busy}
    >
      <form className="pt-form" onSubmit={submit}>
        {holding && (
          <div className="pt-field" role="radiogroup" aria-label="Open position">
            <label className="pt-radio" data-on={choice === 'close'}>
              <input type="radio" name="on_open" checked={choice === 'close'} onChange={() => setChoice('close')} />
              <div><strong>Close it now</strong><span>Sold legs are bought back first, then the rest, at real depth prices.</span></div>
            </label>
            <label className="pt-radio" data-on={choice === 'leave_unmanaged'}>
              <input type="radio" name="on_open" checked={choice === 'leave_unmanaged'} onChange={() => setChoice('leave_unmanaged')} />
              <div><strong>Leave it open, unmanaged</strong><span>Nothing watches its stop-loss until you close it or switch the subscription back on.</span></div>
            </label>
          </div>
        )}
        <Field label="Reason" hint="Recorded in the audit log.">
          {(id) => <input id={id} className="pt-input" value={reason} onChange={(e) => setReason(e.target.value)} autoComplete="off" />}
        </Field>
        {error && <div className="pt-error" role="alert">{error}</div>}
        <div className="pt-modal-foot">
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>Keep running</button>
          <button type="submit" className="button-danger" disabled={busy}>{busy ? 'Switching off…' : 'Switch off'}</button>
        </div>
      </form>
    </Modal>
  )
}

/** Switch a subscription back on (no dialog: it has no choices). */
export async function activate(sub: PaperSubscription): Promise<string | null> {
  try {
    await activatePaperSubscription(sub.id)
    return null
  } catch (err) {
    return messageOf(err, 'Could not switch it on')
  }
}
