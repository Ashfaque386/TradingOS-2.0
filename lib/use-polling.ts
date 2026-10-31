'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { type Poller, createPoller } from './poller'

export interface PollingState<T> {
  /** The last successful result. Kept when a later request fails, so a blip does not blank the page. */
  data: T | null
  /** Why the latest request failed; null after any success. */
  error: string | null
  /** True until the first request finishes. */
  loading: boolean
  /** When `data` was fetched. */
  lastUpdated: Date | null
  /** Fetch now (joins a request already in flight). */
  refresh: () => Promise<void>
}

/**
 * Fetch on mount and then every `intervalMs`, one request at a time, pausing while the tab is hidden
 * and catching up the moment it is shown again. `intervalMs` may change between renders (a desk polls
 * faster while the market is open) and takes effect from the next tick. Pass `enabled: false` to hold off
 * (for example until the session is known).
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  { intervalMs, enabled = true }: { intervalMs: number; enabled?: boolean },
): PollingState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const intervalRef = useRef(intervalMs)
  intervalRef.current = intervalMs
  const pollerRef = useRef<Poller | null>(null)

  useEffect(() => {
    if (!enabled) return
    let live = true
    const poller = createPoller(
      async () => {
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
          if (live) setLoading(false)
        }
      },
      { intervalMs: () => intervalRef.current, isHidden: () => document.hidden },
    )
    pollerRef.current = poller
    const onVisible = () => {
      if (!document.hidden) poller.resume()
    }
    document.addEventListener('visibilitychange', onVisible)
    poller.start()
    return () => {
      live = false
      document.removeEventListener('visibilitychange', onVisible)
      poller.stop()
      pollerRef.current = null
    }
  }, [enabled])

  const refresh = useCallback(async () => {
    await pollerRef.current?.refresh()
  }, [])

  return { data, error, loading, lastUpdated, refresh }
}
