import { ApiError } from '@/lib/api'

/** The words to show for a failed call: the server's own reason when it gave one, else `fallback`. */
export function messageOf(err: unknown, fallback: string): string {
  return err instanceof ApiError && err.message ? err.message : fallback
}
