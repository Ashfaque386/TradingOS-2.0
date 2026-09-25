'use client'

import { useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import {
  Activity,
  ArrowLeftRight,
  BarChart3,
  Beaker,
  FileText,
  Home,
  Menu,
  MessageSquare,
  Network,
  Settings,
  Shield,
  X,
  Zap,
} from 'lucide-react'

// Kept in sync by hand with components/shell/sidebar.tsx's own NAV_ITEMS
// (same list, same order) -- Phase 19's own audit found the org-chart
// department list had drifted from its backend source of truth exactly
// this way; this is the frontend-nav equivalent of that same class of bug,
// caught before it happened rather than after.
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

// Primary destinations pinned to the bottom tab bar; the rest live behind "More".
const PRIMARY = NAV_ITEMS.slice(0, 4)

export function MobileNav() {
  const router = useRouter()
  const pathname = usePathname()
  const [drawerOpen, setDrawerOpen] = useState(false)

  const isActive = (href: string) => (href === '/' ? pathname === '/' : pathname.startsWith(href))

  const navigate = (href: string) => {
    router.push(href)
    setDrawerOpen(false)
  }

  return (
    <>
      {drawerOpen && (
        <div className="mobile-drawer-overlay" role="button" tabIndex={-1} aria-label="Close menu" onClick={() => setDrawerOpen(false)}>
          <nav className="mobile-drawer" aria-label="All sections" onClick={(event) => event.stopPropagation()}>
            <div className="mobile-drawer-head">
              <span className="eyebrow">ALL SECTIONS</span>
              <button className="icon-button" aria-label="Close menu" onClick={() => setDrawerOpen(false)}>
                <X className="size-4" />
              </button>
            </div>
            <div className="mobile-drawer-list">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon
                const active = isActive(item.href)
                return (
                  <button
                    key={item.href}
                    className={`mobile-drawer-item ${active ? 'mobile-drawer-item-active' : ''}`}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => navigate(item.href)}
                  >
                    <Icon className="size-5 shrink-0" />
                    <span>{item.label}</span>
                  </button>
                )
              })}
            </div>
          </nav>
        </div>
      )}

      <nav className="mobile-tabbar" aria-label="Primary">
        {PRIMARY.map((item) => {
          const Icon = item.icon
          const active = isActive(item.href)
          return (
            <button
              key={item.href}
              className={`mobile-tab ${active ? 'mobile-tab-active' : ''}`}
              aria-current={active ? 'page' : undefined}
              onClick={() => navigate(item.href)}
            >
              <Icon className="size-5" />
              <span>{item.label.split(' ')[0]}</span>
            </button>
          )
        })}
        <button
          className={`mobile-tab ${drawerOpen ? 'mobile-tab-active' : ''}`}
          aria-expanded={drawerOpen}
          aria-label="More sections"
          onClick={() => setDrawerOpen((open) => !open)}
        >
          <Menu className="size-5" />
          <span>More</span>
        </button>
      </nav>
    </>
  )
}
