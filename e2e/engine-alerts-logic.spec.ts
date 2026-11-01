import { expect, test } from '@playwright/test'
import { alertFacts, alertmanagerSummary, chipLabel, formatDuration, splitAlerts } from '../lib/engine-alerts'
import type { AlertmanagerStatus, EngineAlert } from '../lib/api'

// The wording of the engine-alert panel, the top-bar count and Settings' Alertmanager line.
// No browser, no backend: each case states the answer worked out by hand.

function alert(overrides: Partial<EngineAlert> = {}): EngineAlert {
  return {
    id: 'a1', key: 'k', source: 'watcher', kind: 'halted', severity: 'warning', title: 'T is not trading', detail: 'why',
    subscription_id: null, opened_at: '2026-11-02T04:00:00Z', last_seen_at: '2026-11-02T04:00:00Z', resolved_at: null, notified_at: null,
    ...overrides,
  }
}

test.describe('how long it lasted', () => {
  test('seconds, minutes, hours, days', () => {
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(44.6)).toBe('45s')
    expect(formatDuration(59.4)).toBe('59s')
    expect(formatDuration(60)).toBe('1 min')
    expect(formatDuration(1860)).toBe('31 min')
    expect(formatDuration(3599)).toBe('60 min')
    expect(formatDuration(3600)).toBe('1.0 h')
    expect(formatDuration(9000)).toBe('2.5 h')
    expect(formatDuration(86_400)).toBe('1 d')
    expect(formatDuration(3 * 86_400)).toBe('3 d')
  })

  test('a clock that is a little behind never shows a negative time', () => {
    expect(formatDuration(-5)).toBe('0s')
  })
})

test.describe('what an alert says about itself', () => {
  const now = new Date('2026-11-02T04:31:00Z')

  test('an open one says who raised it, how long, and that the channels have not been told', () => {
    expect(alertFacts(alert(), now)).toEqual(['Raised by: Paper desk check', 'Open for 31 min', 'Not sent to the channels yet'])
  })

  test('an open one that was announced says so', () => {
    expect(alertFacts(alert({ source: 'alertmanager', notified_at: '2026-11-02T04:02:00Z' }), now)).toEqual([
      'Raised by: Prometheus',
      'Open for 31 min',
      'Sent to the channels',
    ])
  })

  test('a cleared one says how long it lasted', () => {
    expect(alertFacts(alert({ resolved_at: '2026-11-02T04:12:00Z', notified_at: '2026-11-02T04:02:00Z' }), now)).toEqual([
      'Raised by: Paper desk check',
      'Lasted 12 min',
      'Sent to the channels',
    ])
  })

  test('one that cleared before it was announced says that', () => {
    expect(alertFacts(alert({ resolved_at: '2026-11-02T04:01:00Z' }), now)).toEqual([
      'Raised by: Paper desk check',
      'Lasted 1 min',
      'Cleared before it was sent',
    ])
  })

  test('something that happened at a moment (a stop-loss exit) has no duration', () => {
    const stop = alert({ source: 'event', resolved_at: '2026-11-02T04:00:00Z', notified_at: '2026-11-02T04:00:00Z' })
    expect(alertFacts(stop, now)).toEqual(['Raised by: Engine', 'Happened at the time shown', 'Sent to the channels'])
  })
})

test.describe('open and cleared', () => {
  test('open ones come most serious first, newest first within a severity; cleared ones newest first', () => {
    const list = [
      alert({ id: 'w-old', severity: 'warning', opened_at: '2026-11-02T04:00:00Z' }),
      alert({ id: 'c-old', severity: 'critical', opened_at: '2026-11-02T03:00:00Z' }),
      alert({ id: 'w-new', severity: 'warning', opened_at: '2026-11-02T05:00:00Z' }),
      alert({ id: 'done-1', resolved_at: '2026-11-02T06:00:00Z' }),
      alert({ id: 'done-2', resolved_at: '2026-11-02T07:00:00Z' }),
    ]
    const { open, cleared } = splitAlerts(list)
    expect(open.map((a) => a.id)).toEqual(['c-old', 'w-new', 'w-old'])
    expect(cleared.map((a) => a.id)).toEqual(['done-2', 'done-1'])
  })

  test('nothing in, nothing out', () => {
    expect(splitAlerts([])).toEqual({ open: [], cleared: [] })
  })
})

test.describe('the count in the top bar', () => {
  test('nothing open shows nothing', () => {
    expect(chipLabel(0, 0)).toBeNull()
  })

  test('one warning', () => {
    expect(chipLabel(1, 0)).toEqual({ text: '1 alert', title: '1 open engine alert. Open the paper desk to read them.' })
  })

  test('several, some critical', () => {
    expect(chipLabel(3, 2)).toEqual({ text: '3 alerts', title: '3 open engine alerts, 2 critical. Open the paper desk to read them.' })
  })
})

test.describe('the Alertmanager line in Settings', () => {
  const status = (overrides: Partial<AlertmanagerStatus> = {}): AlertmanagerStatus => ({
    configured: true, reachable: true, detail: 'Alertmanager is up.', token_ready: true, last_alert_received_at: null, ...overrides,
  })

  test('a deployment without one says Prometheus alerts do not reach the channels', () => {
    expect(alertmanagerSummary(status({ configured: false, reachable: null }))).toEqual({
      tone: 'off',
      text: 'This deployment has no Alertmanager, so Prometheus alerts do not reach your channels.',
    })
  })

  test('one that cannot be reached is a warning with the reason', () => {
    expect(alertmanagerSummary(status({ reachable: false, detail: 'Could not reach Alertmanager: ConnectError' }))).toEqual({
      tone: 'warn',
      text: 'Could not reach Alertmanager: ConnectError Prometheus alerts are not reaching your channels.',
    })
  })

  test('one that is up with no token is a warning', () => {
    expect(alertmanagerSummary(status({ token_ready: false })).tone).toBe('warn')
  })

  test('one that is up and has never delivered says so; one that has says when', () => {
    expect(alertmanagerSummary(status())).toEqual({ tone: 'ok', text: 'Alertmanager is up. No alert has come through it yet.' })
    const done = alertmanagerSummary(status({ last_alert_received_at: '2026-11-02T04:20:00Z' }))
    expect(done.tone).toBe('ok')
    expect(done.text).toMatch(/^Alertmanager is up\. The last alert came through at /)
  })
})
