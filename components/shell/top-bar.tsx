'use client'

import { useState } from 'react'
import {
  Search,
  Command,
  Circle,
  Clock,
  Palette,
  Moon,
  Sun,
  Battery,
  ChevronDown,
} from 'lucide-react'
import { ROLES, roleLabel, type MockRole } from '@/components/auth/auth-provider'
import { PALETTES } from '@/components/providers/preferences-provider'
import { OPEN_COMMAND_PALETTE_EVENT } from '@/components/shell/command-palette'

function openCommandPalette() {
  window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT))
}

interface TopBarProps {
  palette?: string
  onPaletteChange?: (palette: string) => void
  powerSave?: boolean
  onPowerSaveChange?: (enabled: boolean) => void
  marketHours?: 'open' | 'closed'
  systemHealth?: 'healthy' | 'degraded' | 'critical'
  userName?: string
  onUserMenu?: () => void
  role?: MockRole
  onRoleChange?: (role: MockRole) => void
  onLogout?: () => void
}

export function TopBar({
  palette = 'nominal',
  onPaletteChange,
  powerSave = false,
  onPowerSaveChange,
  marketHours = 'open',
  systemHealth = 'healthy',
  userName = 'User',
  onUserMenu,
  role = 'PortfolioManager',
  onRoleChange,
  onLogout,
}: TopBarProps) {
  const [showPaletteMenu, setShowPaletteMenu] = useState(false)
  const [showUserMenu, setShowUserMenu] = useState(false)

  const healthColor = {
    healthy: '#10b981',
    degraded: '#f59e0b',
    critical: '#ef4444',
  }[systemHealth]

  const currentPalette = PALETTES.find((p) => p.id === palette)

  return (
    <header className="fixed top-0 left-0 right-0 h-16 bg-[var(--glass-bg)] border-b border-[var(--glass-border)] flex items-center justify-between px-6 z-40">
      {/* Left: Logo/Brand */}
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-[var(--current-accent)]/20 border border-[var(--current-accent)]/50 flex items-center justify-center">
          <Zap className="w-4 h-4 text-[var(--current-accent)]" />
        </div>
        <span className="font-mono text-sm font-bold text-foreground hidden sm:inline">
          TradingOS 2.0
        </span>
      </div>

      {/* Center: Search/Command */}
      <div className="flex-1 max-w-md mx-6">
        <button
          onClick={openCommandPalette}
          aria-label="Open command palette"
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-white/5 border border-[var(--glass-border)] text-muted-foreground hover:text-foreground hover:bg-white/10 transition-all duration-200"
        >
          <Search className="w-4 h-4" />
          <span className="text-sm text-muted-foreground hidden sm:inline">
            Search or press
          </span>
          <kbd className="hidden sm:inline ml-auto px-1.5 py-0.5 text-xs bg-white/10 rounded border border-white/20">
            ⌘K
          </kbd>
        </button>
      </div>

      {/* Right: Controls */}
      <div className="flex items-center gap-3">
        {/* Health indicator */}
        <div
          className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-white/5 border border-[var(--glass-border)] text-xs font-medium text-muted-foreground"
          title={`System health: ${systemHealth}`}
        >
          <Circle
            className="w-2 h-2 fill-current"
            style={{ color: healthColor }}
          />
          <span className="hidden sm:inline capitalize">{systemHealth}</span>
        </div>

        {/* Market hours */}
        <div
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/5 border border-[var(--glass-border)] text-xs font-mono text-muted-foreground"
          title="Market hours (NSE/BSE IST)"
        >
          <Clock className="w-4 h-4" />
          <span className="hidden sm:inline uppercase">
            {marketHours === 'open' ? 'OPEN' : 'CLOSED'}
          </span>
        </div>

        {/* Palette switcher */}
        <div className="relative">
          <button
            onClick={() => setShowPaletteMenu(!showPaletteMenu)}
            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/5 border border-[var(--glass-border)] text-muted-foreground hover:text-foreground transition-all duration-200"
            title="Switch palette"
          >
            <Palette className="w-4 h-4" />
            <span className="hidden sm:inline text-xs font-medium capitalize">
              {currentPalette?.label}
            </span>
            <ChevronDown className="w-3 h-3" />
          </button>

          {showPaletteMenu && (
            <div className="absolute right-0 mt-2 w-40 bg-[var(--glass-bg)] border border-[var(--glass-border)] rounded-lg shadow-xl z-50">
              {PALETTES.map((p) => (
                <button
                  key={p.id}
                  onClick={() => {
                    onPaletteChange?.(p.id)
                    setShowPaletteMenu(false)
                  }}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-sm transition-all duration-200 ${
                    palette === p.id
                      ? 'bg-white/10 text-foreground'
                      : 'text-muted-foreground hover:bg-white/5'
                  }`}
                >
                  <div
                    className="w-3 h-3 rounded-full border border-white/20"
                    style={{ backgroundColor: p.color }}
                  />
                  {p.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Power save toggle */}
        <button
          onClick={() => onPowerSaveChange?.(!powerSave)}
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border transition-all duration-200 ${
            powerSave
              ? 'bg-[var(--current-accent)]/20 border-[var(--current-accent)]/50 text-[var(--current-accent)]'
              : 'bg-white/5 border-[var(--glass-border)] text-muted-foreground hover:text-foreground'
          }`}
          title="Power save mode"
        >
          <Battery className="w-4 h-4" />
        </button>

        {/* User menu */}
        <div className="relative">
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white/5 border border-[var(--glass-border)] text-muted-foreground hover:text-foreground transition-all duration-200"
          >
            <div className="w-6 h-6 rounded-full bg-[var(--current-accent)]/20 border border-[var(--current-accent)]/50" />
            <span className="text-sm font-medium hidden sm:inline">{userName}</span>
            <ChevronDown className="w-3 h-3" />
          </button>

          {showUserMenu && (
            <div className="absolute right-0 mt-2 w-48 bg-[var(--glass-bg)] border border-[var(--glass-border)] rounded-lg shadow-xl z-50">
              <div className="px-3 py-2 border-b border-[var(--glass-border)] text-xs text-muted-foreground">
                <span className="block">{userName}</span>
                <span className="mt-1 block font-mono text-[9px] uppercase tracking-wider text-cyan-300/70">{roleLabel(role)}</span>
              </div>
              <div className="border-b border-[var(--glass-border)] px-3 py-2">
                <span className="eyebrow text-[8px]">DEV ROLE SWITCHER</span>
                <div className="mt-2 flex flex-col gap-1">
                  {ROLES.map((item) => <button key={item} onClick={() => onRoleChange?.(item)} className={`w-full rounded px-2 py-1.5 text-left text-[11px] transition-colors ${role === item ? 'bg-cyan-300/15 text-cyan-200' : 'text-muted-foreground hover:bg-white/5 hover:text-foreground'}`}>{roleLabel(item)}</button>)}
                </div>
              </div>
              <button className="w-full text-left px-3 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-white/5 transition-all duration-200">
                Profile
              </button>
              <button className="w-full text-left px-3 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-white/5 transition-all duration-200">
                Settings
              </button>
              <button onClick={onLogout} className="w-full text-left px-3 py-2 text-sm text-destructive hover:bg-white/5 transition-all duration-200 border-t border-[var(--glass-border)]">
                Logout
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}

function Zap({ className }: { className: string }) {
  return (
    <svg
      className={className}
      fill="currentColor"
      viewBox="0 0 24 24"
    >
      <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
    </svg>
  )
}
