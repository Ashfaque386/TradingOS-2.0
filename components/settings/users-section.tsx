'use client'

import { useState } from 'react'
import { CheckCircle2, Lock, ShieldOff, UserPlus, XCircle } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { ErrorNotice } from '@/components/ui/error-notice'
import { useAuth, roleLabel } from '@/components/auth/auth-provider'
import {
  type AdminUser,
  type Role,
  ApiError,
  changeAdminUserRole,
  createAdminUser,
  deactivateAdminUser,
  listAdminUsers,
  reactivateAdminUser,
  revokeAdminUserSessions,
  unlockAdminUser,
} from '@/lib/api'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'
import { ASSIGNABLE_ROLES, ROLE_MEANING, accountState, newAccountProblem, roleChangedText, usersSummary } from '@/lib/users'

// Who can sign in, and as what. Registration is closed once the first account exists, so this is the only
// way another person gets in. Everything here is SystemAdministrator-only on the server too; the section is
// shown to other roles as read-only text, not as buttons that would be refused.

type Pending = { kind: 'deactivate' | 'revoke'; user: AdminUser }
type Outcome = { ok: boolean; text: string }

const reasonOf = (err: unknown, fallback: string) => (err instanceof ApiError && err.message ? err.message : fallback)

function CreateForm({ onCreated, onOutcome }: { onCreated: () => void; onOutcome: (outcome: Outcome) => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('ReadOnlyAuditor')
  const [busy, setBusy] = useState(false)
  const problem = email === '' && password === '' ? null : newAccountProblem(email, password)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (newAccountProblem(email, password)) return
    setBusy(true)
    try {
      const created = await createAdminUser(email.trim(), password, role)
      onOutcome({ ok: true, text: `Created ${created.email} as ${roleLabel(created.role)}. Give them the password yourself; they can sign in now.` })
      setEmail('')
      setPassword('')
      onCreated()
    } catch (err) {
      onOutcome({ ok: false, text: reasonOf(err, 'The account was not created.') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="mt-4 flex flex-wrap items-end gap-2 rounded-xl border border-white/10 p-3" onSubmit={submit} aria-label="Create an account">
      <label className="set-field">
        <span>Email address</span>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
      </label>
      <label className="set-field">
        <span>First password</span>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
      </label>
      <label className="set-field">
        <span>Role</span>
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {ASSIGNABLE_ROLES.map((r) => (
            <option key={r} value={r}>{roleLabel(r)}</option>
          ))}
        </select>
      </label>
      <button className="set-btn set-btn-primary" type="submit" disabled={busy || email === '' || password === '' || problem !== null}>
        <UserPlus className="size-3.5" /> Create account
      </button>
      <p className="w-full text-xs text-muted-foreground">{ROLE_MEANING[role]}.</p>
      {problem && <p className="w-full text-xs text-rose-300" role="alert">{problem}</p>}
    </form>
  )
}

export function UsersSection() {
  const { role, user: me } = useAuth()
  const allowed = can(role, 'GET /api/v1/admin/users')
  const { data, error, loading, refresh } = usePolling(listAdminUsers, { intervalMs: 30_000, enabled: allowed })
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const now = new Date()

  async function act(target: AdminUser, run: () => Promise<unknown>, done: string) {
    setBusyId(target.id)
    setOutcome(null)
    try {
      await run()
      setOutcome({ ok: true, text: done })
    } catch (err) {
      setOutcome({ ok: false, text: reasonOf(err, 'That did not go through.') })
    } finally {
      setBusyId(null)
      await refresh()
    }
  }

  return (
    <div className="set-block" aria-label="Users" data-testid="users-section">
      <div className="set-block-head">
        <div>
          <h2>Users</h2>
          <p>Who can sign in, and as what. Nobody can register themselves once the first account exists, so this is how another person gets in.</p>
        </div>
      </div>

      {!allowed && <p className="text-xs text-muted-foreground">Read-only — SystemAdministrator role required.</p>}
      {allowed && error && <ErrorNotice message={`Could not read the accounts: ${error}`} onRetry={() => void refresh()} className="mb-3" />}
      {allowed && loading && !data && !error && <p className="set-inbound-note">Reading the accounts…</p>}

      {allowed && outcome && (
        <div role={outcome.ok ? 'status' : 'alert'} data-testid="users-result" className={`set-test-result mb-3 ${outcome.ok ? 'ok' : 'fail'}`}>
          {outcome.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}
          {outcome.text}
        </div>
      )}

      {allowed && data && (
        <>
          <p className="set-inbound-note mb-3" data-testid="users-summary">{usersSummary(data, now)}</p>
          <div className="set-matrix table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Role</th>
                  <th scope="col">State</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.map((u) => {
                  const state = accountState(u, now)
                  const isMe = u.id === me?.id
                  const busy = busyId === u.id
                  return (
                    <tr key={u.id} data-testid="user-row" data-email={u.email} data-state={state.kind}>
                      <td>
                        {u.email}
                        {isMe && <small> (you)</small>}
                      </td>
                      <td>
                        <select
                          aria-label={`Role of ${u.email}`}
                          value={u.role}
                          disabled={busy}
                          onChange={(e) => {
                            const next = e.target.value as Role
                            void act(u, () => changeAdminUserRole(u.id, next), roleChangedText(u.email, next))
                          }}
                        >
                          {ASSIGNABLE_ROLES.map((r) => (
                            <option key={r} value={r}>{roleLabel(r)}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <span className={`set-token-strip ${state.kind === 'active' ? 'valid' : 'rejected'}`} data-testid="user-state">
                          {state.kind === 'locked' && <Lock className="size-3" />}
                          {state.kind === 'deactivated' && <ShieldOff className="size-3" />}
                          {state.label}
                        </span>
                      </td>
                      <td>
                        <div className="flex flex-wrap gap-2">
                          {state.kind === 'locked' && (
                            <button className="set-btn set-btn-secondary" disabled={busy} onClick={() => void act(u, () => unlockAdminUser(u.id), `${u.email} can sign in again.`)}>
                              Unlock
                            </button>
                          )}
                          {u.is_active ? (
                            <button className="set-btn set-btn-danger" disabled={busy} onClick={() => setPending({ kind: 'deactivate', user: u })}>
                              Deactivate
                            </button>
                          ) : (
                            <button className="set-btn set-btn-secondary" disabled={busy} onClick={() => void act(u, () => reactivateAdminUser(u.id), `${u.email} can sign in again.`)}>
                              Reactivate
                            </button>
                          )}
                          <button className="set-btn set-btn-ghost" disabled={busy} onClick={() => setPending({ kind: 'revoke', user: u })}>
                            Sign out everywhere
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <CreateForm onCreated={() => void refresh()} onOutcome={setOutcome} />
        </>
      )}

      {pending?.kind === 'deactivate' && (
        <ConfirmDialog
          title={`Deactivate ${pending.user.email}`}
          description="They are signed out at once and cannot sign in again until you reactivate the account. Nothing they did is deleted. The last active administrator cannot be deactivated."
          confirmLabel="Deactivate"
          busyLabel="Deactivating…"
          tone="danger"
          phrase={pending.user.email}
          onConfirm={async () => {
            await deactivateAdminUser(pending.user.id)
            setOutcome({ ok: true, text: `${pending.user.email} is deactivated.` })
            await refresh()
          }}
          onClose={() => setPending(null)}
        />
      )}
      {pending?.kind === 'revoke' && (
        <ConfirmDialog
          title={`Sign ${pending.user.email} out everywhere`}
          description="Every session of theirs, on every device, ends. They stay signed in on a page that is already open for up to 15 minutes more; after that they must sign in again."
          confirmLabel="Sign out everywhere"
          busyLabel="Signing out…"
          tone="danger"
          onConfirm={async () => {
            const result = await revokeAdminUserSessions(pending.user.id)
            setOutcome({
              ok: true,
              text: `${result.revoked_count} session${result.revoked_count === 1 ? '' : 's'} of ${pending.user.email} ended.`,
            })
          }}
          onClose={() => setPending(null)}
        />
      )}
    </div>
  )
}
