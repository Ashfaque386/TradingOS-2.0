'use client'

import { AlertOctagon, AlertTriangle, CheckCircle2, Info } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/error-notice'
import { type EngineAlert, listEngineAlerts } from '@/lib/api'
import { alertFacts, splitAlerts } from '@/lib/engine-alerts'
import { usePolling } from '@/lib/use-polling'

const ICON = { critical: AlertOctagon, warning: AlertTriangle, info: Info } as const
const CLEARED_SHOWN = 10

function Row({ alert, now, onShow, cleared }: { alert: EngineAlert; now: Date; onShow: (id: string) => void; cleared: boolean }) {
  const Icon = cleared ? CheckCircle2 : ICON[alert.severity]
  return (
    <li
      className={`pt-alert pt-alert-${cleared ? 'info' : alert.severity}`}
      role={!cleared && alert.severity === 'critical' ? 'alert' : undefined}
      data-testid="engine-alert"
      data-kind={alert.kind}
      data-state={cleared ? 'cleared' : 'open'}
    >
      <Icon className="size-4" aria-hidden="true" />
      <div>
        <h3>{alert.title}</h3>
        {alert.detail && <p>{alert.detail}</p>}
        <p className="pt-alert-facts">
          <time dateTime={alert.opened_at}>{new Date(alert.opened_at).toLocaleString()}</time> · {alertFacts(alert, now).join(' · ')}
        </p>
      </div>
      {alert.subscription_id && (
        <button type="button" className="button-secondary" onClick={() => onShow(alert.subscription_id as string)}>
          Show on the desk
        </button>
      )}
    </li>
  )
}

/**
 * What the trading engine has needed a person for: the open alerts, and the ones that cleared. An
 * alert closes itself when the problem is gone; nothing here dismisses one that is still true. The same
 * alerts go to the channels subscribed to "Engine alerts" in Settings.
 */
export function EngineAlertsPanel({ onShowSubscription }: { onShowSubscription: (id: string) => void }) {
  const { data, error, lastUpdated, refresh } = usePolling(() => listEngineAlerts('all', 60), { intervalMs: 30_000 })
  const { open, cleared } = splitAlerts(data?.alerts ?? [])
  const now = lastUpdated ?? new Date()

  return (
    <section id="engine-alerts" className="pt-engine-alerts" aria-label="Engine alerts">
      <div className="pt-engine-head">
        <h2>Engine alerts</h2>
        <p>
          {data
            ? data.open_count === 0
              ? 'Nothing is open. The engine checks the desk every minute.'
              : `${data.open_count} open${data.critical_open_count ? `, ${data.critical_open_count} critical` : ''}. Each closes by itself when the problem is gone.`
            : 'Loading…'}
        </p>
      </div>
      {error && <ErrorNotice message={`Could not load the engine alerts: ${error}`} onRetry={() => void refresh()} />}
      {open.length > 0 && (
        <ul className="pt-attention" aria-label="Open engine alerts">
          {open.map((alert) => (
            <Row key={alert.id} alert={alert} now={now} onShow={onShowSubscription} cleared={false} />
          ))}
        </ul>
      )}
      {cleared.length > 0 && (
        <details className="pt-engine-cleared">
          <summary>Recently cleared ({cleared.length})</summary>
          <ul className="pt-attention" aria-label="Cleared engine alerts">
            {cleared.slice(0, CLEARED_SHOWN).map((alert) => (
              <Row key={alert.id} alert={alert} now={now} onShow={onShowSubscription} cleared />
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
