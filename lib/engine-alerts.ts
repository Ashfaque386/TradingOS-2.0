// The words for an engine alert, and how the list is split: open ones, and the ones that cleared.
// Nothing here touches React, the network or the clock (`now` is passed in), so it is tested
// directly (e2e/engine-alerts-logic.spec.ts).

import type { AlertmanagerStatus, EngineAlert, EngineAlertSource } from './api'

export const SOURCE_LABEL: Record<EngineAlertSource, string> = {
  watcher: 'Paper desk check',
  event: 'Engine',
  alertmanager: 'Prometheus',
}

/** `45s`, `31 min`, `2.5 h`, `3 d`: how long a thing lasted. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, seconds)
  if (s < 60) return `${Math.round(s)}s`
  if (s < 3600) return `${Math.round(s / 60)} min`
  if (s < 86_400) return `${(s / 3600).toFixed(1)} h`
  return `${Math.round(s / 86_400)} d`
}

const seconds = (from: string, to: string | Date) => (new Date(to).getTime() - new Date(from).getTime()) / 1000

/** What an alert says about itself under its title, in the order a person reads it. */
export function alertFacts(alert: EngineAlert, now: Date): string[] {
  const facts = [`Raised by: ${SOURCE_LABEL[alert.source]}`]
  if (alert.resolved_at === null) {
    facts.push(`Open for ${formatDuration(seconds(alert.opened_at, now))}`)
  } else if (alert.resolved_at === alert.opened_at) {
    facts.push('Happened at the time shown')
  } else {
    facts.push(`Lasted ${formatDuration(seconds(alert.opened_at, alert.resolved_at))}`)
  }
  if (alert.notified_at !== null) facts.push('Sent to the channels')
  else if (alert.resolved_at !== null) facts.push('Cleared before it was sent')
  else facts.push('Not sent to the channels yet')
  return facts
}

/** Open alerts, most serious first and newest first within a severity; and the cleared ones, newest first. */
export function splitAlerts(alerts: EngineAlert[]): { open: EngineAlert[]; cleared: EngineAlert[] } {
  const rank = { critical: 0, warning: 1, info: 2 } as const
  const open = alerts
    .filter((a) => a.resolved_at === null)
    .sort((a, b) => rank[a.severity] - rank[b.severity] || (a.opened_at < b.opened_at ? 1 : a.opened_at > b.opened_at ? -1 : 0))
  const cleared = alerts
    .filter((a) => a.resolved_at !== null)
    .sort((a, b) => (a.resolved_at! < b.resolved_at! ? 1 : a.resolved_at! > b.resolved_at! ? -1 : 0))
  return { open, cleared }
}

/** The top-bar chip's words: nothing when nothing is open, otherwise how many and whether any is critical. */
export function chipLabel(open: number, critical: number): { text: string; title: string } | null {
  if (open <= 0) return null
  const noun = open === 1 ? 'alert' : 'alerts'
  return {
    text: `${open} ${noun}`,
    title:
      critical > 0
        ? `${open} open engine ${noun}, ${critical} critical. Open the paper desk to read them.`
        : `${open} open engine ${noun}. Open the paper desk to read them.`,
  }
}

/** Settings' one line about the path from Prometheus to a person's channel. */
export function alertmanagerSummary(status: AlertmanagerStatus): { tone: 'ok' | 'warn' | 'off'; text: string } {
  if (!status.configured) return { tone: 'off', text: 'This deployment has no Alertmanager, so Prometheus alerts do not reach your channels.' }
  if (status.reachable === false) return { tone: 'warn', text: `${status.detail} Prometheus alerts are not reaching your channels.` }
  if (!status.token_ready) return { tone: 'warn', text: 'Alertmanager is up but the webhook token has not been created, so the backend would refuse its alerts.' }
  return {
    tone: 'ok',
    text: status.last_alert_received_at
      ? `Alertmanager is up. The last alert came through at ${new Date(status.last_alert_received_at).toLocaleString()}.`
      : 'Alertmanager is up. No alert has come through it yet.',
  }
}
