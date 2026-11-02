// What the pages that edit the agent gateway config decide: when an edit lost a race, which skills may
// still be granted, how a saved change is put in words, how a narration is grouped. The server is the
// authority on every one of them (see `src/gateway/edits.py`); this is how the console says it.

import { ApiError, type NarrationLine, type SkillCatalogEntry } from '@/lib/api'

/** True when the server refused an edit because the config changed after the page read it (a 409 that
 * says so; a 409 for "nothing to change" is a different refusal). */
export function isStaleConfig(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.message.includes('changed since you read it')
}

export const STALE_NOTICE =
  'The configuration changed while you were looking at it, so nothing was saved. The page now shows the current settings: make your change again.'

/** The words for a failed edit, and whether it lost a race (so the page should reload what it shows). */
export function editFailure(err: unknown, fallback: string): { message: string; stale: boolean } {
  if (isStaleConfig(err)) return { message: STALE_NOTICE, stale: true }
  return { message: err instanceof ApiError && err.message ? err.message : fallback, stale: false }
}

/** The skills not yet held, from the catalog, for the grant list. */
export function grantable(catalog: SkillCatalogEntry[], held: string[]): SkillCatalogEntry[] {
  return catalog.filter((entry) => !held.includes(entry.skill_id))
}

/** What an agent's skills become when one is granted or revoked. */
export function skillsAfter(held: string[], skill: string, grant: boolean): string[] {
  const without = held.filter((s) => s !== skill)
  return grant ? [...without, skill] : without
}

/** What to tell a person about a skill beside its name. */
export function skillTags(entry: SkillCatalogEntry): string[] {
  const tags: string[] = []
  if (!entry.working) tags.push('placeholder')
  if (entry.effect === 'action') tags.push('sends messages')
  return tags
}

const CHANGE_WORDS: Record<string, string> = {
  enabled: 'switched on or off',
  heartbeat_enabled: 'heartbeat',
  heartbeat_interval_minutes: 'heartbeat interval',
  skills: 'skills',
}

/** `Saved: heartbeat and skills.` */
export function savedWords(changed: string[]): string {
  const words = changed.map((name) => CHANGE_WORDS[name] ?? name)
  if (words.length === 0) return 'Saved.'
  const list = words.length === 1 ? words[0] : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
  return `Saved: ${list}.`
}

export interface NarrationGroup {
  kind: NarrationLine['kind']
  label: string
  lines: string[]
}

const GROUP_LABELS: [NarrationLine['kind'], string][] = [
  ['state', 'Settings'],
  ['heartbeat', 'Heartbeat'],
  ['work', 'Work'],
  ['gap', 'What is missing'],
]

/** The narration's lines grouped under their headings, in a fixed order, leaving out an empty group. */
export function narrationGroups(lines: NarrationLine[]): NarrationGroup[] {
  return GROUP_LABELS.map(([kind, label]) => ({ kind, label, lines: lines.filter((l) => l.kind === kind).map((l) => l.text) })).filter(
    (group) => group.lines.length > 0,
  )
}
