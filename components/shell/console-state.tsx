'use client'

import { createContext, useCallback, useContext, useMemo } from 'react'
import { type KillSwitchState, type MarketHours, getKillSwitch, getMarketHours } from '@/lib/api'
import { usePolling } from '@/lib/use-polling'

const POLL_MS = 30_000

interface Snapshot {
  marketHours: MarketHours | null
  paper: KillSwitchState | null
  live: KillSwitchState | null
}

export interface ConsoleState {
  /** Null until read, and null again when the last read failed: an unknown is never shown as "open" or "armed". */
  marketHours: MarketHours | null
  killSwitch: { paper: KillSwitchState | null; live: KillSwitchState | null }
  /** Set when none of the three reads succeeded. */
  error: string | null
  loading: boolean
  /** Read again now, e.g. after a reset or when the activity feed reports a trip. */
  refresh: () => Promise<void>
}

const ConsoleStateContext = createContext<ConsoleState | null>(null)

async function readSnapshot(): Promise<Snapshot> {
  const [hours, paper, live] = await Promise.allSettled([
    getMarketHours(),
    getKillSwitch('paper'),
    getKillSwitch('live'),
  ])
  if (hours.status === 'rejected' && paper.status === 'rejected' && live.status === 'rejected') {
    throw hours.reason
  }
  return {
    marketHours: hours.status === 'fulfilled' ? hours.value : null,
    paper: paper.status === 'fulfilled' ? paper.value : null,
    live: live.status === 'fulfilled' ? live.value : null,
  }
}

/**
 * What every page needs to know about the market and the kill switches, read once here instead of by
 * each page on its own timer. Mounted by the console shell, so it only runs while signed in.
 */
export function ConsoleStateProvider({ children }: { children: React.ReactNode }) {
  const { data, error, loading, refresh } = usePolling(readSnapshot, { intervalMs: POLL_MS })

  const refreshNow = useCallback(() => refresh(), [refresh])
  const value = useMemo<ConsoleState>(() => {
    // The last good snapshot is kept for a blip, but once a whole read fails it is no longer what we know.
    const known = error ? null : data
    return {
      marketHours: known?.marketHours ?? null,
      killSwitch: { paper: known?.paper ?? null, live: known?.live ?? null },
      error,
      loading,
      refresh: refreshNow,
    }
  }, [data, error, loading, refreshNow])

  return <ConsoleStateContext.Provider value={value}>{children}</ConsoleStateContext.Provider>
}

export function useConsoleState(): ConsoleState {
  const value = useContext(ConsoleStateContext)
  if (!value) throw new Error('useConsoleState must be used inside the console shell')
  return value
}
