'use client'

import { useId, useState } from 'react'
import { Check, Pause, Play } from 'lucide-react'
import {
  ApiError,
  type NotificationChannelStatus,
  type WriteNotificationChannelInput,
  updateNotificationChannel,
  writeNotificationChannel,
} from '@/lib/api'
import { parseSenderIds } from '@/lib/notification-senders'

type Channel = 'telegram' | 'discord' | 'slack'

// What proves a message came from the channel itself, per channel (write-only: the server never returns it).
const SECRET: Record<Channel, { field: keyof WriteNotificationChannelInput; label: string; help: string }> = {
  telegram: {
    field: 'webhook_secret_token',
    label: 'Webhook secret token',
    help: 'The secret_token you gave Telegram when you set the bot’s webhook.',
  },
  discord: {
    field: 'public_key',
    label: 'Application public key',
    help: 'From your Discord application’s General Information page.',
  },
  slack: {
    field: 'signing_secret',
    label: 'Signing secret',
    help: 'From your Slack app’s Basic Information page.',
  },
}

/**
 * A configured channel's other half: pausing alerts without losing the setup, and who may send commands
 * back through it. Replies are refused unless the sender is on the list and the request proves it came
 * from the channel (the secret below), so both are shown, and an empty list is said to mean nobody.
 */
export function InboundSettings({
  channel,
  status,
  canEdit,
  onChanged,
}: {
  channel: Channel
  status: NotificationChannelStatus
  canEdit: boolean
  onChanged: () => void
}) {
  const sendersId = useId()
  const secretId = useId()
  const [senders, setSenders] = useState(status.allowed_sender_ids.join(', '))
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)

  async function run(work: () => Promise<void>, done: string) {
    setBusy(true)
    setMessage(null)
    try {
      await work()
      setMessage({ ok: true, text: done })
      onChanged()
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : 'Could not save that. Try again.' })
    } finally {
      setBusy(false)
    }
  }

  const ids = parseSenderIds(senders)
  const sendersChanged = ids.join(',') !== status.allowed_sender_ids.join(',')
  const secretInfo = SECRET[channel]

  return (
    <div className="set-inbound">
      <div className="set-inbound-row">
        <span>{status.enabled ? 'Alerts are being sent to this channel.' : 'Alerts are paused for this channel.'}</span>
        {canEdit && (
          <button
            type="button"
            className="set-btn set-btn-secondary"
            disabled={busy}
            onClick={() =>
              run(() => updateNotificationChannel(channel, { enabled: !status.enabled }), status.enabled ? 'Alerts paused.' : 'Alerts resumed.')
            }
          >
            {status.enabled ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
            {status.enabled ? 'Pause alerts' : 'Resume alerts'}
          </button>
        )}
      </div>

      <p className="set-inbound-note">
        Replies: someone can start a run or ask a question from this channel only if their ID is listed here.{' '}
        {status.allowed_sender_ids.length === 0 ? 'The list is empty, so nobody can.' : `${status.allowed_sender_ids.length} allowed.`}
      </p>
      <label htmlFor={sendersId}>Allowed sender IDs</label>
      <div className="set-wizard-inline">
        <input
          id={sendersId}
          value={senders}
          onChange={(event) => setSenders(event.target.value)}
          placeholder="IDs separated by commas"
          disabled={!canEdit || busy}
          autoComplete="off"
        />
        {canEdit && (
          <button
            type="button"
            className="set-btn set-btn-secondary"
            disabled={busy || !sendersChanged}
            onClick={() => run(() => updateNotificationChannel(channel, { allowed_sender_ids: ids }), 'Allowed senders saved.')}
          >
            <Check className="size-3.5" /> Save senders
          </button>
        )}
      </div>

      {canEdit && (
        <>
          <label htmlFor={secretId}>{secretInfo.label}</label>
          <div className="set-wizard-inline">
            <input
              id={secretId}
              type="password"
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
              placeholder="Leave blank to keep the saved one"
              autoComplete="off"
              disabled={busy}
            />
            <button
              type="button"
              className="set-btn set-btn-secondary"
              disabled={busy || !secret}
              onClick={() =>
                run(async () => {
                  await writeNotificationChannel(channel, {
                    enabled: status.enabled,
                    alert_levels: status.alert_levels,
                    [secretInfo.field]: secret,
                  })
                  setSecret('')
                }, `${secretInfo.label} saved.`)
              }
            >
              <Check className="size-3.5" /> Save secret
            </button>
          </div>
          <p className="set-inbound-note">{secretInfo.help} It is never shown again after saving.</p>
        </>
      )}
      {message && (
        <p className={message.ok ? 'set-inbound-ok' : 'set-inbound-error'} role={message.ok ? 'status' : 'alert'}>
          {message.text}
        </p>
      )}
    </div>
  )
}
