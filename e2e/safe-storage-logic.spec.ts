import { expect, test } from '@playwright/test'
import { readChoice, readFlag, writeSetting } from '../lib/safe-storage'
import { navFor } from '../lib/search-index'
import { parseSenderIds } from '../lib/notification-senders'

// The browser's saved preferences are not trusted, and the page list follows the role.

const PALETTES = ['nominal', 'caution', 'risk'] as const

function memoryStore(initial: Record<string, string> = {}) {
  const data = { ...initial }
  return {
    data,
    getItem: (key: string) => (key in data ? data[key] : null),
    setItem: (key: string, value: string) => {
      data[key] = value
    },
  }
}

test('a saved palette that is not one of ours falls back to the default', () => {
  expect(readChoice('p', PALETTES, 'nominal', memoryStore({ p: 'risk' }))).toBe('risk')
  expect(readChoice('p', PALETTES, 'nominal', memoryStore({ p: 'hot-pink' }))).toBe('nominal')
  expect(readChoice('p', PALETTES, 'nominal', memoryStore())).toBe('nominal')
})

test('a saved flag is true or false, and anything else is the default', () => {
  expect(readFlag('f', false, memoryStore({ f: 'true' }))).toBe(true)
  expect(readFlag('f', true, memoryStore({ f: 'false' }))).toBe(false)
  expect(readFlag('f', true, memoryStore({ f: '{"oops":' }))).toBe(true) // used to be JSON.parse'd and throw
})

test('storage that throws, or is missing, never breaks reading or writing', () => {
  const broken = {
    getItem: () => {
      throw new Error('blocked')
    },
    setItem: () => {
      throw new Error('blocked')
    },
  }
  expect(readChoice('p', PALETTES, 'caution', broken)).toBe('caution')
  expect(readFlag('f', true, broken)).toBe(true)
  expect(() => writeSetting('p', 'risk', broken)).not.toThrow()
  expect(readChoice('p', PALETTES, 'caution', null)).toBe('caution')
  expect(() => writeSetting('p', 'risk', null)).not.toThrow()
})

test('a setting is saved as text and read back', () => {
  const store = memoryStore()
  writeSetting('f', true, store)
  expect(readFlag('f', false, store)).toBe(true)
})

test('only the auditor and the administrator are offered the Audit Log', () => {
  const offers = (role: Parameters<typeof navFor>[0]) => navFor(role).some((page) => page.href === '/audit')
  expect(offers('ReadOnlyAuditor')).toBe(true)
  expect(offers('SystemAdministrator')).toBe(true)
  expect(offers('PortfolioManager')).toBe(false)
  expect(offers('RiskManager')).toBe(false)
  expect(offers(null)).toBe(false)
  expect(navFor('PortfolioManager').length).toBe(navFor('SystemAdministrator').length - 1)
})

test('sender IDs are split on commas and spaces, trimmed, and not repeated', () => {
  expect(parseSenderIds('123, 456 789;123')).toEqual(['123', '456', '789'])
  expect(parseSenderIds('  ')).toEqual([])
})
