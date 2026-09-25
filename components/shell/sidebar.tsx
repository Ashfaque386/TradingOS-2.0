'use client'

import { usePathname, useRouter } from 'next/navigation'
import {
  Home,
  Zap,
  Network,
  Beaker,
  BarChart3,
  ArrowLeftRight,
  Activity,
  FileText,
  Shield,
  Settings,
  ChevronRight,
  MessageSquare,
} from 'lucide-react'

// Kept in sync by hand with components/shell/mobile-nav.tsx's own
// NAV_ITEMS (same list, same order) -- see that file's comment.
const NAV_ITEMS = [
  { label: 'Overview', icon: Home, href: '/' },
  { label: 'Mission Control', icon: Zap, href: '/mission-control' },
  { label: 'Chat', icon: MessageSquare, href: '/chat' },
  { label: 'Agent Fleet', icon: Network, href: '/agent-fleet' },
  { label: 'Strategies', icon: Beaker, href: '/strategies' },
  { label: 'Backtests', icon: BarChart3, href: '/backtests' },
  { label: 'Orders & Trades', icon: ArrowLeftRight, href: '/orders' },
  { label: 'Market Analysis', icon: Activity, href: '/analysis' },
  { label: 'Investor Reports', icon: FileText, href: '/investor-reports' },
  { label: 'Audit Log', icon: Shield, href: '/audit' },
  { label: 'Settings', icon: Settings, href: '/settings' },
]

interface SidebarProps {
  isCollapsed?: boolean
  onCollapsedChange?: (collapsed: boolean) => void
}

export function Sidebar({ isCollapsed = false, onCollapsedChange }: SidebarProps) {
  const router = useRouter()
  const pathname = usePathname()

  const isActive = (href: string) =>
    href === '/' ? pathname === '/' : pathname.startsWith(href)

  return (
    <aside
      className={`shell-sidebar fixed left-0 top-0 h-screen bg-[var(--glass-bg)] border-r border-[var(--glass-border)] transition-all duration-300 flex flex-col z-50 ${
        isCollapsed ? 'w-20' : 'w-64'
      }`}
      style={{ marginTop: '60px', height: 'calc(100vh - 60px)' }}
    >
      {/* Nav items */}
      <nav className="flex-1 overflow-y-auto p-4 space-y-2" aria-label="Primary">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          const active = isActive(item.href)
          return (
            <button
              key={item.href}
              onClick={() => router.push(item.href)}
              aria-current={active ? 'page' : undefined}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 ${
                active
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
        onClick={() => onCollapsedChange?.(!isCollapsed)}
        className="mx-4 mb-4 flex items-center justify-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground rounded-lg border border-[var(--glass-border)] hover:bg-white/5 transition-all duration-200"
        title={isCollapsed ? 'Expand' : 'Collapse'}
        aria-label={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
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
