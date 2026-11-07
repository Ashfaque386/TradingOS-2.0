# Postgres backup and restore

The database is the system of record: orders, fills, positions, the audit log, users, approvals, strategy versions,
the login signing keys. Until Phase 7 nothing copied it anywhere. Now a dump is made every night and can be put back,
and the restore checks itself.

## What is made, when, and where

| | |
|---|---|
| When | every day at 22:45 IST (after the data lake backup at 22:15), weekends included: the database changes whether or not the market is open. `Settings > System and backups > Back up now` runs the same job on request. |
| What | `postgres-<UTC timestamp>.dump` (`pg_dump --format=custom`) and `postgres-<UTC timestamp>.manifest.json` beside it. The dump and the figures in the manifest (schema revision, audit log tip, every table's row count) come from **one** database snapshot, so the manifest describes the dump exactly. |
| Where | the `tradingos-2.0_backups` volume, mounted at `/app/backups` in the backend container. A volume of its own, apart from `tradingos-2.0_pgdata`, so rebuilding or losing the database volume does not take the backups with it. |
| Kept | the newest 7 (`POSTGRES_BACKUP_KEEP_COUNT`) and any younger than 14 days (`POSTGRES_BACKUP_RETENTION_DAYS`). The newest complete backup is never removed. A dump with no manifest is an unfinished backup: it is listed as not usable and removed after a day. |
| Checked | each backup is checked as it is made: SHA-256, size, and `pg_restore --list` of the finished file must agree with what was written. A backup that does not check out is not kept. |
| If it fails | a critical engine alert (`The Postgres backup failed`) goes to the channels subscribed to Engine alerts, and clears itself, with a notice, the night a backup goes through. A night the job never ran (the backend was down at 22:45 IST) cannot raise its own alert: Settings warns when the newest backup is more than 36 hours old (`BACKUP_STALE_AFTER_HOURS`), and Prometheus's `PostgresBackupStale` rule (critical; `LakeBackupStale` is a warning) sends the same through Alertmanager to the Engine-alerts channels. |

The files are readable by their owner only. **The dump holds the login signing keys and password hashes in plain
text**: copy it as carefully as you would the keys.

## What a backup does not hold, and what is lost with each thing

| If you lose... | You lose... | How you get back |
|---|---|---|
| the database volume (`pgdata`) | everything in the database | restore the newest backup (below). Anything since the backup is gone: up to a day, unless you back up by hand first. |
| `SECRETS_ENCRYPTION_KEY` | the saved broker, LLM and notification credentials (they are sealed with it, in the `secrets` volume, which is not in the database). Nothing else. | enter each credential again in Settings. The key is not in any backup: **keep it somewhere other than this machine**. |
| the `secrets` volume | the same credentials, even with the key | enter them again. |
| the `backups` volume and the database together | all of it | nothing. This is why the newest dump should also live somewhere else (next section). |
| the `audit_archive` volume | the independent copy of the audit chain. The database still has the chain; the archive is rebuilt from it, but the independent proof is gone. | none needed for operation; keep it with the dumps if you value the independent copy. |
| `data_lake` and its backups | price history | refetch (the data lake has its own nightly backup). |
| Redis, Qdrant, Prometheus, Grafana volumes | live price ticks, agent memory vectors, metrics history | not in this backup. Agent memory (Qdrant) has no backup yet. |

## Keep a copy somewhere else

The `backups` volume is on the same machine as the database. If the machine goes, so do both. Copy the newest dump off
it regularly, to a place you would trust with the signing keys:

```bash
docker cp TradingOS-2.0-Backend:/app/backups/. ./offsite-backups/
```

## Look at what you have

```bash
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres list
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres verify latest
```

`list` shows each backup with its size, table and row counts, the audit log's tip and the schema revision, and marks
anything unusable. `verify` re-reads the dump and compares it with its manifest: checksum, size, and what
`pg_restore` makes of it. A backup that fails `verify` is **not safe to restore from**; use the next one.

From a machine that has only a copy of the folder, give `--root`: `python -m scripts.restore_postgres --root ./offsite-backups list`
(it needs the backend's Python dependencies and the Postgres client tools, so run it in the backend image or a
checkout with the dev install).

## Restore

The tool drops the target database, creates it empty, loads the dump, then checks the result against the manifest. It
refuses a backup that does not verify before it drops anything. It will not touch the database the application is
configured to use unless you add `--replace-live`.

### First, restore beside the live database and look

This is always safe, and it is how you find out what a backup holds:

```bash
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres restore latest --into tradingos_check --confirm tradingos_check
```

`--confirm` repeats the name: the target is dropped first. The output ends with what was checked:

- every table has the row count the manifest recorded;
- every audit entry's hash chain verifies, and the audit log's last entry is the one the manifest recorded;
- the audit log's append-only trigger is in place (a restore that lost it would let history be rewritten);
- the schema is brought up to the code's version with `alembic upgrade head` when the backup is older (`--no-migrate` skips it).

If any check fails the tool says which and exits non-zero. Drop the check database when you are done:
`docker exec TradingOS-2.0-Backend psql -U tradingos -d postgres -c "DROP DATABASE tradingos_check"`.

### The database is lost (new empty volume)

1. Start the stack. A fresh `pgdata` volume is initialised with an empty, migrated `tradingos` database.
2. Restore over it. The API's connections are terminated by the drop; restart the backend straight after.
   ```bash
   docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres restore latest --into tradingos --confirm tradingos --replace-live
   docker compose restart backend
   ```
3. Put `SECRETS_ENCRYPTION_KEY` back in `.env` **before** step 1, from your own copy, or enter the credentials again.
4. Everyone is signed in again as of the backup: the signing keys came back with the database, so tokens issued before
   the backup still verify and later ones do not. Expect to log in again.

### The data is wrong and you want yesterday's (the database is still there)

Restore beside, check it, then point the stack at it. The current database stays untouched as the way back:

```bash
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres restore 20261101T171500Z --into tradingos_restored --confirm tradingos_restored
# set POSTGRES_DB=tradingos_restored in .env, then
docker compose up -d
```

To go back, set `POSTGRES_DB` back. The container's start-up skips initialisation when the volume already holds a
cluster, so the restored database is used as it is.

### After any restore over live data: the audit archive

The audit archive is an independent, append-only copy of the audit chain. A restore to an earlier backup leaves the
archive **ahead of the database**: it holds entries the restored database no longer has. The hourly divergence check
will say so (`sequence N exists in the archive but not in the live DB`) and write an audit entry for it. That is the
audit trail doing its job: it records that history was lost, and when.

To start a consistent archive from the restored database, keep the old one as evidence and let a new one begin:

```bash
docker exec TradingOS-2.0-Backend sh -c 'mv /app/audit_archive/audit_log.ndjson /app/audit_archive/audit_log.before-restore-$(date +%Y%m%d).ndjson'
```

The next archival sweep (every five minutes) re-archives the database from its first entry. (If the file refuses to
move, it was made append-only with `chattr +a`; that needs `chattr -a` first, which the container may not be allowed.)

### Afterwards, check

- Settings > System and backups shows the newest backup, and `GET /api/v1/system/jwt-signing-key` answers.
- Paper Desk: positions and the ledger are what the backup held; the kill switch state is the backup's.
- Mission Control > scheduled jobs: the jobs are running. A restore does not restart the day's market-data work.
- Broker connections: Zerodha tokens expire daily regardless; Upstox/Zerodha credentials need the encryption key.

## Drill

A backup nobody has restored is a hope. Run this after a change to the schema or to how backups are made, and
periodically on real data (see the operator pass in the phase plan):

```bash
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres verify latest
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres restore latest --into tradingos_check --confirm tradingos_check
```

### Drill record

**2026-11-01, Phase 7, test containers** (Postgres 16.15, the backend's own `create_postgres_backup`, then the tool above;
nothing live was touched). The source was built with the real migrations (`alembic upgrade head`, schema `b4f7d2a8c1e3`),
the four fixture users, and 60 audit entries. The same sequence was run once as a superuser and once as a role that is
**not** a superuser and only owns its database, which is how the image's `tradingos` role is set up.

| Step | Result |
|---|---|
| Backup | `postgres-20261101T145952Z.dump`, 131,318 bytes, mode 0600; 52 tables, 64 rows, audit tip 60 |
| `list`, `verify latest` | "the dump matches its manifest and can be read by pg_restore" |
| `restore latest --into drill_restored` | 4 s (3 s as the non-superuser). 52 tables with the recorded row counts, 60 audit entries chain correctly, tip as recorded, append-only trigger present, schema already at head |
| `restore` into the configured database without `--replace-live` | refused, exit 2, nothing dropped |
| One byte flipped in the dump | `verify`: "checksum mismatch", exit 1; `restore`: "damaged and was not restored", exit 1, target untouched |
| Counts read straight from the restored database | `users 4`, `audit_log 60`, `alembic_version b4f7d2a8c1e3`; `alembic current` says `(head)` |

Not yet done: a drill on the live database's real data and size, and a restore through the container with `docker exec`
rather than the test harness. Both belong to the first operator session. (The live stack has made backups since its
2026-11-02 rebuild, so there is a real dump to drill on; on 2026-11-07 the volume held two, about 0.5 MB each, from 2 and
7 November. The nights between are missing because the machine was off: the job does not catch up on a missed run.)

## Troubleshooting

- **`pg_dump: error: aborting because of server version mismatch`**: the client in the image or the runner is older than the
  server. The backend image installs PostgreSQL 16 from the same repository as its server; CI installs `postgresql-client-16`.
- **The failed-backup alert says `pg_dump exited with ...`**: the text after it is what `pg_dump` printed. A full volume is the
  usual cause; `df` on the volume, then `Back up now`.
- **The stale warning or `PostgresBackupStale` with no failed alert**: the job did not run (the backend was down at 22:45 IST). The scheduler does not
  catch up on a missed run; `Back up now`, or wait for the next night.
- **A dump with no manifest**: an unfinished backup (the process died mid-dump). It is not usable and is removed after a day.
