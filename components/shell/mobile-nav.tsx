'use client'

import { useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import type { LucideIcon } from 'lucide-react'
import { useAuth } from '@/components/auth/auth-provider'
import { navFor } from '@/lib/search-index'
import {
  Activity,
  ArrowLeftRight,
  BarChart3,
  Beaker,
  FileText,
  Home,
  Landmark,
  Menu,
  MessageSquare,
  Network,
  PieChart,
  Settings,
  Shield,
  X,
  Zap,
} from 'lucide-react'

// The list of pages is lib/search-index.ts's NAV_TARGETS (shared with the sidebar and the command
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

export function MobileNav() {
  const pathname = usePathname()
  const { role } = useAuth()
  const NAV_ITEMS = navFor(role).map((target) => ({ ...target, icon: NAV_ICONS[target.href] ?? Home }))
  // Primary destinations pinned to the bottom tab bar; the rest live behind "More".
  const PRIMARY = NAV_ITEMS.slice(0, 4)
  const [drawerOpen, setDrawerOpen] = useState(false)

  const isActive = (href: string) => (href === '/' ? pathname === '/' : pathname.startsWith(href))


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
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`mobile-drawer-item ${active ? 'mobile-drawer-item-active' : ''}`}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => setDrawerOpen(false)}
                  >
                    <Icon className="size-5 shrink-0" />
                    <span>{item.label}</span>
                  </Link>
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
            <Link
              key={item.href}
              href={item.href}
              className={`mobile-tab ${active ? 'mobile-tab-active' : ''}`}
              aria-current={active ? 'page' : undefined}
              aria-label={item.label}
              onClick={() => setDrawerOpen(false)}
            >
              <Icon className="size-5" />
              <span>{item.label.split(' ')[0]}</span>
            </Link>
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
