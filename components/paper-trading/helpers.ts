import { ApiError, type StrategySummary } from '@/lib/api'

export function messageOf(error: unknown, fallback: string): string {
  return error instanceof ApiError || error instanceof Error ? error.message : fallback
}

/** The strategy version a new subscription is attached to: the newest version that passed static
 * validation, or the newest at all. */
export function latestVersionId(strategy: StrategySummary): string | null {
  const versions = [...strategy.versions].sort((a, b) => b.version_number - a.version_number)
  return (versions.find((v) => v.static_validation_passed) ?? versions[0])?.id ?? null
}
