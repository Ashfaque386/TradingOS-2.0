import type { AdminUser, Role } from './api'

// What Settings > Users says about an account. Kept apart from the component so the wording and the
// rules (when an account counts as locked, what a new password must be) are checked without a browser.

export const ASSIGNABLE_ROLES: Role[] = ['SystemAdministrator', 'PortfolioManager', 'RiskManager', 'ReadOnlyAuditor']

export const ROLE_MEANING: Record<Role, string> = {
  SystemAdministrator: 'Everything, including accounts, keys and backups',
  PortfolioManager: 'Strategies, backtests, paper and live trading',
  RiskManager: 'Kill switch, risk limits and approvals',
  ReadOnlyAuditor: 'Looks at everything, changes nothing',
}

/** Whole minutes left on a lock, rounded up, or null when the account is not locked (including when the
 * lock has run out, which the server leaves on the row until the next sign-in). */
export function lockedMinutesLeft(user: Pick<AdminUser, 'locked_until'>, now: Date): number | null {
  if (!user.locked_until) return null
  const ms = new Date(user.locked_until).getTime() - now.getTime()
  if (!Number.isFinite(ms) || ms <= 0) return null
  return Math.ceil(ms / 60_000)
}

export type AccountState = { kind: 'active' | 'deactivated' | 'locked'; label: string }

export function accountState(user: Pick<AdminUser, 'is_active' | 'locked_until'>, now: Date): AccountState {
  if (!user.is_active) return { kind: 'deactivated', label: 'Deactivated' }
  const left = lockedMinutesLeft(user, now)
  if (left !== null) return { kind: 'locked', label: `Locked, ${left} min left` }
  return { kind: 'active', label: 'Active' }
}

/** Why a new account cannot be created yet, or null. The server checks again; this saves the round trip. */
export function newAccountProblem(email: string, password: string): string | null {
  if (!/^\S+@\S+\.\S+$/.test(email.trim())) return 'Enter a valid email address.'
  if (password.length < 8) return 'The password must be at least 8 characters.'
  if (password.length > 128) return 'The password must be no more than 128 characters.'
  return null
}

/** The sentence under the table's heading, from what the server told us about the people in it. */
export function usersSummary(users: AdminUser[], now: Date): string {
  const active = users.filter((u) => u.is_active).length
  const locked = users.filter((u) => accountState(u, now).kind === 'locked').length
  const admins = users.filter((u) => u.is_active && u.role === 'SystemAdministrator').length
  const parts = [`${users.length} account${users.length === 1 ? '' : 's'}`, `${active} active`]
  if (locked > 0) parts.push(`${locked} locked`)
  parts.push(`${admins} administrator${admins === 1 ? '' : 's'}`)
  return parts.join(', ') + '.'
}

/** What happened, in the past tense, after a role was changed. */
export function roleChangedText(email: string, role: Role): string {
  return `${email} is now ${role.replace(/([a-z])([A-Z])/g, '$1 $2')}.`
}
