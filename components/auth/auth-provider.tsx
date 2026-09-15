'use client'

import { createContext, useContext, useMemo, useState } from 'react'

export const ROLES = ['SystemAdministrator', 'PortfolioManager', 'RiskManager', 'ReadOnlyAuditor'] as const
export type MockRole = (typeof ROLES)[number]

type AuthContextValue = {
  isAuthenticated: boolean
  role: MockRole
  setRole: (role: MockRole) => void
  signIn: (role: MockRole) => void
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [role, setRole] = useState<MockRole>('PortfolioManager')

  const value = useMemo(() => ({
    isAuthenticated,
    role,
    setRole,
    signIn: (nextRole: MockRole) => {
      setRole(nextRole)
      setIsAuthenticated(true)
    },
    signOut: () => setIsAuthenticated(false),
  }), [isAuthenticated, role])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside AuthProvider')
  return value
}

export function roleLabel(role: MockRole) {
  return role.replace(/([a-z])([A-Z])/g, '$1 $2')
}
