"""List, check and restore the nightly Postgres backups (src/data/pg_backup.py).

    python -m scripts.restore_postgres list
    python -m scripts.restore_postgres verify latest
    python -m scripts.restore_postgres restore latest --into restored_db --confirm restored_db

A backup is a dump and a manifest. `verify` re-reads the dump and compares it with the manifest
(checksum, size, `pg_restore --list`). `restore` verifies first, drops and recreates the target
database, loads the dump, then checks the result against the manifest: every table's row count, the
audit log's hash chain, the audit log's tip, and the append-only trigger. It then brings the schema
up to the code's version (`alembic upgrade head`) unless `--no-migrate`.

`BACKUP` is a backup id (the timestamp in its file name) or `latest`, the newest backup that
finished. The target database is named with `--into` and named again with `--confirm`: it is
dropped first. Restoring into the database the application is configured to use also needs
`--replace-live`, and the application stopped (its connections are terminated). Restoring into a
second database to look at it first is the safe way to find out what a backup holds.

Runs wherever the Postgres client tools and the backend's dependencies are: inside the backend
container (`docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres list`), or on a
machine that has a copy of the backups folder (`--root`).
"""

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.core.config import get_settings
from src.data.pg_backup import (
    BackupInfo,
    PostgresBackupError,
    list_backups,
    load_manifest,
    restore_postgres_backup,
    verify_backup,
    verify_restored_database,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _resolve(root: Path, backup: str) -> str:
    """`latest` is the newest backup that finished; anything else is taken as an id."""
    if backup != "latest":
        return backup
    finished = [b for b in list_backups(root) if b.status == "ok"]
    if not finished:
        raise PostgresBackupError(f"there is no finished backup in {root}")
    return finished[0].backup_id


def _print_listing(found: list[BackupInfo], root: Path) -> None:
    print(f"Backups in {root} (newest first):")
    if not found:
        print("  none")
        return
    for info in found:
        if info.status == "ok":
            tip = info.audit_tip_sequence if info.audit_tip_sequence is not None else "-"
            print(
                f"  {info.backup_id}  ok  {info.size_bytes:>12,} bytes  {info.table_count} tables, "
                f"{info.total_rows:,} rows  audit tip {tip}  schema {info.alembic_revision or '-'}"
            )
        else:
            print(f"  {info.backup_id}  NOT USABLE  {info.note}")


def _target_url(into: str) -> str:
    url = make_url(get_settings().database_url).set(database=into)
    return url.render_as_string(hide_password=False)


def _migrate(target_url: str, recorded_revision: str | None) -> str:
    """Brings the restored schema up to the code's. Returns what was done, for the report."""
    if recorded_revision is None:
        return (
            "The backup records no schema revision (the database was not built by migrations), "
            "so the schema was left as it is."
        )
    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    if recorded_revision == head:
        return f"The schema is already at the code's revision ({head}); nothing to migrate."
    env = {**os.environ, "DATABASE_URL": target_url}
    subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
    )
    return f"Migrated the schema from {recorded_revision} to {head}."


async def _verify(root: Path, backup_id: str) -> int:
    result = await verify_backup(root, backup_id, database_url=get_settings().database_url)
    if result.ok:
        print(f"{backup_id}: the dump matches its manifest and can be read by pg_restore.")
        return 0
    print(f"{backup_id}: NOT SAFE TO RESTORE FROM")
    for problem in result.problems:
        print(f"  - {problem}")
    return 1


async def _restore(root: Path, backup_id: str, args: argparse.Namespace) -> int:
    live = make_url(get_settings().database_url).database
    if args.confirm != args.into:
        print(f"--confirm must repeat the target database name exactly ({args.into!r}).")
        return 2
    if args.into == live and not args.replace_live:
        print(
            f"{args.into!r} is the database the application is configured to use. Restoring into "
            "it replaces the live data: stop the API first, then add --replace-live."
        )
        return 2

    manifest = load_manifest(root, backup_id)
    target = _target_url(args.into)
    print(
        f"Checking {backup_id}, then restoring it into {args.into!r} "
        "(dropped and recreated first)..."
    )
    await restore_postgres_backup(root, backup_id, target_url=target)

    check = await verify_restored_database(root, backup_id, database_url=target)
    if not check.ok:
        print("The restored database does NOT match the backup:")
        for problem in check.problems:
            print(f"  - {problem}")
        return 1
    print(
        f"Checked against the manifest: {check.tables_compared} tables have the recorded row "
        f"counts, {check.audit_entries_checked} audit entries chain correctly, the audit log's tip "
        "is the recorded one, and its append-only trigger is in place."
    )
    if args.no_migrate:
        print(f"Schema left as it was ({manifest.alembic_revision or 'unrecorded'}); --no-migrate.")
    else:
        print(_migrate(target, manifest.alembic_revision))
    print(
        "Not in a backup: SECRETS_ENCRYPTION_KEY (without the key that sealed them, the saved "
        "broker, LLM and notification credentials cannot be read) and the other volumes' files."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, help="the backups folder (default: the configured one)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="what is in the backups folder")
    verify = commands.add_parser("verify", help="check one backup without restoring it")
    verify.add_argument("backup", help="a backup id, or 'latest'")
    restore = commands.add_parser("restore", help="load a backup into a database")
    restore.add_argument("backup", help="a backup id, or 'latest'")
    restore.add_argument("--into", required=True, help="the database to drop, create and fill")
    restore.add_argument("--confirm", required=True, help="the same name again")
    restore.add_argument(
        "--replace-live", action="store_true", help="allow the configured database"
    )
    restore.add_argument("--no-migrate", action="store_true", help="skip alembic upgrade head")
    args = parser.parse_args(argv)

    root = args.root or Path(get_settings().postgres_backup_path)
    try:
        if args.command == "list":
            _print_listing(list_backups(root), root)
            return 0
        backup_id = _resolve(root, args.backup)
        if args.command == "verify":
            return asyncio.run(_verify(root, backup_id))
        return asyncio.run(_restore(root, backup_id, args))
    except (PostgresBackupError, subprocess.CalledProcessError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
