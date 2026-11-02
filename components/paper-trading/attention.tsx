'use client'

import { AlertOctagon, AlertTriangle, Info } from 'lucide-react'
import { type AttentionItem, isHistoryWait } from '@/lib/paper-trading'

const ICON = { critical: AlertOctagon, warning: AlertTriangle, info: Info } as const

/** What an operator should look at first. Renders nothing when everything is fine. */
export function Attention({
  items,
  canAct,
  onAction,
}: {
  items: AttentionItem[]
  canAct: boolean
  onAction: (item: AttentionItem) => void
}) {
  if (items.length === 0) return null
  return (
    <section className="pt-attention" aria-label="Needs attention">
      {items.map((item) => {
        const Icon = ICON[item.severity]
        return (
          <div key={item.id} className={`pt-alert pt-alert-${item.severity}`} role={item.severity === 'critical' ? 'alert' : 'status'}>
            <Icon className="size-4" aria-hidden="true" />
            <div>
              <h3>{item.title}</h3>
              <p>{item.detail}</p>
            </div>
            {canAct && item.action === 'close' && (
              <button type="button" className="button-danger" onClick={() => onAction(item)}>Close position</button>
            )}
            {canAct && isHistoryWait(item) && (
              <button type="button" className="button-secondary" onClick={() => onAction(item)}>Fetch price history</button>
            )}
          </div>
        )
      })}
    </section>
  )
}
