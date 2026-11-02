import { expect, test } from '@playwright/test'
import { ApiError, type NarrationLine, type SkillCatalogEntry } from '../lib/api'
import {
  STALE_NOTICE,
  editFailure,
  grantable,
  isStaleConfig,
  narrationGroups,
  savedWords,
  skillTags,
  skillsAfter,
} from '../lib/gateway-edit'

// What the pages that edit the agent gateway config decide, with no browser and no backend: each case
// states the answer worked out by hand.

const STALE_FROM_SERVER =
  'The configuration changed since you read it (you edited version 4, version 6 is running now). Reload and make your change again.'

const entry = (skill_id: string, over: Partial<SkillCatalogEntry> = {}): SkillCatalogEntry => ({
  skill_id,
  summary: `${skill_id} does a thing`,
  effect: 'read',
  working: true,
  granted_to: [],
  ...over,
})

test.describe('an edit that lost a race', () => {
  test('only a 409 that says the config changed is a lost race', () => {
    expect(isStaleConfig(new ApiError(409, STALE_FROM_SERVER))).toBe(true)
    expect(isStaleConfig(new ApiError(409, 'Everything asked for is already so.'))).toBe(false)
    expect(isStaleConfig(new ApiError(422, STALE_FROM_SERVER))).toBe(false)
    expect(isStaleConfig(new ApiError(500, 'boom'))).toBe(false)
    expect(isStaleConfig(new Error(STALE_FROM_SERVER))).toBe(false)
    expect(isStaleConfig(null)).toBe(false)
  })

  test('a lost race says nothing was saved and tells the page to reload', () => {
    expect(editFailure(new ApiError(409, STALE_FROM_SERVER), 'Failed')).toEqual({ message: STALE_NOTICE, stale: true })
    expect(STALE_NOTICE).toContain('nothing was saved')
  })

  test('any other failure gives the server\'s reason when there is one, else the fallback', () => {
    expect(editFailure(new ApiError(422, 'No such skill: nonsense.'), 'Failed')).toEqual({ message: 'No such skill: nonsense.', stale: false })
    expect(editFailure(new ApiError(409, 'Everything asked for is already so.'), 'Failed').stale).toBe(false)
    expect(editFailure(new ApiError(500, ''), 'Failed to apply change')).toEqual({ message: 'Failed to apply change', stale: false })
    expect(editFailure(new Error('network'), 'Failed to apply change').message).toBe('Failed to apply change')
  })
})

test.describe('granting skills', () => {
  const catalog = [entry('code-format-lint'), entry('market-data-read'), entry('notification-send')]

  test('what may still be granted is the catalog less what is held, in the catalog\'s order', () => {
    expect(grantable(catalog, ['market-data-read']).map((e) => e.skill_id)).toEqual(['code-format-lint', 'notification-send'])
    expect(grantable(catalog, [])).toHaveLength(3)
    expect(grantable(catalog, catalog.map((e) => e.skill_id))).toEqual([])
    // a skill held but not in the catalog (a stale config) is no reason to offer anything twice
    expect(grantable(catalog, ['gone'])).toHaveLength(3)
  })

  test('granting adds the skill once, and revoking takes it away', () => {
    expect(skillsAfter(['a', 'b'], 'c', true)).toEqual(['a', 'b', 'c'])
    expect(skillsAfter(['a', 'b'], 'a', true)).toEqual(['b', 'a'])
    expect(skillsAfter(['a', 'b'], 'a', false)).toEqual(['b'])
    expect(skillsAfter(['a', 'b'], 'z', false)).toEqual(['a', 'b'])
    expect(skillsAfter([], 'a', true)).toEqual(['a'])
  })

  test('a skill is tagged when it is a placeholder and when it sends something', () => {
    expect(skillTags(entry('x'))).toEqual([])
    expect(skillTags(entry('x', { working: false }))).toEqual(['placeholder'])
    expect(skillTags(entry('x', { effect: 'action' }))).toEqual(['sends messages'])
    expect(skillTags(entry('x', { working: false, effect: 'action' }))).toEqual(['placeholder', 'sends messages'])
  })
})

test.describe('what a saved change is called', () => {
  test('one, two and three settings', () => {
    expect(savedWords([])).toBe('Saved.')
    expect(savedWords(['skills'])).toBe('Saved: skills.')
    expect(savedWords(['enabled', 'skills'])).toBe('Saved: switched on or off and skills.')
    expect(savedWords(['heartbeat_enabled', 'heartbeat_interval_minutes', 'skills'])).toBe('Saved: heartbeat, heartbeat interval and skills.')
    expect(savedWords(['something_new'])).toBe('Saved: something_new.')
  })
})

test.describe('the narration', () => {
  const lines: NarrationLine[] = [
    { kind: 'work', text: 'No task has claimed its capabilities yet.' },
    { kind: 'state', text: 'Market Analyst is switched on.' },
    { kind: 'heartbeat', text: 'Its heartbeat check is off, so it records no self-checks.' },
    { kind: 'state', text: 'It may use 1 skill: market-data-read.' },
  ]

  test('is grouped under its headings in a fixed order, each group in the order given', () => {
    expect(narrationGroups(lines)).toEqual([
      { kind: 'state', label: 'Settings', lines: ['Market Analyst is switched on.', 'It may use 1 skill: market-data-read.'] },
      { kind: 'heartbeat', label: 'Heartbeat', lines: ['Its heartbeat check is off, so it records no self-checks.'] },
      { kind: 'work', label: 'Work', lines: ['No task has claimed its capabilities yet.'] },
    ])
  })

  test('leaves out a heading with nothing under it, and says what is missing under its own', () => {
    expect(narrationGroups([])).toEqual([])
    expect(narrationGroups([{ kind: 'gap', text: 'No data source.' }])).toEqual([{ kind: 'gap', label: 'What is missing', lines: ['No data source.'] }])
  })
})
