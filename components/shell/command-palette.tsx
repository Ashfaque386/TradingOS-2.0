'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeftRight,
  Battery,
  Beaker,
  Bot,
  CornerDownLeft,
  Palette,
  Search,
  ShieldCheck,
  SquareArrowOutUpRight,
} from 'lucide-react'
import { NAV_TARGETS, SEARCH_ENTITIES } from '@/lib/search-index'
import { PALETTES, usePreferences } from '@/components/providers/preferences-provider'

export const OPEN_COMMAND_PALETTE_EVENT = 'tradingos:open-command-palette'

type Command = {
  id: string
  label: string
  hint?: string
  group: 'Actions' | 'Navigate' | 'Strategies' | 'Orders' | 'Agents' | 'Themes'
  icon: React.ComponentType<{ className?: string }>
  keywords?: string
  run: () => void
}

const kindIcon = { strategy: Beaker, order: ArrowLeftRight, agent: Bot } as const
const kindGroup = { strategy: 'Strategies', order: 'Orders', agent: 'Agents' } as const

export function CommandPalette() {
  const router = useRouter()
  const { powerSave, togglePowerSave, setPalette, palette } = usePreferences()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const close = useCallback(() => {
    setOpen(false)
    setQuery('')
    setActiveIndex(0)
  }, [])

  const go = useCallback(
    (href: string) => {
      router.push(href)
      close()
    },
    [router, close],
  )

  const commands = useMemo<Command[]>(() => {
    const actions: Command[] = [
      {
        id: 'action-signoff',
        label: 'View Sign-off Queue',
        hint: 'Mission Control',
        group: 'Actions',
        icon: ShieldCheck,
        keywords: 'approve review intents',
        run: () => go('/mission-control?view=queue'),
      },
      {
        id: 'action-power-save',
        label: powerSave ? 'Disable Power Save Mode' : 'Enable Power Save Mode',
        hint: powerSave ? 'Currently on' : 'Currently off',
        group: 'Actions',
        icon: Battery,
        keywords: 'toggle power save battery motion performance',
        run: () => {
          togglePowerSave()
          close()
        },
      },
    ]

    const nav: Command[] = NAV_TARGETS.map((target) => ({
      id: `nav-${target.href}`,
      label: target.label,
      hint: target.href,
      group: 'Navigate',
      icon: SquareArrowOutUpRight,
      keywords: target.keywords,
      run: () => go(target.href),
    }))

    const entities: Command[] = SEARCH_ENTITIES.map((entity) => ({
      id: `entity-${entity.kind}-${entity.name}`,
      label: entity.name,
      hint: entity.detail,
      group: kindGroup[entity.kind],
      icon: kindIcon[entity.kind],
      keywords: entity.kind,
      run: () => go(entity.href),
    }))

    const themes: Command[] = PALETTES.map((option) => ({
      id: `theme-${option.id}`,
      label: `Theme: ${option.label}`,
      hint: palette === option.id ? 'Active' : 'Switch palette',
      group: 'Themes',
      icon: Palette,
      keywords: `theme palette color ${option.id}`,
      run: () => {
        setPalette(option.id)
        close()
      },
    }))

    return [...actions, ...nav, ...entities, ...themes]
  }, [powerSave, palette, go, togglePowerSave, setPalette, close])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return commands
    return commands.filter((command) =>
      `${command.label} ${command.hint ?? ''} ${command.keywords ?? ''}`
        .toLowerCase()
        .includes(q),
    )
  }, [commands, query])

  const grouped = useMemo(() => {
    const map = new Map<string, Command[]>()
    for (const command of filtered) {
      const list = map.get(command.group) ?? []
      list.push(command)
      map.set(command.group, list)
    }
    return [...map.entries()]
  }, [filtered])

  // Global ⌘K / Ctrl+K listener + custom open event from the top bar button.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen((current) => !current)
      }
      if (event.key === 'Escape') close()
    }
    const onOpen = () => setOpen(true)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen)
    }
  }, [close])

  useEffect(() => {
    if (open) {
      const timer = window.setTimeout(() => inputRef.current?.focus(), 10)
      return () => window.clearTimeout(timer)
    }
  }, [open])

  useEffect(() => {
    setActiveIndex(0)
  }, [query])

  if (!open) return null

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((index) => Math.min(index + 1, filtered.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((index) => Math.max(index - 1, 0))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      filtered[activeIndex]?.run()
    }
  }

  let runningIndex = -1

  return (
    <div
      className="cmdk-overlay"
      role="button"
      tabIndex={-1}
      aria-label="Close command palette"
      onClick={close}
    >
      <div
        className="cmdk-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="cmdk-search">
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Jump to a page, search strategies, orders, agents, or run an action..."
            aria-label="Command palette search"
            className="cmdk-input"
          />
          <kbd className="cmdk-kbd">ESC</kbd>
        </div>

        <div className="cmdk-list" ref={listRef}>
          {filtered.length === 0 && (
            <p className="cmdk-empty">No matches for &ldquo;{query}&rdquo;</p>
          )}
          {grouped.map(([group, items]) => (
            <div key={group} className="cmdk-group">
              <p className="cmdk-group-label">{group}</p>
              {items.map((command) => {
                runningIndex += 1
                const index = runningIndex
                const Icon = command.icon
                const isActive = index === activeIndex
                return (
                  <button
                    key={command.id}
                    type="button"
                    className={`cmdk-item ${isActive ? 'cmdk-item-active' : ''}`}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={command.run}
                  >
                    <Icon className="size-4 shrink-0 text-cyan-200/80" />
                    <span className="cmdk-item-label">{command.label}</span>
                    {command.hint && <span className="cmdk-item-hint">{command.hint}</span>}
                    {isActive && <CornerDownLeft className="size-3 shrink-0 text-muted-foreground" />}
                  </button>
                )
              })}
            </div>
          ))}
        </div>

        <div className="cmdk-footer">
          <span><kbd className="cmdk-kbd">↑</kbd><kbd className="cmdk-kbd">↓</kbd> navigate</span>
          <span><kbd className="cmdk-kbd">↵</kbd> select</span>
          <span><kbd className="cmdk-kbd">⌘K</kbd> toggle</span>
        </div>
      </div>
    </div>
  )
}
