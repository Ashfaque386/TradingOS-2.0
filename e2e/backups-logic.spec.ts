import { expect, test } from '@playwright/test'
import type { BackupEntry, BackupKind, PostgresBackupRun } from '../lib/api'
import {
  backupContents,
  backupRunSummary,
  backupSummary,
  formatAge,
  formatBytes,
  formatMadeAt,
  shortHash,
} from '../lib/backups'

// What Settings says about the backups. No browser, no backend: each case states the answer worked out by hand.

const entry = (over: Partial<BackupEntry> = {}): BackupEntry => ({
  backup_id: '20261101T171500Z',
  status: 'ok',
  created_at: '2026-11-01T17:15:00Z',
  age_seconds: 3600,
  size_bytes: 125_687,
  sha256: 'ab'.repeat(32),
  note: null,
  alembic_revision: null,
  audit_tip_sequence: null,
  audit_tip_hash: null,
  table_count: null,
  files: null,
  total_rows: null,
  ...over,
})

const kind = (over: Partial<BackupKind> = {}): BackupKind => ({
  kind: 'postgres',
  directory: '/app/backups',
  backups: [entry()],
  newest_ok_age_seconds: 3600,
  stale: false,
  ...over,
})

test.describe('sizes', () => {
  test('are shown in the unit a person reads', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(125_687)).toBe('123 KB')
    expect(formatBytes(5 * 1024 * 1024 + 300_000)).toBe('5.3 MB')
    expect(formatBytes(2.5 * 1024 ** 3)).toBe('2.5 GB')
  })

  test('a size that makes no sense is a dash, never "NaN KB"', () => {
    expect(formatBytes(-1)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
  })
})

test.describe('how long ago', () => {
  test('is minutes, then hours, then days', () => {
    expect(formatAge(5)).toBe('under a minute ago')
    expect(formatAge(60)).toBe('1 min ago')
    expect(formatAge(59 * 60 + 59)).toBe('59 min ago')
    expect(formatAge(3600)).toBe('1 h ago')
    expect(formatAge(47 * 3600 + 3599)).toBe('47 h ago')
    expect(formatAge(48 * 3600)).toBe('2 days ago')
    expect(formatAge(100 * 3600)).toBe('4 days ago')
  })

  test('an age that makes no sense is a dash', () => {
    expect(formatAge(-5)).toBe('—')
    expect(formatAge(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

test.describe('when a backup was made', () => {
  test('is India time whatever the browser zone is', () => {
    expect(formatMadeAt('2026-11-01T17:15:00Z')).toBe('01 Nov, 22:45 IST')
    expect(formatMadeAt('2026-11-01T18:30:00Z')).toBe('02 Nov, 00:00 IST')
  })

  test('a date that cannot be read is a dash', () => {
    expect(formatMadeAt('not a date')).toBe('—')
  })
})

test('a checksum is shown by its first twelve characters, and a missing one as a dash', () => {
  expect(shortHash('ab'.repeat(32))).toBe('abababababab')
  expect(shortHash(null)).toBe('—')
  expect(shortHash(undefined)).toBe('—')
})

test.describe('the one line about each kind of backup', () => {
  test('a recent backup is fine, and says how old it is', () => {
    expect(backupSummary(kind(), 36)).toEqual({ tone: 'ok', text: 'The newest database backup is from 1 h ago.' })
  })

  test('no backup at all is a warning that names what is at risk', () => {
    const none = kind({ backups: [], newest_ok_age_seconds: null, stale: true })
    expect(backupSummary(none, 36)).toEqual({
      tone: 'warn',
      text: 'No database backup has been made, so there is nothing to restore the database from.',
    })
    expect(backupSummary({ ...none, kind: 'lake' }, 36)).toEqual({ tone: 'warn', text: 'No data lake backup has been made.' })
  })

  test('an old backup says how old, against the limit, and what to check', () => {
    const old = kind({ backups: [entry({ age_seconds: 40 * 3600 })], newest_ok_age_seconds: 40 * 3600, stale: true })
    expect(backupSummary(old, 36)).toEqual({
      tone: 'warn',
      text: 'The newest database backup is from 40 h ago — older than the 36 hours it should be. Check that the nightly job is running.',
    })
  })

  test('a newer attempt that did not finish is said, so a fresh-looking row is not mistaken for a backup', () => {
    const unfinished = entry({ backup_id: '20261101T170000Z', status: 'incomplete', note: 'the dump has no manifest: it did not finish', age_seconds: 600 })
    const result = backupSummary(kind({ backups: [unfinished, entry({ age_seconds: 3 * 86_400 })], newest_ok_age_seconds: 3 * 86_400, stale: true }), 36)
    expect(result.tone).toBe('warn')
    expect(result.text).toBe(
      'The newest database backup is from 3 days ago — older than the 36 hours it should be. Check that the nightly job is running. The latest attempt did not finish and cannot be restored from.',
    )
  })

  test('only unfinished attempts is the same as none', () => {
    const only = kind({ backups: [entry({ status: 'incomplete' })], newest_ok_age_seconds: null, stale: true })
    expect(backupSummary(only, 36).text).toBe('No database backup has been made, so there is nothing to restore the database from.')
  })
})

test.describe('what "Back up now" reports', () => {
  const run = (pruned: string[]): PostgresBackupRun => ({ backup: entry({ size_bytes: 5 * 1024 * 1024 + 300_000 }), pruned })

  test('names the backup and its size, and says it was checked', () => {
    expect(backupRunSummary(run([]))).toBe('Backed up as 20261101T171500Z (5.3 MB, checked).')
  })

  test('says how many older ones the retention rule removed', () => {
    expect(backupRunSummary(run(['a']))).toBe('Backed up as 20261101T171500Z (5.3 MB, checked). Removed 1 older backup under the retention rule.')
    expect(backupRunSummary(run(['a', 'b']))).toBe('Backed up as 20261101T171500Z (5.3 MB, checked). Removed 2 older backups under the retention rule.')
  })
})

test.describe('what a row holds', () => {
  test('a database backup: tables, rows, and where the audit log stood', () => {
    expect(backupContents('postgres', entry({ table_count: 52, total_rows: 12_345, audit_tip_sequence: 41 }))).toBe('52 tables, 12,345 rows, audit entry 41')
    expect(backupContents('postgres', entry({ table_count: 52, total_rows: 0, audit_tip_sequence: null }))).toBe('52 tables, 0 rows, audit log empty')
  })

  test('a lake backup: files and rows', () => {
    expect(backupContents('lake', entry({ files: 3, total_rows: 150 }))).toBe('3 files, 150 rows')
  })

  test('an unusable backup says why instead', () => {
    expect(backupContents('postgres', entry({ status: 'incomplete', note: 'the dump file is missing' }))).toBe('the dump file is missing')
    expect(backupContents('postgres', entry({ status: 'incomplete', note: null }))).toBe('not usable')
  })
})
