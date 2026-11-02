import { expect, test } from '@playwright/test'
import type { AuditActorSummary, AuditChainVerifyResult } from '../lib/api'
import {
  NO_FILTER,
  actionShares,
  filterFromUrl,
  filterToUrl,
  indiaStamp,
  indiaTimeToIso,
  isFiltered,
  pageWindow,
  queryFor,
  rangeProblem,
  shortHash,
  verifyChecks,
  verifyFailed,
} from '../lib/audit'

// What the Audit page decides, with no browser and no backend: each case states the answer worked out by hand.

const verified = (over: Partial<AuditChainVerifyResult> = {}): AuditChainVerifyResult => ({
  diverged: false,
  archive_internally_valid: true,
  live_db_matches_archive: true,
  first_diverged_sequence: null,
  reason: null,
  entries_checked_in_db_chain: 12_450,
  db_chain_valid: true,
  db_chain_reason: null,
  ...over,
})

test.describe('typing a time', () => {
  test('is read as India time, which is five and a half hours ahead of UTC', () => {
    expect(indiaTimeToIso('2026-11-02T09:15')).toBe('2026-11-02T09:15:00+05:30')
    expect(new Date(indiaTimeToIso('2026-11-02T09:15') as string).toISOString()).toBe('2026-11-02T03:45:00.000Z')
  })

  test('keeps seconds when they are typed', () => {
    expect(indiaTimeToIso('2026-11-02T09:15:30')).toBe('2026-11-02T09:15:30+05:30')
  })

  test('is nothing at all for a box that is empty or half filled', () => {
    expect(indiaTimeToIso('')).toBeUndefined()
    expect(indiaTimeToIso('2026-11-02')).toBeUndefined()
    expect(indiaTimeToIso('2026-13-45T99:99')).toBeUndefined()
  })
})

test.describe('the request for what is on screen', () => {
  test('leaves out every empty box and trims what is typed', () => {
    expect(queryFor({ ...NO_FILTER, search: '  order  ', actor: 'alice', action: ' ' })).toEqual({
      search: 'order',
      actor: 'alice',
    })
  })

  test('carries each filter under the name the server expects', () => {
    const query = queryFor({
      search: 's',
      actor: 'a',
      entityType: 'order',
      action: 'order.filled',
      entityId: 'o-1',
      since: '2026-11-02T09:15',
      until: '2026-11-02T15:30',
    })

    expect(query).toEqual({
      search: 's',
      actor: 'a',
      entityType: 'order',
      action: 'order.filled',
      entityId: 'o-1',
      since: '2026-11-02T09:15:00+05:30',
      until: '2026-11-02T15:30:00+05:30',
    })
  })

  test('is nothing when nothing is set, and that is "not filtered"', () => {
    expect(queryFor(NO_FILTER)).toEqual({})
    expect(isFiltered(NO_FILTER)).toBe(false)
    expect(isFiltered({ ...NO_FILTER, entityId: 'o-1' })).toBe(true)
    expect(isFiltered({ ...NO_FILTER, search: '   ' })).toBe(false)
  })

  test('a range that ends before it starts is named, not shown as an empty log', () => {
    expect(rangeProblem({ ...NO_FILTER, since: '2026-11-02T10:00', until: '2026-11-02T09:00' })).toMatch(/before its start/)
    expect(rangeProblem({ ...NO_FILTER, since: '2026-11-02T09:00', until: '2026-11-02T10:00' })).toBeNull()
    expect(rangeProblem({ ...NO_FILTER, since: '2026-11-02T09:00' })).toBeNull()
  })
})

test.describe('the filters in the address', () => {
  test('round-trip, with the server’s names for the keys', () => {
    const filter = { ...NO_FILTER, actor: 'a@b.in', entityType: 'order', entityId: 'o 1', since: '2026-11-02T09:15' }

    const url = filterToUrl(filter)

    expect(url).toBe('?actor=a%40b.in&entity_type=order&entity_id=o+1&since=2026-11-02T09%3A15')
    expect(filterFromUrl(url)).toEqual(filter)
  })

  test('are no address at all when nothing is set', () => {
    expect(filterToUrl(NO_FILTER)).toBe('')
    expect(filterFromUrl('')).toEqual(NO_FILTER)
  })

  test('ignore anything else in the address', () => {
    expect(filterFromUrl('?utm=1&actor=bob')).toEqual({ ...NO_FILTER, actor: 'bob' })
  })
})

