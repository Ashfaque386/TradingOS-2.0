/**
 * What the strategy review page says about a strategy's versions and the suggestions that made some of
 * them, kept free of React so a logic spec can drive it: how each version fared in validation and the
 * sandbox, where it came from, which two to compare by default, and how a suggestion's life reads.
 */
import type { StrategyVersion, SuggestionDto } from './api'
import { indiaStamp } from './time'

export const versionLabel = (version: Pick<StrategyVersion, 'version_number'>): string => `v${version.version_number}`

/** Newest version first. */
export const newestFirst = (versions: StrategyVersion[]): StrategyVersion[] => [...versions].sort((a, b) => b.version_number - a.version_number)

export function validationNote(version: StrategyVersion): string {
  if (version.static_validation_passed) return 'Passed static validation'
  const errors = version.static_validation_errors ?? []
  return errors.length > 0 ? `Failed static validation: ${errors.join('; ')}` : 'Failed static validation'
}

export function sandboxNote(version: StrategyVersion): string {
  if (version.sandbox_passed === null) return 'Sandbox: not run'
  const duration = typeof version.sandbox_result?.duration_seconds === 'number' ? ` in ${version.sandbox_result.duration_seconds.toFixed(1)} s` : ''
  return version.sandbox_passed ? `Sandbox: passed${duration}` : `Sandbox: failed${duration}`
}

/** What a sandbox run recorded, as labelled facts. The code's own result is shown as it is: it is what the
 * strategy's code said about itself, not something the platform measured. */
export function sandboxFacts(version: StrategyVersion): { label: string; value: string }[] {
  const run = version.sandbox_result
  if (!run) return []
  const facts: { label: string; value: string }[] = [{ label: 'Result', value: run.success === true ? 'Passed' : 'Failed' }]
  if (typeof run.duration_seconds === 'number') facts.push({ label: 'Took', value: `${run.duration_seconds.toFixed(2)} seconds` })
  if (typeof run.error === 'string' && run.error.trim() !== '') facts.push({ label: 'Error', value: run.error.trim() })
  if (run.result !== undefined && run.result !== null) facts.push({ label: 'What the code returned', value: JSON.stringify(run.result, null, 2) })
  return facts
}

const clip = (text: string, length = 70): string => (text.length > length ? `${text.slice(0, length - 1).trimEnd()}…` : text)

export type VersionRow = {
  version: StrategyVersion
  label: string
  made: string
  validation: string
  sandbox: string
  from: string | null
}

/** Each version, newest first, with how it came to be: the first from the objective, a later one from the
 * suggestion that was regenerated into it. */
export function versionRows(versions: StrategyVersion[], suggestions: SuggestionDto[]): VersionRow[] {
  const oldest = versions.length > 0 ? Math.min(...versions.map((v) => v.version_number)) : 0
  const byVersion = new Map(suggestions.filter((s) => s.regenerated_version_id).map((s) => [s.regenerated_version_id as string, s]))
  return newestFirst(versions).map((version) => {
    const suggestion = byVersion.get(version.id)
    return {
      version,
      label: versionLabel(version),
      made: indiaStamp(version.created_at),
      validation: validationNote(version),
      sandbox: sandboxNote(version),
      from: suggestion
        ? `Regenerated from a suggestion${suggestion.requested_by ? ` by ${suggestion.requested_by}` : ''}: “${clip(suggestion.suggestion_text)}”`
        : version.version_number === oldest
          ? 'Generated from the strategy’s objective'
          : null,
    }
  })
}

/** The two versions to compare when the tab opens: the one before the newest, and the newest. */
export function defaultCompare(versions: StrategyVersion[]): { from: string | null; to: string | null } {
  const sorted = newestFirst(versions)
  return { from: sorted[1]?.id ?? null, to: sorted[0]?.id ?? null }
}

const STATUS_LABEL: Record<string, string> = {
  pending: 'Waiting for the AI review',
  reviewed: 'Reviewed, not yet turned into a version',
  regenerated: 'Turned into a new version',
  dismissed: 'Dismissed',
}

export const suggestionStatus = (status: string): string => STATUS_LABEL[status] ?? status

export type SuggestionRow = {
  suggestion: SuggestionDto
  status: string
  asked: string
  base: string
  produced: string | null
  verdict: string | null
}

/** A suggestion's life in words: who asked, on which version, what the reviewer said, and which version it became. */
export function suggestionRows(suggestions: SuggestionDto[], versions: StrategyVersion[]): SuggestionRow[] {
  const label = new Map(versions.map((v) => [v.id, versionLabel(v)]))
  return suggestions.map((suggestion) => {
    const verdict = suggestion.ai_verdict
    const said = verdict ? String(verdict.verdict ?? verdict.status ?? '') : ''
    const notes = verdict ? String(verdict.notes ?? verdict.summary ?? '') : ''
    return {
      suggestion,
      status: suggestionStatus(suggestion.status),
      asked: `${suggestion.requested_by ?? 'Someone'} · ${indiaStamp(suggestion.created_at)}`,
      base: label.get(suggestion.base_version_id) ?? 'an earlier version',
      produced: suggestion.regenerated_version_id ? (label.get(suggestion.regenerated_version_id) ?? 'a new version') : null,
      verdict: said || notes ? [said, notes].filter(Boolean).join(': ') : null,
    }
  })
}
