'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'
import { MobileNav } from './mobile-nav'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'
import { getKillSwitch, getMarketHours } from '@/lib/api'

interface ShellLayoutProps {
  children: React.ReactNode
}

export function ShellLayout({ children }: ShellLayoutProps) {
  const { palette, setPalette, powerSave, setPowerSave } = usePreferences()
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false)
  const { user, role, isAuthenticated, isLoading, isUnreachable, retrySession, signOut } = useAuth()
  const router = useRouter()
  const [marketOpen, setMarketOpen] = useState(true)
  const [systemHealth, setSystemHealth] = useState<'healthy' | 'critical'>('healthy')

  useEffect(() => {
    // An unreachable server is not a signed-out session: stored tokens are
    // still good, so stay put (the screen below retries) rather than
    // sending the operator to log in again.
    if (!isLoading && !isAuthenticated && !isUnreachable) router.replace('/login')
  }, [isLoading, isAuthenticated, isUnreachable, router])

  useEffect(() => {
    if (!isAuthenticated) return
    let cancelled = false
    async function poll() {
      const [hours, paperKs, liveKs] = await Promise.allSettled([
        getMarketHours(),
        getKillSwitch('paper'),
        getKillSwitch('live'),
      ])
      if (cancelled) return
      if (hours.status === 'fulfilled') setMarketOpen(hours.value.is_open)
      const tripped =
        (paperKs.status === 'fulfilled' && paperKs.value.tripped) ||
        (liveKs.status === 'fulfilled' && liveKs.value.tripped)
      setSystemHealth(tripped ? 'critical' : 'healthy')
    }
    poll()
    const interval = setInterval(poll, 30000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [isAuthenticated])

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
    <div className="shell-root min-h-screen bg-background">
      {/* Top Bar */}
      <TopBar
        palette={palette}
        onPaletteChange={setPalette}
        powerSave={powerSave}
        onPowerSaveChange={setPowerSave}
        marketHours={marketOpen ? 'open' : 'closed'}
        systemHealth={systemHealth}
        userName={user?.email ?? 'User'}
        role={role}
        onLogout={() => {
          void signOut()
          router.replace('/login')
        }}
      />

      {/* Sidebar (desktop) */}
      <Sidebar isCollapsed={sidebarCollapsed} onCollapsedChange={setSidebarCollapsed} />

      {/* Main Content */}
      <main
        className="shell-main transition-all duration-300"
        data-collapsed={sidebarCollapsed}
      >
        <div className="shell-main-inner">{children}</div>
      </main>

      {/* Bottom tab bar + drawer (mobile) */}
      <MobileNav />
    </div>
  )
}
