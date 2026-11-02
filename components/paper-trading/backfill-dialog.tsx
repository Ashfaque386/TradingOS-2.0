'use client'

import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { type PaperSubscription, backfillPaperHistory } from '@/lib/api'
import { describeBackfill, subscriptionName } from '@/lib/paper-trading'

/** Fetches a subscription's daily price history from the connected broker. It is a request to a
 * broker, so it says what it will replace before it starts and what it did when it ends; a failure
 * stays on screen with the broker's reason for another try. */
export function BackfillDialog({
  sub,
  onClose,
  onDone,
}: {
  sub: PaperSubscription
  onClose: () => void
  onDone: (message: string) => void
}) {
  return (
    <ConfirmDialog
      title={`Fetch price history for ${subscriptionName(sub)}`}
      description="Downloads the daily prices from the connected broker and stores them. It replaces any generated or untagged rows this symbol has, so only real prices remain, and keeps the symbol on the broker its history started with. Safe to repeat."
      confirmLabel="Fetch history"
      busyLabel="Fetching…"
      onConfirm={async () => {
        const result = await backfillPaperHistory(sub.id)
        if (result.status === 'failed') throw new Error(describeBackfill(result))
        onDone(describeBackfill(result))
      }}
      onClose={onClose}
    />
  )
}
