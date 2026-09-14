'use client'

import { useEffect, useState } from 'react'
import { Sidebar } from './sidebar'
import { TopBar } from './top-bar'

interface ShellLayoutProps {
  children: React.ReactNode
}

export function ShellLayout({ children }: ShellLayoutProps) {
  const [palette, setPalette] = useState<string>('nominal')
  const [powerSave, setPowerSave] = useState<boolean>(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false)
  const [activeNav, setActiveNav] = useState<string>('overview')

  // Persist theme and power-save to localStorage
  useEffect(() => {
    const savedPalette = localStorage.getItem('tradingos-palette')
    const savedPowerSave = localStorage.getItem('tradingos-power-save')

    if (savedPalette) setPalette(savedPalette)
    if (savedPowerSave) setPowerSave(JSON.parse(savedPowerSave))
  }, [])

  const handlePaletteChange = (newPalette: string) => {
    setPalette(newPalette)
    localStorage.setItem('tradingos-palette', newPalette)
  }

  const handlePowerSaveChange = (enabled: boolean) => {
    setPowerSave(enabled)
    localStorage.setItem('tradingos-power-save', JSON.stringify(enabled))
  }

  return (
    <div
      className="min-h-screen bg-background"
      data-palette={palette}
      data-power-save={powerSave}
      style={
        {
          '--current-accent': palette === 'nominal' 
            ? '#06b6d4'
            : palette === 'caution'
              ? '#f59e0b'
              : palette === 'risk'
                ? '#ef4444'
                : palette === 'neutral'
                  ? '#6b7280'
                  : palette === 'contrast'
                    ? '#ffffff'
                    : '#1f2937',
        } as React.CSSProperties
      }
    >
      {/* Top Bar */}
      <TopBar
        palette={palette}
        onPaletteChange={handlePaletteChange}
        powerSave={powerSave}
        onPowerSaveChange={handlePowerSaveChange}
        marketHours="open"
        systemHealth="healthy"
        userName="Trader"
      />

      {/* Sidebar */}
      <Sidebar
        isCollapsed={sidebarCollapsed}
        onCollapsedChange={setSidebarCollapsed}
        activeId={activeNav}
        onNavClick={setActiveNav}
      />

      {/* Main Content */}
      <main
        className="transition-all duration-300"
        style={{
          marginLeft: sidebarCollapsed ? '80px' : '256px',
          marginTop: '60px',
          minHeight: 'calc(100vh - 60px)',
        }}
      >
        <div className="p-6">{children}</div>
      </main>
    </div>
  )
}
