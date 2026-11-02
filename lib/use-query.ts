'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPoller } from './poller'

export interface QueryState<T> {
  /** The last successful result. Kept while a new `key` loads and when a later request fails, so the page does not blank. */
  data: T | null
  /** Why the latest request failed; null after any success. */
  error: string | null
  /** True from a new `key` until its first answer (or failure) arrives; a refetch of the same key does not set it. */
  loading: boolean
  /** When `data` was fetched. */
  lastUpdated: Date | null
  /** Fetch now. */
  refresh: () => Promise<void>
}

/**
 * A read that depends on what the person has chosen (a filter, a page). It fetches when `key`
 * changes, and an answer for an earlier key that arrives late is dropped, so slow requests can never
 * overwrite a newer one. With `intervalMs` it also refetches on that interval, one request at a time and
 * not while the tab is hidden (see `createPoller`). Pass `enabled: false` to hold off.
 */
export function useQuery<T>(
  key: string,
  fetcher: () => Promise<T>,
  { intervalMs, enabled = true }: { intervalMs?: number; enabled?: boolean } = {},
): QueryState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const fetcherRef = useRef(fetcher)
  const intervalRef = useRef(intervalMs ?? 0)
  const polling = (intervalMs ?? 0) > 0
  const refreshNowRef = useRef<() => Promise<void>>(async () => undefined)

  // Declared before the effect that fetches, so a fetch always runs the latest closure.
  useEffect(() => {
    fetcherRef.current = fetcher
    intervalRef.current = intervalMs ?? 0
  })

  useEffect(() => {
    if (!enabled) return
    let live = true // false once this key (or the page) has gone: whatever arrives now is stale
    const task = async () => {
      try {
        const result = await fetcherRef.current()
        if (!live) return
        setData(result)
        setError(null)
        setLastUpdated(new Date())
      } catch (err) {
        if (!live) return
        setError(err instanceof Error && err.message ? err.message : 'Could not load this')
      } finally {
        if (live) setLoadedKey(key)
      }
    }
    refreshNowRef.current = task
    if (!polling) {
      void task()
      return () => {
        live = false
      }
    }
    const poller = createPoller(task, { intervalMs: () => intervalRef.current, isHidden: () => document.hidden })
    const onVisible = () => {
      if (!document.hidden) poller.resume()
    }
    document.addEventListener('visibilitychange', onVisible)
    poller.start()
    return () => {
      live = false
      document.removeEventListener('visibilitychange', onVisible)
      poller.stop()
    }
  }, [key, enabled, polling])

  const refresh = useCallback(async () => {
    await refreshNowRef.current()
  }, [])

  return { data, error, loading: loadedKey !== key, lastUpdated, refresh }
}
