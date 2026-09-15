'use client'

import { useState } from 'react'
import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'
import { MobileNav } from './mobile-nav'
import { useAuth } from '@/components/auth/auth-provider'
import { usePreferences } from '@/components/providers/preferences-provider'

interface ShellLayoutProps {
  children: React.ReactNode
}

export function ShellLayout({ children }: ShellLayoutProps) {
  const { palette, setPalette, powerSave, setPowerSave } = usePreferences()
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false)
  const { role, setRole, signOut } = useAuth()

  return (
    <div className="shell-root min-h-screen bg-background">
      {/* Top Bar */}
      <TopBar
        palette={palette}
        onPaletteChange={setPalette}
        powerSave={powerSave}
        onPowerSaveChange={setPowerSave}
        marketHours="open"
        systemHealth="healthy"
        userName="Trader"
        role={role}
        onRoleChange={setRole}
        onLogout={signOut}
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
