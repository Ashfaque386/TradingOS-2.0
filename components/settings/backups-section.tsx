'use client'

import { useState } from 'react'
import { AlertTriangle, CheckCircle2, KeyRound, RefreshCw, XCircle } from 'lucide-react'
import { ErrorNotice } from '@/components/ui/error-notice'
import { useAuth } from '@/components/auth/auth-provider'
import { type BackupKind, ApiError, getBackups, runPostgresBackup } from '@/lib/api'
import {
  backupContents,
  backupRunSummary,
  backupSummary,
  formatAge,
  formatBytes,
  formatMadeAt,
  shortHash,
} from '@/lib/backups'
import { can } from '@/lib/permissions'
import { usePolling } from '@/lib/use-polling'

// What is backed up, how old the newest backup is, and what a restore needs that no backup holds. The
// rows come from the manifests alone (nothing is re-read here); the run that makes each backup has
// already checked it. "Back up now" does exactly what the nightly job does.

const KIND_TITLE: Record<BackupKind['kind'], string> = { postgres: 'Database (Postgres)', lake: 'Data lake' }

function KindPanel({ kind, staleAfterHours, children }: { kind: BackupKind; staleAfterHours: number; children?: React.ReactNode }) {
  const summary = backupSummary(kind, staleAfterHours)
  const tokenClass = summary.tone === 'ok' ? 'valid' : kind.newest_ok_age_seconds === null ? 'rejected' : 'expired'
  return (
    <div className="mb-5" data-testid={`backups-${kind.kind}`}>
      <p className="set-group-label">{KIND_TITLE[kind.kind]}</p>
      <div className={`set-token-strip ${tokenClass}`} data-testid={`backup-summary-${kind.kind}`} data-tone={summary.tone} role={summary.tone === 'ok' ? undefined : 'alert'}>
        {summary.tone === 'ok' ? <CheckCircle2 className="size-4 shrink-0" /> : <AlertTriangle className="size-4 shrink-0" />}
        <span>{summary.text}</span>
      </div>
      {children}
      {kind.backups.length > 0 && (
        <div className="set-matrix mt-3 table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Made</th>
                <th scope="col">Size</th>
                <th scope="col">Contents</th>
                <th scope="col">SHA-256</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {kind.backups.map((b) => (
                <tr key={b.backup_id} data-testid="backup-row" data-status={b.status}>
                  <td>
                    {formatMadeAt(b.created_at)}
                    <br />
                    <small>{formatAge(b.age_seconds)}</small>
                  </td>
                  <td>{formatBytes(b.size_bytes)}</td>
                  <td>
                    {backupContents(kind.kind, b)}
                    {b.alembic_revision && (
                      <>
                        <br />
                        <small>schema {b.alembic_revision}</small>
                      </>
                    )}
                  </td>
                  <td>
                    <code title={b.sha256 ?? undefined}>{shortHash(b.sha256)}</code>
                  </td>
                  <td>
                    {b.status === 'ok' ? (
                      <span className="set-conn set-conn-connected"><CheckCircle2 className="size-3" /> Usable</span>
                    ) : (
                      <span className="set-conn set-conn-error"><XCircle className="size-3" /> Not usable</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="set-inbound-note mt-2">
        Folder: <code>{kind.directory}</code>. Newest first, at most ten shown.
      </p>
    </div>
  )
}

export function BackupsSection() {
  const { role } = useAuth()
  const allowed = can(role, 'GET /api/v1/system/backups')
  const canRun = can(role, 'POST /api/v1/system/postgres-backup')
  const { data, error, loading, lastUpdated, refresh } = usePolling(getBackups, { intervalMs: 60_000, enabled: allowed })
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null)

  async function backUpNow() {
    setBusy(true)
    setOutcome(null)
    try {
      const run = await runPostgresBackup()
      setOutcome({ ok: true, text: backupRunSummary(run) })
    } catch (err) {
      setOutcome({ ok: false, text: err instanceof ApiError ? err.message : 'The backup did not finish.' })
    } finally {
      setBusy(false)
      await refresh()
    }
  }

  return (
    <div className="set-block" aria-label="System and backups" data-testid="backups-section">
      <div className="set-block-head">
        <div>
          <h2>System and backups</h2>
          <p>What the nightly backups hold, how old the newest is, and what a restore needs that no backup contains.</p>
        </div>
      </div>

      {!allowed && <p className="text-xs text-muted-foreground">Read-only — SystemAdministrator role required.</p>}
      {allowed && error && <ErrorNotice message={`Could not read the backups: ${error}`} onRetry={() => void refresh()} className="mb-3" />}
      {allowed && loading && !data && !error && <p className="set-inbound-note">Reading the backup folders…</p>}

      {allowed && data && (
        <>
          <p className="set-inbound-note mb-4" data-testid="backup-schedule">
            {data.schedule}. Keeps the newest {data.keep_count} and any younger than {data.retention_days} days; the newest complete
            backup is never removed. Warns when the newest is older than {data.stale_after_hours} hours.
            {lastUpdated && ` Checked ${lastUpdated.toLocaleTimeString()}.`}
          </p>

          <KindPanel kind={data.postgres} staleAfterHours={data.stale_after_hours}>
            {canRun && (
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <button className="set-btn set-btn-secondary" data-testid="backup-now" disabled={busy} onClick={() => void backUpNow()}>
                  <RefreshCw className={`size-3.5 ${busy ? 'animate-spin' : ''}`} /> {busy ? 'Backing up…' : 'Back up now'}
                </button>
                {busy && <span className="set-inbound-note">Dumping and checking the database. This can take a few minutes.</span>}
              </div>
            )}
            {outcome && (
              <div
                role={outcome.ok ? 'status' : 'alert'}
                data-testid="backup-result"
                className={`set-test-result mt-3 ${outcome.ok ? 'ok' : 'fail'}`}
              >
                {outcome.ok ? <CheckCircle2 className="size-3.5" /> : <XCircle className="size-3.5" />}
                {outcome.text}
              </div>
            )}
          </KindPanel>

          <KindPanel kind={data.lake} staleAfterHours={data.stale_after_hours} />

          <div className="set-reminder" data-testid="key-custody">
            <KeyRound className="mt-0.5 size-3.5 shrink-0" />
            <div>
              <strong>What a database backup does not hold</strong>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                <li>
                  <code>SECRETS_ENCRYPTION_KEY</code> is not in it. Without that key the saved broker, LLM and notification credentials
                  cannot be read after a restore, and each has to be entered again. Keep the key somewhere other than this machine.
                </li>
                <li>
                  The dump itself holds the login signing keys and password hashes in plain text. Keep copies of it as carefully as the key.
                </li>
                <li>
                  These backups sit on the same machine as the database. Copy the newest dump somewhere else too; if this machine is lost,
                  so are they.
                </li>
              </ul>
              <p className="mt-1">How to restore, and what the restore checks: <code>docs/runbooks/postgres-restore.md</code>.</p>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
