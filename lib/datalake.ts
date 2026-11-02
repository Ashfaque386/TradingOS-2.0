// The data lake's status said in words: a pipeline's name, its light, and how far behind the newest
// daily data is. The figures come from `GET /market-data/datalake/status`.

/** `incremental_daily` -> `Incremental Daily`. */
export function pipelineLabel(pipeline: string): string {
  return pipeline
    .split('_')
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ')
}

export type Light = 'green' | 'amber' | 'red' | 'gray'

/** A pipeline's last run as a light: green succeeded, amber partial, red failed, gray never ran. */
export function statusLight(status: string | null): Light {
  if (status === 'success') return 'green'
  if (status === 'failed') return 'red'
  if (status === 'partial') return 'amber'
  return 'gray'
}

/** How far the newest daily data is from the day a backtest requires, and whether that is a problem. */
export function behindWords(behind: number | null): { text: string; tone: 'ok' | 'bad' | 'none' } {
  if (behind === null) return { text: 'no daily data in the lake', tone: 'none' }
  if (behind === 0) return { text: 'current', tone: 'ok' }
  return { text: `${behind} trading day${behind === 1 ? '' : 's'} behind`, tone: 'bad' }
}
