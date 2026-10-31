'use client'

import { useRef } from 'react'

/**
 * The container for a row of tab buttons (each gets `role="tab"` and `aria-selected`). Adds the
 * keyboard a tab row is expected to have: Left and Right (and Home and End) move to the neighbouring
 * tab and open it, and only the open tab is in the Tab order.
 */
export function TabList({
  label,
  className,
  children,
}: {
  label: string
  className?: string
  children: React.ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End']
    if (!keys.includes(event.key) || !ref.current) return
    const tabs = [...ref.current.querySelectorAll<HTMLElement>('[role="tab"]')]
    const current = tabs.findIndex((tab) => tab === document.activeElement)
    if (tabs.length === 0 || current === -1) return
    event.preventDefault()
    const next =
      event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    tabs[next].focus()
    tabs[next].click()
  }

  return (
    <div ref={ref} role="tablist" aria-label={label} className={className} onKeyDown={onKeyDown}>
      {children}
    </div>
  )
}
