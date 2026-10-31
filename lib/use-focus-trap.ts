'use client'

import { type RefObject, useEffect, useRef } from 'react'

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

/**
 * Keeps keyboard focus inside an open dialog or drawer: focus moves into `panel` when it opens, Tab and
 * Shift+Tab wrap at its ends, Escape calls `onEscape`, and focus returns to whatever opened it. Give the
 * panel `tabIndex={-1}` so it can take focus itself (a screen reader then starts at its title).
 */
export function useFocusTrap(panel: RefObject<HTMLElement | null>, onEscape: () => void, enabled = true) {
  const escapeRef = useRef(onEscape)
  escapeRef.current = onEscape

  useEffect(() => {
    if (!enabled) return
    const opener = document.activeElement as HTMLElement | null
    panel.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        escapeRef.current()
        return
      }
      if (event.key !== 'Tab' || !panel.current) return
      const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
      if (items.length === 0) return
      const [head, tail] = [items[0], items[items.length - 1]]
      if (event.shiftKey && (document.activeElement === head || document.activeElement === panel.current)) {
        event.preventDefault()
        tail.focus()
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault()
        head.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      opener?.focus?.()
    }
  }, [panel, enabled])
}