test.describe('which rows a page shows', () => {
  test('the first page of many', () => {
    expect(pageWindow(130, 1, 50)).toEqual({ pages: 3, current: 1, offset: 0, from: 1, to: 50 })
  })

  test('the last page holds what is left', () => {
    expect(pageWindow(130, 3, 50)).toEqual({ pages: 3, current: 3, offset: 100, from: 101, to: 130 })
  })

  test('a page past the end is the last page, and one before the start is the first', () => {
    expect(pageWindow(130, 9, 50).current).toBe(3)
    expect(pageWindow(130, 0, 50).current).toBe(1)
  })

  test('nothing to show is page 1 of 1 with no rows', () => {
    expect(pageWindow(0, 1, 50)).toEqual({ pages: 1, current: 1, offset: 0, from: 0, to: 0 })
  })

  test('a total that fills the pages exactly has no empty page after it', () => {
    expect(pageWindow(100, 2, 50)).toEqual({ pages: 2, current: 2, offset: 50, from: 51, to: 100 })
  })
})

test.describe('times and hashes', () => {
  test('a time is shown in India time whatever the browser’s zone is', () => {
    expect(indiaStamp('2026-11-02T03:45:09Z')).toBe('02 Nov 2026, 09:15:09')
    expect(indiaStamp('2026-11-01T20:30:00Z')).toBe('02 Nov 2026, 02:00:00') // already tomorrow in Mumbai
  })

  test('a missing or unreadable time is a dash', () => {
    expect(indiaStamp(null)).toBe('—')
    expect(indiaStamp('not a time')).toBe('—')
  })

  test('a long hash is shortened to its two ends, a short one is left alone', () => {
    expect(shortHash('0123456789abcdef0123456789abcdef')).toBe('01234567…abcdef')
    expect(shortHash('abc123')).toBe('abc123')
  })
})

test.describe('what a verification says', () => {
  test('all three checks hold, and the first says how many entries were read', () => {
    const checks = verifyChecks(verified())

    expect(checks.map((c) => c.ok)).toEqual([true, true, true])
    expect(checks[0].detail).toBe('12,450 entries checked; every link matches the entry before it.')
    expect(verifyFailed(verified())).toBe(false)
  })

  test('a broken database chain is named with its reason, and the others still hold', () => {
    const result = verified({ db_chain_valid: false, db_chain_reason: 'entry 88 does not follow entry 87' })

    const checks = verifyChecks(result)

    expect(checks.map((c) => c.ok)).toEqual([false, true, true])
    expect(checks[0].detail).toBe('entry 88 does not follow entry 87')
    expect(verifyFailed(result)).toBe(true)
  })

  test('an archive that differs from the database says where it first differs', () => {
    const result = verified({
      diverged: true,
      live_db_matches_archive: false,
      first_diverged_sequence: 4021,
      reason: 'hash differs',
    })

    const checks = verifyChecks(result)

    expect(checks[2].ok).toBe(false)
    expect(checks[2].detail).toBe('hash differs The first difference is at entry #4021.')
    expect(checks[1].ok).toBe(true)
    expect(verifyFailed(result)).toBe(true)
  })

  test('an archive that fails on its own is told apart from one that merely differs', () => {
    const checks = verifyChecks(verified({ diverged: true, archive_internally_valid: false, reason: 'line 9 is cut off' }))

    expect(checks[1]).toMatchObject({ ok: false, detail: 'line 9 is cut off' })
  })
})

test.describe('an actor’s actions', () => {
  const summary = (actions: Record<string, number>): AuditActorSummary => ({
    actor: 'alice',
    action_count: Object.values(actions).reduce((a, b) => a + b, 0),
    first_seen: null,
    last_seen: null,
    actions,
  })

  test('come most frequent first, each with its share of the whole', () => {
    expect(actionShares(summary({ 'order.filled': 1, 'auth.login': 3 }))).toEqual([
      { action: 'auth.login', count: 3, share: 0.75 },
      { action: 'order.filled', count: 1, share: 0.25 },
    ])
  })

  test('equal counts are in alphabetical order, so the list does not shuffle', () => {
    expect(actionShares(summary({ b: 2, a: 2 })).map((r) => r.action)).toEqual(['a', 'b'])
  })

  test('no actions is an empty list, not a division by zero', () => {
    expect(actionShares(summary({}))).toEqual([])
  })
})
