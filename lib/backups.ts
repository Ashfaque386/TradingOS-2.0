import type { BackupEntry, BackupKind, PostgresBackupRun } from './api'

// What Settings says about the backups, worked out from the API's numbers. Pure: no browser, no fetch.

const KIND_NAME: Record<BackupKind['kind'], string> = { postgres: 'database', lake: 'data lake' }

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}

/** How long ago, in the largest sensible unit: minutes under an hour, hours under two days, then days. */
export function formatAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return 'under a minute ago'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} h ago`
  return `${Math.floor(hours / 24)} days ago`
}

/** When a backup was made, in India time like the schedule: "01 Nov, 22:45 IST". */
export function formatMadeAt(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const day = date.toLocaleDateString('en-GB', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' })
  const time = date.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false })
  return `${day}, ${time} IST`
}

export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 12) : '—'
}

export type BackupSummary = { tone: 'ok' | 'warn'; text: string }

/**
 * One line per kind: is there a backup a person could restore from, and is it recent enough. A newer
 * attempt that did not finish is said too: the page would otherwise show a fresh-looking row that cannot
 * be used.
 */
export function backupSummary(kind: BackupKind, staleAfterHours: number): BackupSummary {
  const name = KIND_NAME[kind.kind]
  const newestOk = kind.backups.find((b) => b.status === 'ok')
  if (!newestOk || kind.newest_ok_age_seconds === null) {
    return {
      tone: 'warn',
      text:
        kind.kind === 'postgres'
          ? 'No database backup has been made, so there is nothing to restore the database from.'
          : 'No data lake backup has been made.',
    }
  }
  const unfinished = kind.backups[0]?.status === 'incomplete' ? ' The latest attempt did not finish and cannot be restored from.' : ''
  const age = formatAge(kind.newest_ok_age_seconds)
  if (kind.stale) {
    return {
      tone: 'warn',
      text: `The newest ${name} backup is from ${age} — older than the ${staleAfterHours} hours it should be. Check that the nightly job is running.${unfinished}`,
    }
  }
  return { tone: 'ok', text: `The newest ${name} backup is from ${age}.${unfinished}` }
}

/** What "Back up now" reports once it has finished. */
export function backupRunSummary(run: PostgresBackupRun): string {
  const removed = run.pruned.length
  const tail = removed === 0 ? '' : ` Removed ${removed} older backup${removed === 1 ? '' : 's'} under the retention rule.`
  return `Backed up as ${run.backup.backup_id} (${formatBytes(run.backup.size_bytes)}, checked).${tail}`
}

/** The one-line description of a row's contents, for the table. */
export function backupContents(kind: BackupKind['kind'], entry: BackupEntry): string {
  if (entry.status !== 'ok') return entry.note ?? 'not usable'
  const rows = entry.total_rows === null ? '' : `${entry.total_rows.toLocaleString('en-IN')} rows`
  if (kind === 'lake') return [entry.files === null ? '' : `${entry.files} files`, rows].filter(Boolean).join(', ')
  const tip = entry.audit_tip_sequence === null ? 'audit log empty' : `audit entry ${entry.audit_tip_sequence}`
  return [entry.table_count === null ? '' : `${entry.table_count} tables`, rows, tip].filter(Boolean).join(', ')
}
