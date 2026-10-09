'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import {
  type CurrentUser,
  type Role,
  getMe,
  isAuthenticated as hasStoredToken,
  isTransientError,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
} from '@/lib/api'

export const ROLES: Role[] = ['SystemAdministrator', 'PortfolioManager', 'RiskManager', 'ReadOnlyAuditor']

const FALLBACK_ROLE: Role = 'ReadOnlyAuditor'

type AuthContextValue = {
  isAuthenticated: boolean
  isLoading: boolean
  // True when stored tokens exist but the server couldn't be reached to
  // confirm them (rate-limited, erroring, offline) even after retries. The
  // session is NOT over -- the shell shows a "can't reach the server"
  // screen instead of bouncing the operator to /login.
  isUnreachable: boolean
  user: CurrentUser | null
  role: Role
  signIn: (email: string, password: string) => Promise<void>
  signUp: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  retrySession: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

type SessionState = 'loading' | 'authenticated' | 'unauthenticated' | 'unreachable'

// Quick retries on page load cover a momentary blip (a rate-limit window
// rolling over, a restarting backend) without the operator ever noticing;
// past that the shell takes over with a visible retry loop.
const REHYDRATE_RETRY_DELAYS_MS = [500, 1500, 3000]
const UNREACHABLE_AUTO_RETRY_MS = 10_000

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  // Starts 'loading' so a page wrapped in ShellLayout doesn't flash its
  // protected content (or bounce to /login) before a stored refresh token
  // has had a chance to resolve into a real session.
  const [sessionState, setSessionState] = useState<SessionState>('loading')

  const rehydrate = useCallback(async (isCancelled: () => boolean) => {
    if (!hasStoredToken()) {
      setSessionState('unauthenticated')
      return
    }
    setSessionState('loading')
    for (let attempt = 0; ; attempt++) {
      try {
        const me = await getMe()
        if (isCancelled()) return
        setUser(me)
        setSessionState('authenticated')
        return
      } catch (err) {
        if (isCancelled()) return
        // A real answer (the token is expired/invalid and the refresh was
        // refused too) means there is no session to restore. Anything else
        // -- 429, 5xx, no response -- says nothing about the session, so
        // never treat it as signed out.
        if (!isTransientError(err)) {
          setSessionState('unauthenticated')
          return
        }
        if (attempt >= REHYDRATE_RETRY_DELAYS_MS.length) {
          setSessionState('unreachable')
          return
        }
        await sleep(REHYDRATE_RETRY_DELAYS_MS[attempt])
        if (isCancelled()) return
      }
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    void rehydrate(() => cancelled)
    return () => {
      cancelled = true
    }
  }, [rehydrate])

  // While the server is unreachable, keep trying in the background so the
  // console comes back by itself the moment it can.
  useEffect(() => {
    if (sessionState !== 'unreachable') return
    let cancelled = false
    const timer = setTimeout(() => {
      void rehydrate(() => cancelled)
    }, UNREACHABLE_AUTO_RETRY_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [sessionState, rehydrate])

  const retrySession = useCallback(() => {
    void rehydrate(() => false)
  }, [rehydrate])

  const signIn = useCallback(async (email: string, password: string) => {
    const me = await apiLogin(email, password)
    setUser(me)
    setSessionState('authenticated')
  }, [])

  const signUp = useCallback(async (email: string, password: string) => {
    const me = await apiRegister(email, password)
    setUser(me)
    setSessionState('authenticated')
  }, [])

  const signOut = useCallback(async () => {
    await apiLogout()
    setUser(null)
    setSessionState('unauthenticated')
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: user !== null,
      isLoading: sessionState === 'loading',
      isUnreachable: sessionState === 'unreachable' && user === null,
      user,
      role: user?.role ?? FALLBACK_ROLE,
      signIn,
      signUp,
      signOut,
      retrySession,
    }),
    [user, sessionState, signIn, signUp, signOut, retrySession],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside AuthProvider')
  return value
}

export function roleLabel(role: Role) {
  return role.replace(/([a-z])([A-Z])/g, '$1 $2')
}
