/**
 * Reading and writing the browser's saved preferences without trusting them: storage can be missing
 * or throw (private windows, blocked site data), and what is in it can be stale or hand-edited. A bad
 * value falls back to the default rather than breaking the page.
 */
type Store = Pick<Storage, 'getItem' | 'setItem'>

function browserStore(): Store | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

export function readChoice<T extends string>(key: string, allowed: readonly T[], fallback: T, store: Store | null = browserStore()): T {
  try {
    const value = store?.getItem(key)
    return value !== null && value !== undefined && (allowed as readonly string[]).includes(value) ? (value as T) : fallback
  } catch {
    return fallback
  }
}

export function readFlag(key: string, fallback: boolean, store: Store | null = browserStore()): boolean {
  try {
    const value = store?.getItem(key)
    if (value === 'true') return true
    if (value === 'false') return false
    return fallback
  } catch {
    return fallback
  }
}

export function writeSetting(key: string, value: string | boolean, store: Store | null = browserStore()): void {
  try {
    store?.setItem(key, String(value))
  } catch {
    // not saved: the choice still applies for this visit
  }
}
