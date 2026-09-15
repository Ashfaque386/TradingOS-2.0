'use client'

import { useRouter } from 'next/navigation'
import {
  Home,
  Zap,
  Network,
  Beaker,
  BarChart3,
  ArrowLeftRight,
  Activity,
  Shield,
  Settings,
  ChevronRight,
} from 'lucide-react'

const NAV_ITEMS = [
  { id: 'overview', label: 'Overview', icon: Home },
  { id: 'mission-control', label: 'Mission Control', icon: Zap },
  { id: 'agents', label: 'Agent Fleet', icon: Network },
  { id: 'strategies', label: 'Strategies', icon: Beaker },
  { id: 'backtests', label: 'Backtests', icon: BarChart3 },
  { id: 'orders', label: 'Orders & Trades', icon: ArrowLeftRight },
  { id: 'analysis', label: 'Market Analysis', icon: Activity },
  { id: 'audit', label: 'Audit Log', icon: Shield },
  { id: 'settings', label: 'Settings', icon: Settings },
]

interface SidebarProps {
  isCollapsed?: boolean
  onCollapsedChange?: (collapsed: boolean) => void
  activeId?: string
  onNavClick?: (id: string) => void
}

export function Sidebar({
  isCollapsed = false,
  onCollapsedChange,
  activeId = 'overview',
  onNavClick,
}: SidebarProps) {
  const router = useRouter()
  const handleToggle = () => {
    onCollapsedChange?.(!isCollapsed)
  }

  return (
    <aside
      className={`fixed left-0 top-0 h-screen bg-[var(--glass-bg)] border-r border-[var(--glass-border)] transition-all duration-300 flex flex-col z-50 ${
        isCollapsed ? 'w-20' : 'w-64'
      }`}
      style={{ marginTop: '60px', height: 'calc(100vh - 60px)' }}
    >
      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto p-4 space-y-2">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          const isActive = activeId === item.id
          return (
            <button
              key={item.id}
              onClick={() => { onNavClick?.(item.id); if (item.id === 'mission-control') router.push('/mission-control'); if (item.id === 'agents') router.push('/agent-fleet'); if (item.id === 'strategies') router.push('/strategies'); if (item.id === 'overview') router.push('/'); }}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 ${
                isActive
                  ? 'bg-[var(--current-accent)]/20 text-[var(--current-accent)] border border-[var(--current-accent)]/50'
                  : 'text-muted-foreground hover:text-foreground hover:bg-white/5'
              }`}
              title={item.label}
            >
              <Icon className="w-5 h-5 flex-shrink-0" />
              {!isCollapsed && <span className="text-sm font-medium">{item.label}</span>}
            </button>
          )
        })}
      </nav>

      {/* Collapse toggle */}
      <button
        onClick={handleToggle}
        className="mx-4 mb-4 flex items-center justify-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground rounded-lg border border-[var(--glass-border)] hover:bg-white/5 transition-all duration-200"
        title={isCollapsed ? 'Expand' : 'Collapse'}
      >
        <ChevronRight
          className={`w-4 h-4 transition-transform duration-200 ${
            isCollapsed ? 'rotate-0' : 'rotate-180'
          }`}
        />
        {!isCollapsed && <span>Collapse</span>}
      </button>
    </aside>
  )
}
