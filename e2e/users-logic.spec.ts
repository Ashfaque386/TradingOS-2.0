import { expect, test } from '@playwright/test'
import type { AdminUser } from '../lib/api'
import { accountState, lockedMinutesLeft, newAccountProblem, roleChangedText, usersSummary } from '../lib/users'

// What Settings > Users says about an account. No browser, no backend: each case states the answer worked out by hand.

const NOW = new Date('2026-11-02T10:00:00Z')

const user = (over: Partial<AdminUser> = {}): AdminUser => ({
  id: '00000000-0000-4000-8000-000000000001',
  email: 'pm@example.com',
  role: 'PortfolioManager',
  is_active: true,
  locked_until: null,
  ...over,
})

test.describe('a lock', () => {
  test('has minutes left, rounded up', () => {
    expect(lockedMinutesLeft({ locked_until: '2026-11-02T10:10:00Z' }, NOW)).toBe(10)
    expect(lockedMinutesLeft({ locked_until: '2026-11-02T10:00:01Z' }, NOW)).toBe(1)
    expect(lockedMinutesLeft({ locked_until: '2026-11-02T10:14:01Z' }, NOW)).toBe(15)
  })

  test('that has run out, or was never set, is not a lock', () => {
    expect(lockedMinutesLeft({ locked_until: '2026-11-02T10:00:00Z' }, NOW)).toBeNull()
    expect(lockedMinutesLeft({ locked_until: '2026-11-02T09:00:00Z' }, NOW)).toBeNull()
    expect(lockedMinutesLeft({ locked_until: null }, NOW)).toBeNull()
  })

  test('that cannot be read is not a lock', () => {
    expect(lockedMinutesLeft({ locked_until: 'tomorrow-ish' }, NOW)).toBeNull()
  })
})

test.describe('the state of an account', () => {
  test('is active by default', () => {
    expect(accountState(user(), NOW)).toEqual({ kind: 'active', label: 'Active' })
  })

  test('says how long a lock has to run', () => {
    expect(accountState(user({ locked_until: '2026-11-02T10:07:30Z' }), NOW)).toEqual({
      kind: 'locked',
      label: 'Locked, 8 min left',
    })
  })

  test('shows a lock that has run out as active', () => {
    expect(accountState(user({ locked_until: '2026-11-02T09:59:00Z' }), NOW).kind).toBe('active')
  })

  test('deactivated wins over a lock', () => {
    expect(accountState(user({ is_active: false, locked_until: '2026-11-02T10:30:00Z' }), NOW)).toEqual({
      kind: 'deactivated',
      label: 'Deactivated',
    })
  })
})

test.describe('a new account', () => {
  test('needs a plausible email', () => {
    expect(newAccountProblem('', 'long-enough-1')).toBe('Enter a valid email address.')
    expect(newAccountProblem('not an email', 'long-enough-1')).toBe('Enter a valid email address.')
    expect(newAccountProblem('a@b', 'long-enough-1')).toBe('Enter a valid email address.')
  })

  test('needs a password of 8 to 128 characters', () => {
    expect(newAccountProblem('a@b.co', '1234567')).toBe('The password must be at least 8 characters.')
    expect(newAccountProblem('a@b.co', '12345678')).toBeNull()
    expect(newAccountProblem('a@b.co', 'x'.repeat(128))).toBeNull()
    expect(newAccountProblem('a@b.co', 'x'.repeat(129))).toBe('The password must be no more than 128 characters.')
  })

  test('is not judged on spaces around the email', () => {
    expect(newAccountProblem('  a@b.co  ', 'long-enough-1')).toBeNull()
  })
})

test.describe('the summary line', () => {
  test('counts accounts, active ones, locked ones and administrators', () => {
    const users = [
      user({ email: 'a', role: 'SystemAdministrator' }),
      user({ email: 'b', role: 'SystemAdministrator', is_active: false }),
      user({ email: 'c', locked_until: '2026-11-02T10:05:00Z' }),
      user({ email: 'd', locked_until: '2026-11-02T08:00:00Z' }),
    ]
    expect(usersSummary(users, NOW)).toBe('4 accounts, 3 active, 1 locked, 1 administrator.')
  })

  test('leaves out locked when nobody is, and speaks of one account in the singular', () => {
    expect(usersSummary([user({ role: 'SystemAdministrator' })], NOW)).toBe('1 account, 1 active, 1 administrator.')
  })

  test('does not count a deactivated administrator', () => {
    expect(usersSummary([user({ role: 'SystemAdministrator', is_active: false })], NOW)).toBe(
      '1 account, 0 active, 0 administrators.',
    )
  })
})

test('a role change is reported with the role in words', () => {
  expect(roleChangedText('pm@example.com', 'RiskManager')).toBe('pm@example.com is now Risk Manager.')
})
