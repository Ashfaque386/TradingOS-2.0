'use client'

import { useEffect, useState } from 'react'
import { type RegistrationStatus, getRegistrationStatus } from './api'

/** Whether anyone may register: only while the system has no accounts at all (the first one becomes the
 * administrator); after that an administrator creates accounts. 'unknown' covers both "still asking" and
 * "could not ask", and the pages offer nothing in either case rather than a link that may not work. */
export type RegistrationState = { kind: 'unknown' } | { kind: 'known'; status: RegistrationStatus }

export function useRegistrationStatus(): RegistrationState {
  const [state, setState] = useState<RegistrationState>({ kind: 'unknown' })

  useEffect(() => {
    let cancelled = false
    getRegistrationStatus()
      .then((status) => {
        if (!cancelled) setState({ kind: 'known', status })
      })
      .catch(() => {
        // the sign-in page works without it
      })
    return () => {
      cancelled = true
    }
  }, [])

  return state
}
