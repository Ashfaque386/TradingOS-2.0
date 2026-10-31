import { ApiError } from '@/lib/api'

/**
 * What to tell a person whose sign-in or sign-up the server refused for a reason other than
 * "wrong credentials" / "already exists": too many attempts, a locked account, a server fault.
 * Returns null for anything that is not an ApiError (the server could not be reached at all).
 */
export function refusalMessage(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null
  if (err.status === 429) {
    return err.retryAfterSeconds
      ? `Too many attempts. Try again in ${err.retryAfterSeconds} seconds.`
      : 'Too many attempts. Wait a minute and try again.'
  }
  if (err.status === 423 || err.status === 403) return err.message || 'This account cannot sign in right now.'
  if (err.status >= 500) return `The server had a problem (${err.status}). Try again shortly.`
  return err.message || null
}
