'use client'

import { useState } from 'react'
import { Field } from '@/components/ui/field'
import { Modal } from '@/components/ui/modal'
import { confirmationMatches } from '@/lib/paper-trading'

/**
 * A confirmation for something that cannot be quietly undone. Optionally asks for a reason (kept by
 * the caller, e.g. in the audit log) and for a phrase to be typed, so it cannot be done on autopilot.
 * `onConfirm` does the work and throws to report a failure, which stays on screen for another try;
 * the dialog closes itself when it returns.
 */
export function ConfirmDialog({
  title,
  description,
  confirmLabel,
  busyLabel,
  tone = 'default',
  phrase,
  reason,
  onConfirm,
  onClose,
  children,
}: {
  title: string
  description?: string
  confirmLabel: string
  busyLabel?: string
  tone?: 'default' | 'danger'
  /** Typed (case-insensitively) to enable the button. */
  phrase?: string
  /** Ask for a reason of at least `minLength` characters. */
  reason?: { label: string; hint?: string; minLength?: number }
  onConfirm: (reason: string) => Promise<void>
  onClose: () => void
  children?: React.ReactNode
}) {
  const [typed, setTyped] = useState('')
  const [why, setWhy] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const minLength = reason?.minLength ?? 3
  const missing = (() => {
    if (reason && why.trim().length < minLength) return `Give a reason of at least ${minLength} characters.`
    if (phrase && !confirmationMatches(typed, phrase)) return `Type ${phrase} to confirm.`
    return null
  })()

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (missing) return setError(missing)
    setBusy(true)
    setError(null)
    try {
      await onConfirm(why.trim())
      onClose()
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : 'That did not go through. Try again.')
      setBusy(false)
    }
  }

  return (
    <Modal title={title} description={description} onClose={onClose} busy={busy}>
      <form className="ui-form" onSubmit={submit}>
        {children}
        {reason && (
          <Field label={reason.label} hint={reason.hint}>
            {(id) => (
              <textarea
                id={id}
                className="ui-input"
                rows={3}
                value={why}
                onChange={(event) => setWhy(event.target.value)}
                disabled={busy}
              />
            )}
          </Field>
        )}
        {phrase && (
          <Field label={`Type ${phrase} to confirm`}>
            {(id) => (
              <input
                id={id}
                className="ui-input ui-confirm"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                disabled={busy}
              />
            )}
          </Field>
        )}
        {error && <div className="ui-error" role="alert">{error}</div>}
        <div className="ui-modal-foot">
          {missing && !error && <span className="ui-foot-hint">{missing}</span>}
          <button type="button" className="button-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="submit"
            className={tone === 'danger' ? 'button-danger' : 'button-primary'}
            disabled={busy || missing !== null}
          >
            {busy ? (busyLabel ?? 'Working…') : confirmLabel}
          </button>
        </div>
      </form>
    </Modal>
  )
}
