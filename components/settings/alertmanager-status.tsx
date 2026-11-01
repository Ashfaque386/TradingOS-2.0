'use client'

import { ErrorNotice } from '@/components/ui/error-notice'
import { getAlertmanagerStatus } from '@/lib/api'
import { alertmanagerSummary } from '@/lib/engine-alerts'
import { usePolling } from '@/lib/use-polling'

/**
 * Whether the alerts Prometheus raises can reach the channels above: is Alertmanager up, has the webhook
 * token been created, and when did an alert last come through it. A line, not a form: there is nothing to
 * set here (the token is created on first boot).
 */
export function AlertmanagerStatus() {
  const { data, error, refresh } = usePolling(getAlertmanagerStatus, { intervalMs: 60_000 })
  const summary = data ? alertmanagerSummary(data) : null

  return (
    <div className="set-inbound" aria-label="Prometheus alerts">
      <label>Prometheus alerts</label>
      {error && <ErrorNotice message={`Could not check Alertmanager: ${error}`} onRetry={() => void refresh()} />}
      {!data && !error && <p className="set-inbound-note">Checking…</p>}
      {summary && (
        <p
          className={summary.tone === 'ok' ? 'set-inbound-ok' : summary.tone === 'warn' ? 'set-inbound-error' : 'set-inbound-note'}
          data-testid="alertmanager-status"
          data-tone={summary.tone}
        >
          {summary.text}
        </p>
      )}
      <p className="set-inbound-note">
        What Prometheus raises (a stalled price feed, a slow order, the alert watcher stopping) goes to the channels
        subscribed to “Engine alerts”. If the backend itself is down it cannot receive the alert, so that one is
        visible in Prometheus and Grafana only.
      </p>
    </div>
  )
}
