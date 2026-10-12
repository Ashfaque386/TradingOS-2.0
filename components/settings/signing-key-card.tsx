'use client'

import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, KeyRound, RefreshCw } from 'lucide-react'
import { type JwtSigningKeyStatus, ApiError, getJwtSigningKeyStatus, rotateJwtSigningKey } from '@/lib/api'
import { useAuth } from '@/components/auth/auth-provider'

// The key that signs every session. A deployment started with the defaults used
// to be seeded with a public placeholder (and the database copy wins over .env
// from then on), which made every login token forgeable. This card shows where
// the key came from and whether it can be trusted -- never the key itself -- and
// rotates it. Rotating is deliberately disruptive: every session, this one
// included, is signed out, so it sits behind an explicit confirm step.

const when = (iso: string) => new Date(iso).toLocaleString()

export function SigningKeyCard() {
  const { role, signOut } = useAuth()
  const isAdmin = role === 'SystemAdministrator'
  const [status, setStatus] = useState<JwtSigningKeyStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [rotated, setRotated] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      setStatus(await getJwtSigningKeyStatus())
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not read the signing key status')
    }
  }, [])

  useEffect(() => {
    if (isAdmin) void load()
  }, [isAdmin, load])

  async function rotate() {
    setBusy(true)
    setError(null)
    try {
      const result = await rotateJwtSigningKey()
      setRotated(result.sessions_revoked)
      setConfirming(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Rotation failed')
    } finally {
      setBusy(false)
    }
  }

  async function signInAgain() {
    try {
      await signOut()
    } catch {
      // the old session is already invalid -- that is the point of rotating
    }
    window.location.assign('/login')
  }

  return (
    <div className="set-block" aria-label="JWT signing key">
      <div className="set-block-head">
        <div>
          <h2>Signing key</h2>
          <p>The secret that signs every login token. Its value is never shown.</p>
        </div>
      </div>

      {!isAdmin && <p className="text-xs text-muted-foreground">Read-only — SystemAdministrator role required.</p>}
      {error && <div role="alert" className="mb-3 rounded-lg border border-rose-400/30 bg-rose-400/10 p-3 text-xs text-rose-200">{error}</div>}

      {isAdmin && rotated !== null && (
        <div role="status" className="mb-3 rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-3 text-xs text-emerald-200">
          Rotated. {rotated} refresh token{rotated === 1 ? '' : 's'} revoked and every session — this one too — is now signed out.
          <div className="mt-2"><button className="set-btn set-btn-primary" onClick={signInAgain}>Sign in again</button></div>
        </div>
      )}

      {isAdmin && status && rotated === null && (
        <>
          {status.weak ? (
            <div role="alert" className="set-dualcontrol-banner">
              <AlertTriangle className="size-4" />
              <div>
                <strong>This key can be guessed</strong>
                <p>It was created by <code>{status.created_by}</code> and {status.weakness}. Anyone who has read the repository can forge a login token. Rotate it.</p>
              </div>
            </div>
          ) : (
            <div className="set-dualcontrol-banner">
              <CheckCircle2 className="size-4" />
              <div>
                <strong>Key looks strong</strong>
                <p>Created {when(status.created_at)} by <code>{status.created_by}</code>.</p>
              </div>
            </div>
          )}

          {!confirming ? (
            <div className="mt-4">
              <button className={`set-btn ${status.weak ? 'set-btn-primary' : 'set-btn-secondary'}`} onClick={() => setConfirming(true)}>
                <RefreshCw className="size-3.5" /> Rotate signing key
              </button>
            </div>
          ) : (
            <div className="mt-4 rounded-xl border border-rose-400/30 p-3" role="group" aria-label="Confirm rotation">
              <p className="text-xs"><KeyRound className="mr-1 inline size-3.5" />
                This signs <strong>everyone</strong> out immediately, you included — every access token stops working and every refresh token is revoked. There is no grace period.
              </p>
              <div className="mt-3 flex gap-2">
                <button className="set-btn set-btn-danger" disabled={busy} onClick={rotate}>Rotate now</button>
                <button className="set-btn set-btn-ghost" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
