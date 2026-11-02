import { ApiError } from '@/lib/api'

/** The words to show for a failed call: the server's own reason when it gave one, else `fallback`. */
export function messageOf(err: unknown, fallback: string): string {
  return err instanceof ApiError && err.message ? err.message : fallback
}

/** One read of several a page makes together: what came back, or why it did not. */
export type Section<T> = { data: T | null; error: string | null }

/** Runs one read so that its failure is a value, not a throw, and the page's other reads still show.
 * `what` names the thing in the sentence ("the portfolio summary"); the server's reason follows. */
export async function section<T>(read: () => Promise<T>, what: string): Promise<Section<T>> {
  try {
    return { data: await read(), error: null }
  } catch (err) {
    return { data: null, error: `Could not load ${what}: ${messageOf(err, 'the request failed')}` }
  }
}
