'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'
import { MobileNav } from './mobile-nav'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'
import type { Role } from '@/lib/api'
import { ConsoleStateProvider, useConsoleState } from './console-state'
import { KillSwitchBanner } from './kill-switch-banner'

interface ShellLayoutProps {
  children: React.ReactNode
}

export function ShellLayout({ children }: ShellLayoutProps) {
  const { palette, setPalette, powerSave, setPowerSave } = usePreferences()
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false)
  const { user, role, isAuthenticated, isLoading, isUnreachable, retrySession, signOut } = useAuth()
  const router = useRouter()

  useEffect(() => {
    // An unreachable server is not a signed-out session: stored tokens are
    // still good, so stay put (the screen below retries) rather than
    // sending the operator to log in again.
    if (!isLoading && !isAuthenticated && !isUnreachable) router.replace('/login')
  }, [isLoading, isAuthenticated, isUnreachable, router])

  if (isUnreachable) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center p-6">
        <div role="alert" className="max-w-md text-center space-y-3">
          <h1 className="text-xl font-semibold">Can&apos;t reach the TradingOS server</h1>
          <p className="text-sm text-muted-foreground">
            Your session is still saved. Retrying automatically every few seconds.
          </p>
          <Button onClick={retrySession}>Retry now</Button>
        </div>
      </div>
    )
  }

  if (isLoading || !isAuthenticated) return null

  return (
    <ConsoleStateProvider>
      <ShellFrame
        palette={palette}
        setPalette={setPalette}
        powerSave={powerSave}
        setPowerSave={setPowerSave}
        sidebarCollapsed={sidebarCollapsed}
        setSidebarCollapsed={setSidebarCollapsed}
        userName={user?.email ?? 'User'}
        role={role}
        onLogout={() => {
          void signOut()
          router.replace('/login')
        }}
      >
        {children}
      </ShellFrame>
    </ConsoleStateProvider>
  )
}

function ShellFrame({
  palette,
  setPalette,
  powerSave,
  setPowerSave,
  sidebarCollapsed,
  setSidebarCollapsed,
  userName,
  role,
  onLogout,
  children,
}: {
  palette: string
  setPalette: (palette: string) => void
  powerSave: boolean
  setPowerSave: (enabled: boolean) => void
  sidebarCollapsed: boolean
  setSidebarCollapsed: (collapsed: boolean) => void
  userName: string
  role: Role
  onLogout: () => void
  children: React.ReactNode
}) {
  const { marketHours, killSwitch, engineAlerts, loading } = useConsoleState()
  // A switch we could not read is not a switch that is armed: show it as degraded, not healthy.
  const systemHealth =
    killSwitch.paper?.tripped || killSwitch.live?.tripped
      ? 'critical'
      : killSwitch.paper === null || killSwitch.live === null
        ? loading
          ? 'checking'
          : 'degraded'
        : 'healthy'

  return (
    <div className="shell-root min-h-screen bg-background">
      {/* Top Bar */}
      <TopBar
        palette={palette}
        onPaletteChange={setPalette}
        powerSave={powerSave}
        onPowerSaveChange={setPowerSave}
        marketHours={marketHours === null ? (loading ? 'checking' : 'unknown') : marketHours.is_open ? 'open' : 'closed'}
        systemHealth={systemHealth}
        engineAlerts={engineAlerts}
        userName={userName}
        role={role}
        onLogout={onLogout}
      />

      {/* Sidebar (desktop) */}
      <Sidebar isCollapsed={sidebarCollapsed} onCollapsedChange={setSidebarCollapsed} />

      {/* Main Content */}
      <main
        className="shell-main transition-all duration-300"
        data-collapsed={sidebarCollapsed}
      >
        <div className="shell-main-inner">
          <KillSwitchBanner role={role} />
          {children}
        </div>
      </main>

      {/* Bottom tab bar + drawer (mobile) */}
      <MobileNav />
    </div>
  )
}
