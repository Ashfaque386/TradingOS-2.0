import { expect, test } from '@playwright/test'
import type { LlmProviderStatus } from '../lib/api'
import { MAX_TITLE, exportFilename, providerChoices, titleProblem } from '../lib/chat-view'
import { SECTION_IDS, sectionFromQuery, sectionToUrl } from '../lib/settings-view'

// What the Chat page and the Settings address decide, with no browser and no backend: each case states
// the answer worked out by hand.

const status = (provider: string, configured: boolean): LlmProviderStatus => ({ provider, configured, base_url: null, in_fallback_order: configured })

test.describe('a chat title', () => {
  test('is saved when it has something in it, spaces around it do not count, and a blank one is named', () => {
    expect(titleProblem('Morning check')).toBeNull()
    expect(titleProblem('  Morning check  ')).toBeNull()
    expect(titleProblem('')).toBe('A chat needs a title.')
    expect(titleProblem('   \t ')).toBe('A chat needs a title.')
  })

  test('is at most 200 characters, counted after the spaces are trimmed', () => {
    expect(MAX_TITLE).toBe(200)
    expect(titleProblem('x'.repeat(200))).toBeNull()
    expect(titleProblem(`  ${'x'.repeat(200)}  `)).toBeNull()
    expect(titleProblem('x'.repeat(201))).toBe('A title is at most 200 characters; this is 201.')
  })
})

test.describe('which provider answers a chat', () => {
  test('before the statuses are known there is auto and every provider, with no marks', () => {
    const choices = providerChoices(null)
    expect(choices[0]).toEqual({ value: '', label: 'Auto (the fallback order)' })
    expect(choices.slice(1).map((c) => c.value)).toEqual(['anthropic', 'openai', 'gemini', 'deepseek', 'huggingface', 'ollama', 'custom'])
    expect(choices.slice(1).every((c) => !c.label.includes('not configured'))).toBe(true)
    expect(choices[1].label).toBe('Anthropic')
  })

  test('a provider whose key is not saved is marked, and so is one the statuses leave out', () => {
    const choices = providerChoices([status('anthropic', true), status('openai', false)])
    const label = (value: string) => choices.find((c) => c.value === value)!.label
    expect(label('anthropic')).toBe('Anthropic')
    expect(label('openai')).toBe('OpenAI (not configured)')
    expect(label('gemini')).toBe('Google Gemini (not configured)')
    expect(choices[0].label).toBe('Auto (the fallback order)') // auto is never marked
  })
})

test('a conversation is saved under the server\'s own file name', () => {
  expect(exportFilename('abc', 'markdown')).toBe('chat_abc.md')
  expect(exportFilename('abc', 'json')).toBe('chat_abc.json')
})

test.describe('the Settings address', () => {
  test('names a section, and anything else is ignored', () => {
    expect(sectionFromQuery('?section=llm')).toBe('llm')
    expect(sectionFromQuery('?oauth=success&broker=zerodha&section=broker')).toBe('broker')
    expect(sectionFromQuery('')).toBeNull()
    expect(sectionFromQuery('?section=admin')).toBeNull()
    expect(sectionFromQuery('?section=')).toBeNull()
    expect(sectionFromQuery('?section=LLM')).toBeNull()
  })

  test('is the bare page for the section it opens on, and carries any other', () => {
    expect(sectionToUrl('gateway')).toBe('/settings')
    expect(sectionToUrl('security')).toBe('/settings?section=security')
  })

  test('survives a round trip for every section', () => {
    expect(SECTION_IDS).toHaveLength(9)
    for (const id of SECTION_IDS) expect(sectionFromQuery(sectionToUrl(id).split('?')[1] ? `?${sectionToUrl(id).split('?')[1]}` : '') ?? 'gateway').toBe(id)
  })
})
