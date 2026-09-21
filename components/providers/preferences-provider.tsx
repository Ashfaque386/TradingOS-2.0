'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

export const PALETTES = [
  { id: 'nominal', label: 'Nominal', color: '#06b6d4' },
  { id: 'caution', label: 'Caution', color: '#f59e0b' },
  { id: 'risk', label: 'Risk', color: '#ef4444' },
  { id: 'neutral', label: 'Neutral', color: '#94a3b8' },
  { id: 'light', label: 'Light', color: '#f5f5f5' },
  { id: 'contrast', label: 'Contrast', color: '#ffffff' },
] as const

export type PaletteId = (typeof PALETTES)[number]['id']

const ACCENTS: Record<string, string> = {
  nominal: '#06b6d4',
  caution: '#f59e0b',
  risk: '#ef4444',
  // Build Spec §21-22 WCAG 2.1 AA pass: was #6b7280 (4.17:1 against the
  // dark background as literal nav-item text -- under the 4.5:1 minimum).
  neutral: '#94a3b8',
  light: '#1f2937',
  contrast: '#ffffff',
}

type PreferencesContextValue = {
  palette: string
  setPalette: (palette: string) => void
  powerSave: boolean
  setPowerSave: (enabled: boolean) => void
  togglePowerSave: () => void
  prefersReducedMotion: boolean
  /** True when either the manual power-save toggle OR the OS reduced-motion setting is active. */
  reduceMotion: boolean
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

export function PreferencesProvider({ children }: { children: React.ReactNode }) {
  const [palette, setPaletteState] = useState<string>('nominal')
  const [powerSave, setPowerSaveState] = useState<boolean>(false)
  const [prefersReducedMotion, setPrefersReducedMotion] = useState<boolean>(false)

  // Hydrate persisted preferences.
  useEffect(() => {
    const savedPalette = localStorage.getItem('tradingos-palette')
    const savedPowerSave = localStorage.getItem('tradingos-power-save')
    if (savedPalette) setPaletteState(savedPalette)
    if (savedPowerSave) setPowerSaveState(JSON.parse(savedPowerSave))
  }, [])

  // Track OS-level reduced-motion preference, independent of the manual toggle.
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setPrefersReducedMotion(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  // Reflect preferences onto <html> so global CSS and every route (including auth) respond.
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-palette', palette)
    root.setAttribute('data-power-save', String(powerSave))
    root.setAttribute('data-reduced-motion', String(prefersReducedMotion))
    root.style.setProperty('--current-accent', ACCENTS[palette] ?? ACCENTS.nominal)
  }, [palette, powerSave, prefersReducedMotion])

  const setPalette = useCallback((next: string) => {
    setPaletteState(next)
    localStorage.setItem('tradingos-palette', next)
  }, [])

  const setPowerSave = useCallback((enabled: boolean) => {
    setPowerSaveState(enabled)
    localStorage.setItem('tradingos-power-save', JSON.stringify(enabled))
  }, [])

  const togglePowerSave = useCallback(() => {
    setPowerSaveState((current) => {
      const next = !current
      localStorage.setItem('tradingos-power-save', JSON.stringify(next))
      return next
    })
  }, [])

  const value = useMemo<PreferencesContextValue>(
    () => ({
      palette,
      setPalette,
      powerSave,
      setPowerSave,
      togglePowerSave,
      prefersReducedMotion,
      reduceMotion: powerSave || prefersReducedMotion,
    }),
    [palette, setPalette, powerSave, setPowerSave, togglePowerSave, prefersReducedMotion],
  )

  return (
    <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>
  )
}

export function usePreferences() {
  const value = useContext(PreferencesContext)
  if (!value) throw new Error('usePreferences must be used inside PreferencesProvider')
  return value
}
