/**
 * What the Audit page decides, kept free of React so a logic spec can drive it: how the filters on
 * screen become a request and a URL, which page of rows is shown, and how a chain verification is
 * put into words. Times are shown and typed in India time (the exchange's clock, as on the Paper
 * Desk); India has no daylight saving, so a fixed +05:30 is exact.
 */
import type { AuditActorSummary, AuditChainVerifyResult, AuditQuery } from './api'

export type AuditFilterState = {
  search: string
  actor: string
  entityType: string
  action: string
  entityId: string
  /** Typed as India time, `YYYY-MM-DDTHH:mm` (what a datetime-local input holds). */
  since: string
  until: string
}

export const NO_FILTER: AuditFilterState = {
  search: '',
  actor: '',
  entityType: '',
  action: '',
  entityId: '',
  since: '',
  until: '',
}

export const isFiltered = (filter: AuditFilterState): boolean =>
  (Object.keys(NO_FILTER) as (keyof AuditFilterState)[]).some((key) => filter[key].trim() !== '')

/** `2026-11-02T09:15` typed as India time, as an ISO time with its offset; undefined for anything else. */
export function indiaTimeToIso(local: string): string | undefined {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(:\d{2})?$/.exec(local.trim())
  if (!match) return undefined
  const iso = `${match[1]}${match[2] ?? ':00'}+05:30`
  return Number.isNaN(Date.parse(iso)) ? undefined : iso
}

/** The request for what is on screen: empty boxes are left out, text is trimmed. */
export function queryFor(filter: AuditFilterState): AuditQuery {
  const query: AuditQuery = {}
  if (filter.search.trim()) query.search = filter.search.trim()
  if (filter.actor.trim()) query.actor = filter.actor.trim()
  if (filter.entityType.trim()) query.entityType = filter.entityType.trim()
  if (filter.action.trim()) query.action = filter.action.trim()
  if (filter.entityId.trim()) query.entityId = filter.entityId.trim()
  const since = indiaTimeToIso(filter.since)
  const until = indiaTimeToIso(filter.until)
  if (since) query.since = since
  if (until) query.until = until
  return query
}

/** A time range that ends before it starts matches nothing; say so instead of showing an empty log. */
export function rangeProblem(filter: AuditFilterState): string | null {
  const since = indiaTimeToIso(filter.since)
  const until = indiaTimeToIso(filter.until)
  if (since && until && Date.parse(until) < Date.parse(since)) return 'The end of the range is before its start.'
  return null
}

const URL_KEYS: Record<keyof AuditFilterState, string> = {
  search: 'search',
  actor: 'actor',
  entityType: 'entity_type',
  action: 'action',
  entityId: 'entity_id',
  since: 'since',
  until: 'until',
}

/** The filters in a page address (`?actor=...`), so a link from another page or a shared URL opens the log already narrowed. */
export function filterFromUrl(queryString: string): AuditFilterState {
  const params = new URLSearchParams(queryString)
  const filter = { ...NO_FILTER }
  for (const key of Object.keys(URL_KEYS) as (keyof AuditFilterState)[]) {
    filter[key] = params.get(URL_KEYS[key]) ?? ''
  }
  return filter
}

export function filterToUrl(filter: AuditFilterState): string {
  const params = new URLSearchParams()
  for (const key of Object.keys(URL_KEYS) as (keyof AuditFilterState)[]) {
    if (filter[key].trim()) params.set(URL_KEYS[key], filter[key].trim())
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

/** Which rows a page shows, and how many pages there are; a page past the end is clamped to the last. */
export function pageWindow(total: number, page: number, size: number) {
  const pages = Math.max(1, Math.ceil(total / size))
  const current = Math.min(Math.max(1, page), pages)
  const offset = (current - 1) * size
  return { pages, current, offset, from: total === 0 ? 0 : offset + 1, to: Math.min(total, offset + size) }
}

/** `02 Nov 2026, 14:05:09` in India time; a dash for anything that is not a time. */
export function indiaStamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

/** `3f9a1c2e…b7d4e1`: enough to tell two hashes apart in a list; the whole value is in the entry's details. */
export function shortHash(hash: string): string {
  return hash.length <= 16 ? hash : `${hash.slice(0, 8)}…${hash.slice(-6)}`
}

export type VerifyCheck = { label: string; ok: boolean; detail: string }

/** The three things a verification checks, each with whether it held and what that means. */
export function verifyChecks(result: AuditChainVerifyResult): VerifyCheck[] {
  const where =
    result.first_diverged_sequence !== null ? ` The first difference is at entry #${result.first_diverged_sequence}.` : ''
  return [
    {
      label: 'Hash chain in the database',
      ok: result.db_chain_valid,
      detail: result.db_chain_valid
        ? `${result.entries_checked_in_db_chain.toLocaleString('en-IN')} entries checked; every link matches the entry before it.`
        : (result.db_chain_reason ?? 'A link in the chain does not match.'),
    },
    {
      label: 'Archive file',
      ok: result.archive_internally_valid,
      detail: result.archive_internally_valid
        ? 'The archive’s own chain is intact.'
        : `${result.reason ?? 'The archive does not verify on its own.'}${where}`,
    },
    {
      label: 'Database against the archive',
      ok: result.live_db_matches_archive,
      detail: result.live_db_matches_archive
        ? 'Every archived entry is in the database, unchanged.'
        : `${result.reason ?? 'The database and the archive disagree.'}${where}`,
    },
  ]
}

export const verifyFailed = (result: AuditChainVerifyResult): boolean => result.diverged || !result.db_chain_valid

export type ActionShare = { action: string; count: number; share: number }

/** An actor's actions, most frequent first, each with its share of all they did (0 to 1). */
export function actionShares(summary: AuditActorSummary): ActionShare[] {
  const total = summary.action_count > 0 ? summary.action_count : 1
  return Object.entries(summary.actions)
    .map(([action, count]) => ({ action, count, share: count / total }))
    .sort((a, b) => b.count - a.count || a.action.localeCompare(b.action))
}
