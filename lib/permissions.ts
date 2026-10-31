import type { Role } from './api'
import table from './permissions.generated.json'

/**
 * Whether `role` may call a route, answered from the backend's own policy table
 * (`backend/scripts/export_permissions.py` writes `permissions.generated.json`; a backend test fails
 * when it is stale). Ask with the action the control performs, e.g.
 * `can(role, 'POST /api/v1/kill-switch/{mode}/reset')`, not with a list of roles: when the server's
 * policy changes the control follows, instead of showing a button the server then refuses.
 *
 * An action that is not in the table is refused, as the server would.
 */
export type Action = keyof typeof table

const permissions: Record<string, readonly string[]> = table

export function can(role: Role | null | undefined, action: Action): boolean {
  if (!role) return false
  return permissions[action]?.includes(role) ?? false
}
