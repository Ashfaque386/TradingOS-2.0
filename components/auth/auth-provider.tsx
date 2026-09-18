'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import {
  type CurrentUser,
  type Role,
  getMe,
  isAuthenticated as hasStoredToken,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
} from '@/lib/api'

export const ROLES: Role[] = ['SystemAdministrator', 'PortfolioManager', 'RiskManager', 'ReadOnlyAuditor']

const FALLBACK_ROLE: Role = 'ReadOnlyAuditor'

type AuthContextValue = {
  isAuthenticated: boolean
  isLoading: boolean
  user: CurrentUser | null
  role: Role
  signIn: (email: string, password: string) => Promise<void>
  signUp: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)
  // Starts true so a page wrapped in ShellLayout doesn't flash its
  // protected content (or bounce to /login) before a stored refresh token
  // has had a chance to resolve into a real session.
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function rehydrate() {
      if (!hasStoredToken()) {
        setIsLoading(false)
        return
      }
      try {
        const me = await getMe()
        if (!cancelled) setUser(me)
      } catch {
        // Stored access token is expired/invalid and the refresh (tried
        // automatically inside getMe's underlying fetch) also failed --
        // there is no valid session to rehydrate.
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    rehydrate()
    return () => {
      cancelled = true
    }
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    const me = await apiLogin(email, password)
    setUser(me)
  }, [])

  const signUp = useCallback(async (email: string, password: string) => {
    const me = await apiRegister(email, password)
    setUser(me)
  }, [])

  const signOut = useCallback(async () => {
    await apiLogout()
    setUser(null)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: user !== null,
      isLoading,
      user,
      role: user?.role ?? FALLBACK_ROLE,
      signIn,
      signUp,
      signOut,
    }),
    [user, isLoading, signIn, signUp, signOut],
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
