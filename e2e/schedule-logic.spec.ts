import { expect, test } from '@playwright/test'
import { entityLabel, entityOptions, isRoutineEvent, describeEvent } from '../lib/activity-feed'
import { describeTrigger } from '../lib/schedule-format'

// Mission Control's schedule and the activity feeds, after the 2026-11-21 review found the first
// showing APScheduler's raw text and the second mostly sign-ins and webhooks under two spellings.

test.describe('describeTrigger', () => {
  test('an interval reads as how often', () => {
    expect(describeTrigger('interval[0:00:03]')).toBe('every 3 s')
    expect(describeTrigger('interval[0:01:00]')).toBe('every minute')
    expect(describeTrigger('interval[0:05:00]')).toBe('every 5 min')
    expect(describeTrigger('interval[0:10:00]')).toBe('every 10 min')
    expect(describeTrigger('interval[1:00:00]')).toBe('every hour')
    expect(describeTrigger('interval[6:00:00]')).toBe('every 6 h')
    expect(describeTrigger('interval[1 day, 0:00:00]')).toBe('every day')
    expect(describeTrigger('interval[0:01:30]')).toBe('every 90 s')
  })

  test('a cron time reads in IST, with its days', () => {
    expect(describeTrigger("cron[hour='7', minute='30']")).toBe('daily 07:30 IST')
    expect(describeTrigger("cron[hour='22', minute='45']")).toBe('daily 22:45 IST')
    expect(describeTrigger("cron[day_of_week='mon-fri', hour='8', minute='30']")).toBe('Mon–Fri 08:30 IST')
    expect(describeTrigger("cron[day_of_week='mon', hour='7', minute='0']")).toBe('Mondays 07:00 IST')
    expect(describeTrigger("cron[day_of_week='mon,wed', hour='9', minute='5']")).toBe('Mon, Wed 09:05 IST')
  })

  test('anything it does not recognise is shown as sent', () => {
    for (const raw of [
      "cron[hour='*/2', minute='0']",
      "cron[day='1', hour='7', minute='0']",
      "cron[hour='7', minute='0', second='30']",
      "cron[day_of_week='weekday', hour='7', minute='0']",
      'interval[0:00:00]',
      'date[2026-11-21 10:00:00 IST]',
      'something else',
    ]) {
      expect(describeTrigger(raw)).toBe(raw)
    }
  })
})

test.describe('activity feed labels', () => {
  test('the two spellings of one thing get one label', () => {
    expect(entityLabel('broker-credentials')).toBe(entityLabel('broker_credentials'))
    expect(entityLabel('notification-channels')).toBe(entityLabel('notification_channel'))
    expect(entityLabel('paper-trading')).toBe(entityLabel('paper_trading_subscription'))
    expect(entityLabel('risk-limits')).toBe(entityLabel('risk-limit'))
    expect(entityLabel('strategies')).toBe(entityLabel('strategy'))
    expect(entityLabel('webhooks')).toBe('Webhooks')
  })

  test('an unknown type is spelled out rather than dropped', () => {
    expect(entityLabel('new_thing-here')).toBe('New thing here')
    expect(entityLabel(null)).toBe('Other')
  })

  test('the filter offers one option per label, standing for every spelling', () => {
    expect(entityOptions(['strategy', 'broker_credentials', 'strategies', 'broker-credentials'])).toEqual([
      { label: 'Broker credentials', values: ['broker-credentials', 'broker_credentials'] },
      { label: 'Strategies', values: ['strategies', 'strategy'] },
    ])
  })

  test('sign-ins and webhook deliveries are routine; everything else is not', () => {
    expect(isRoutineEvent({ entity_type: 'auth', action: 'POST /api/v1/auth/login' })).toBe(true)
    expect(isRoutineEvent({ entity_type: 'webhooks', action: 'POST /api/v1/webhooks/alertmanager' })).toBe(true)
    expect(isRoutineEvent({ entity_type: null, action: 'POST /api/v1/auth/logout' })).toBe(true)
    expect(isRoutineEvent({ entity_type: 'kill-switch', action: 'POST /api/v1/kill-switch/{mode}/reset' })).toBe(false)
    expect(isRoutineEvent({ entity_type: 'user', action: 'auth.account_locked' })).toBe(false)
  })

  test('a row names the entity in the console words', () => {
    expect(
      describeEvent({ actor: 'alertmanager', action: 'POST /api/v1/webhooks/alertmanager', entity_type: 'webhooks', entity_id: null }),
    ).toBe('alertmanager · POST /api/v1/webhooks/alertmanager Webhooks')
    expect(
      describeEvent({ actor: 'startup', action: 'agent_gateway.config_applied', entity_type: 'agent_config_version', entity_id: '502' }),
    ).toBe('startup · agent_gateway.config_applied Agent Gateway 502')
  })
})
