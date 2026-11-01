'use client'

import { createContext, useCallback, useContext, useMemo } from 'react'
import { type KillSwitchState, type MarketHours, getKillSwitch, getMarketHours, listEngineAlerts } from '@/lib/api'
import { usePolling } from '@/lib/use-polling'

const POLL_MS = 30_000

interface Snapshot {
  marketHours: MarketHours | null
  paper: KillSwitchState | null
  live: KillSwitchState | null
  engineAlerts: EngineAlertCount | null
}

/** Engine alerts that are open right now, across every source. */
export interface EngineAlertCount {
  open: number
  critical: number
}

export interface ConsoleState {
  /** Null until read, and null again when the last read failed: an unknown is never shown as "open" or "armed". */
  marketHours: MarketHours | null
  killSwitch: { paper: KillSwitchState | null; live: KillSwitchState | null }
  /** Null when it could not be read: an unknown count is not shown as zero. */
  engineAlerts: EngineAlertCount | null
  /** Set when none of the three reads succeeded. */
  error: string | null
  loading: boolean
  /** Read again now, e.g. after a reset or when the activity feed reports a trip. */
  refresh: () => Promise<void>
}

const ConsoleStateContext = createContext<ConsoleState | null>(null)

async function readSnapshot(): Promise<Snapshot> {
  const [hours, paper, live, alerts] = await Promise.allSettled([
    getMarketHours(),
    getKillSwitch('paper'),
    getKillSwitch('live'),
    listEngineAlerts('open', 1),
  ])
  if (hours.status === 'rejected' && paper.status === 'rejected' && live.status === 'rejected') {
    throw hours.reason
  }
  return {
    marketHours: hours.status === 'fulfilled' ? hours.value : null,
    paper: paper.status === 'fulfilled' ? paper.value : null,
    live: live.status === 'fulfilled' ? live.value : null,
    engineAlerts:
      alerts.status === 'fulfilled'
        ? { open: alerts.value.open_count, critical: alerts.value.critical_open_count }
        : null,
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
      engineAlerts: known?.engineAlerts ?? null,
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
