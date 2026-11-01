'use client'

import { ErrorNotice } from '@/components/ui/error-notice'
import { useAuth, roleLabel } from '@/components/auth/auth-provider'
import { getSignInPolicy } from '@/lib/api'
import { usePolling } from '@/lib/use-polling'

// How this console keeps you signed in, and what a run of wrong passwords does. The numbers come from the
// server's own settings, not from this page, so they cannot drift from what is enforced.

export function SignInPolicyCard() {
  const { user } = useAuth()
  const { data, error, refresh } = usePolling(getSignInPolicy, { intervalMs: 600_000 })

  return (
    <div className="set-block mb-4" aria-label="Sign-in" data-testid="sign-in-policy">
      <div className="set-block-head">
        <div>
          <h2>Sign-in</h2>
          <p>How this console keeps a session, and what wrong passwords do.</p>
        </div>
      </div>

      {error && <ErrorNotice message={`Could not read the sign-in rules: ${error}`} onRetry={() => void refresh()} className="mb-3" />}

      <ul className="set-inbound-note list-disc space-y-1.5 pl-4">
        {user && (
          <li data-testid="signed-in-as">
            You are signed in as <strong>{user.email}</strong> ({roleLabel(user.role)}).
          </li>
        )}
        {data && (
          <>
            <li data-testid="session-rule">
              This page holds a short-lived access token in memory only, renewed every {data.access_token_minutes} minutes. What keeps
              you signed in is a cookie scripts on the page cannot read; it expires after {data.session_days} days, and each use of it
              replaces it.
            </li>
            <li data-testid="lock-rule">
              {data.max_failed_attempts} wrong passwords within {data.failure_window_minutes} minutes lock that account for{' '}
              {data.lockout_minutes} minutes, whichever address the guesses came from. An administrator can unlock it sooner.
            </li>
          </>
        )}
        <li>Nobody can register themselves once the first account exists. An administrator creates accounts under Users.</li>
      </ul>
    </div>
  )
}
