'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import type { LucideIcon } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { navFor } from '@/lib/search-index'
import {
  Home,
  Zap,
  Network,
  Beaker,
  BarChart3,
  ArrowLeftRight,
  PieChart,
  Activity,
  FileText,
  Shield,
  Settings,
  ChevronRight,
  MessageSquare,
  Landmark,
} from 'lucide-react'

// The list of pages is lib/search-index.ts's NAV_TARGETS (shared with the mobile nav and the command
// palette); this only says which icon goes with which page.
const NAV_ICONS: Record<string, LucideIcon> = {
  '/': Home,
  '/mission-control': Zap,
  '/chat': MessageSquare,
  '/agent-fleet': Network,
  '/strategies': Beaker,
  '/backtests': BarChart3,
  '/orders': ArrowLeftRight,
  '/paper-trading': Landmark,
  '/portfolio': PieChart,
  '/analysis': Activity,
  '/investor-reports': FileText,
  '/audit': Shield,
  '/settings': Settings,
}

interface SidebarProps {
  isCollapsed?: boolean
  onCollapsedChange?: (collapsed: boolean) => void
}

export function Sidebar({ isCollapsed = false, onCollapsedChange }: SidebarProps) {
  const pathname = usePathname()
  const { role } = useAuth()
  const navItems = navFor(role).map((target) => ({ ...target, icon: NAV_ICONS[target.href] ?? Home }))

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
        {navItems.map((item) => {
          const Icon = item.icon
          const active = isActive(item.href)
          return (
            // A link, not a button: it can be opened in a new tab, copied and middle-clicked.
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? 'page' : undefined}
              aria-label={item.label}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 ${
                active
                  ? 'bg-[var(--current-accent)]/20 text-[var(--current-accent)] border border-[var(--current-accent)]/50'
                  : 'text-muted-foreground hover:text-foreground hover:bg-white/5'
              }`}
              title={item.label}
            >
              <Icon className="w-5 h-5 flex-shrink-0" />
              {!isCollapsed && <span className="text-sm font-medium">{item.label}</span>}
            </Link>
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
