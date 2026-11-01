"""Backup, verification, retention and restore of the system of record (Postgres).

The data lake has had a nightly checksummed backup since phase 14 (`src.data.backup`). The
database -- users, the hash-chained audit log, strategies, subscriptions, positions, fills,
approvals, equity history -- had none: one `docker compose down -v`, a corrupted volume or a bad
migration was unrecoverable. This is that backup, and the way back.

**What a backup is.** One `pg_dump --format=custom` file and, beside it, a manifest
(`postgres-<id>.dump`, `postgres-<id>.manifest.json`; <id> is the UTC time). The dump is written
to a `.partial` name and renamed when pg_dump has finished, and the manifest is written after the
dump, so a dump with no manifest is a backup that did not finish and is reported as incomplete,
never as a backup. Both files are readable by their owner only: **a dump contains the JWT signing
keys in plaintext and every user's password hash -- treat it as a secret**
(docs/runbooks/backup-restore.md).

**What the manifest holds, and why it can be trusted.** The SHA-256 and size of the dump, the
Alembic revision, the audit log's tip (its sequence and hash), the row count of every table, and
what `pg_restore --list` made of the dump. The figures are read inside a REPEATABLE READ
transaction whose snapshot is exported (`pg_export_snapshot`) and handed to `pg_dump --snapshot`,
so they describe the dump exactly, not the database a moment later. A restore is then checked
against them: the same counts, the same audit tip, an audit chain that still verifies, and the
append-only trigger still in place.

**What it does not do.** It does not back up `SECRETS_ENCRYPTION_KEY` (the key that opens the
encrypted broker, LLM and notification stores: lose it and those credentials are gone, whatever
else is restored) or the stores themselves. Where the key and the dumps live is the operator's
decision, and the runbook says what is lost with each."""

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401 - registers every table on the metadata (the manifest's list)
from src.audit.service import verify_db_chain
from src.models.base import Base

logger = structlog.get_logger(__name__)

DUMP_SUFFIX = ".dump"
MANIFEST_SUFFIX = ".manifest.json"
PARTIAL_SUFFIX = ".partial"
_PREFIX = "postgres-"
_ID_FORMAT = "%Y%m%dT%H%M%SZ"
_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")
_DB_NAME = re.compile(r"^[A-Za-z0-9_]{1,63}$")
_OUTPUT_TAIL_CHARS = 600
# A leftover from a dump that died is removed once it is this old (a running dump is never touched).
_STALE_LEFTOVER = timedelta(hours=24)


class PostgresBackupError(Exception):
    """A backup, verification or restore that could not be done; the message says what and why."""


@dataclass(frozen=True, slots=True)
class PostgresBackupManifest:
    backup_id: str
    created_at: str
    dump_file: str
    size_bytes: int
    sha256: str
    alembic_revision: str | None
    audit_tip_sequence: int | None
    audit_tip_hash: str | None
    tables: dict[str, int]
    toc_entries: int
    toc_sha256: str
    pg_dump_version: str
    server_version: str


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One backup as found on disk. `status` is `ok` when both files are there and the manifest
    reads, `incomplete` for anything else (a dump with no manifest, a manifest with no dump, an
    unreadable manifest): not a backup anyone should restore from. Listing never re-reads the dump,
    so it is cheap; `verify_backup` is the check that does."""

    backup_id: str
    status: str
    created_at: datetime
    size_bytes: int
    sha256: str | None = None
    alembic_revision: str | None = None
    audit_tip_sequence: int | None = None
    audit_tip_hash: str | None = None
    table_count: int = 0
    total_rows: int = 0
    toc_entries: int = 0
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    ok: bool
    problems: tuple[str, ...] = ()
    audit_entries_checked: int = 0
    tables_compared: int = 0


@dataclass(frozen=True, slots=True)
class _Facts:
    alembic_revision: str | None
    audit_tip_sequence: int | None
    audit_tip_hash: str | None
    tables: dict[str, int] = field(default_factory=dict)
    server_version: str = ""


def backup_is_stale(newest_ok_age_seconds: float | None, stale_after_hours: float) -> bool:
    """No usable backup, or the newest is older than the limit: the one rule Settings, the API and
    the Prometheus gauge all use."""
    return newest_ok_age_seconds is None or newest_ok_age_seconds > stale_after_hours * 3600


# ---- paths ----


def dump_path(root: Path, backup_id: str) -> Path:
    return root / f"{_PREFIX}{backup_id}{DUMP_SUFFIX}"


def manifest_path(root: Path, backup_id: str) -> Path:
    return root / f"{_PREFIX}{backup_id}{MANIFEST_SUFFIX}"


def _id_from(name: str, suffix: str) -> str | None:
    if not (name.startswith(_PREFIX) and name.endswith(suffix)):
        return None
    candidate = name[len(_PREFIX) : -len(suffix)]
    return candidate if _ID_PATTERN.match(candidate) else None


def _created_at(backup_id: str) -> datetime:
    return datetime.strptime(backup_id, _ID_FORMAT).replace(tzinfo=UTC)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owner_only(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # a file system with no modes (a Windows bind mount): nothing to tighten
        logger.warning("pg_backup.could_not_restrict_file", path=str(path))


# ---- running the Postgres tools ----


def _connection_env(database_url: str) -> dict[str, str]:
    """The PG* variables for one connection. The password travels in the environment, never in the
    argument list, where `ps` would show it."""
    url = make_url(database_url)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
    if url.database:
        env["PGDATABASE"] = url.database
    return env


async def _run(
    args: list[str], *, env: dict[str, str], timeout: float, what: str
) -> tuple[str, str]:
    """Runs a Postgres client tool and returns (stdout, stderr). Any failure -- the tool missing, a
    non-zero exit, a timeout -- is a `PostgresBackupError` saying which tool and what it printed."""
    try:
        # A fixed argv built from this module's own constants and a path it just chose (no shell, no
        # user input in the command).
        process = await asyncio.create_subprocess_exec(  # nosec B603
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PostgresBackupError(
            f"{what}: {args[0]} is not installed here (install the PostgreSQL 16 client tools)"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PostgresBackupError(f"{what}: {args[0]} did not finish in {timeout:.0f}s") from exc
    out, err = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    if process.returncode != 0:
        tail = err.strip()[-_OUTPUT_TAIL_CHARS:]
        raise PostgresBackupError(f"{what}: {args[0]} exited with {process.returncode}: {tail}")
    return out, err


async def _tool_version(tool: str, env: dict[str, str]) -> str:
    out, _ = await _run([tool, "--version"], env=env, timeout=30, what="version check")
    return out.strip()


def _toc_summary(listing: str) -> tuple[int, str]:
    """How many objects `pg_restore --list` found, and a digest of the listing. Lines starting with
    `;` are its header comments."""
    entries = [line for line in listing.splitlines() if line.strip() and not line.startswith(";")]
    return len(entries), hashlib.sha256(listing.encode("utf-8")).hexdigest()


# ---- creating a backup ----


async def _read_facts(db: AsyncSession) -> _Facts:
    revision: str | None = None
    if (await db.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar_one():
        revision = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    tip = (
        await db.execute(
            text("SELECT sequence, hash FROM audit_log ORDER BY sequence DESC LIMIT 1")
        )
    ).first()
    tables: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counted = await db.execute(text(f'SELECT count(*) FROM "{table.name}"'))  # nosec B608
        tables[table.name] = int(counted.scalar_one())
    server = (await db.execute(text("SHOW server_version"))).scalar_one()
    return _Facts(
        alembic_revision=revision,
        audit_tip_sequence=int(tip[0]) if tip else None,
        audit_tip_hash=str(tip[1]) if tip else None,
        tables=tables,
        server_version=str(server),
    )


async def create_postgres_backup(
    session_factory: async_sessionmaker[AsyncSession],
    root: Path,
    *,
    database_url: str,
    timeout_seconds: float = 1800.0,
    now: datetime | None = None,
) -> PostgresBackupManifest:
    """Dumps the database to `root` and returns its manifest. The dump is checked before this
    returns (readable by `pg_restore --list`, checksummed); if anything fails nothing is left behind
    that looks like a backup."""
    root.mkdir(parents=True, exist_ok=True)
    backup_id = (now or datetime.now(UTC)).strftime(_ID_FORMAT)
    final, partial = (
        dump_path(root, backup_id),
        dump_path(root, backup_id).with_suffix(DUMP_SUFFIX + PARTIAL_SUFFIX),
    )
    if final.exists() or manifest_path(root, backup_id).exists():
        raise PostgresBackupError(f"a backup with the id {backup_id} already exists")
    env = _connection_env(database_url)
    dump_version = await _tool_version("pg_dump", env)

    try:
        async with session_factory() as db:
            # One snapshot for the facts and the dump, so the manifest describes the dump exactly.
            await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            snapshot = (await db.execute(text("SELECT pg_export_snapshot()"))).scalar_one()
            facts = await _read_facts(db)
            await _run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    f"--snapshot={snapshot}",
                    f"--file={partial}",
                ],
                env=env,
                timeout=timeout_seconds,
                what="backup",
            )
            await db.rollback()
        listing, _ = await _run(
            ["pg_restore", "--list", str(partial)], env=env, timeout=300, what="backup check"
        )
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _owner_only(final)

    toc_entries, toc_sha = _toc_summary(listing)
    manifest = PostgresBackupManifest(
        backup_id=backup_id,
        created_at=_created_at(backup_id).isoformat(),
        dump_file=final.name,
        size_bytes=final.stat().st_size,
        sha256=await asyncio.to_thread(_sha256_of, final),
        alembic_revision=facts.alembic_revision,
        audit_tip_sequence=facts.audit_tip_sequence,
        audit_tip_hash=facts.audit_tip_hash,
        tables=facts.tables,
        toc_entries=toc_entries,
        toc_sha256=toc_sha,
        pg_dump_version=dump_version,
        server_version=facts.server_version,
    )
    target = manifest_path(root, backup_id)
    temporary = target.with_suffix(target.suffix + PARTIAL_SUFFIX)
    temporary.write_text(json.dumps(asdict(manifest), indent=2), encoding="utf-8")
    _owner_only(temporary)
    os.replace(temporary, target)
    logger.info(
        "pg_backup.created",
        backup_id=backup_id,
        size_bytes=manifest.size_bytes,
        tables=len(manifest.tables),
        audit_tip=manifest.audit_tip_sequence,
    )
    return manifest


# ---- reading what is on disk ----


def load_manifest(root: Path, backup_id: str) -> PostgresBackupManifest:
    path = manifest_path(root, backup_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PostgresBackupManifest(**data)
    except (OSError, ValueError, TypeError) as exc:
        raise PostgresBackupError(f"the manifest for {backup_id} cannot be read: {exc}") from exc


def list_backups(root: Path) -> list[BackupInfo]:
    """Every backup (and every unfinished one) in `root`, newest first."""
    if not root.is_dir():
        return []
    ids: set[str] = set()
    for entry in root.iterdir():
        for suffix in (DUMP_SUFFIX, MANIFEST_SUFFIX):
            found = _id_from(entry.name, suffix)
            if found:
                ids.add(found)
    found_backups = [_info(root, backup_id) for backup_id in ids]
    return sorted(found_backups, key=lambda info: info.backup_id, reverse=True)


def _info(root: Path, backup_id: str) -> BackupInfo:
    dump, manifest_file = dump_path(root, backup_id), manifest_path(root, backup_id)
    created = _created_at(backup_id)
    size = dump.stat().st_size if dump.exists() else 0
    if not manifest_file.exists():
        return BackupInfo(
            backup_id,
            "incomplete",
            created,
            size,
            note="the dump has no manifest: it did not finish",
        )
    if not dump.exists():
        return BackupInfo(backup_id, "incomplete", created, 0, note="the dump file is missing")
    try:
        manifest = load_manifest(root, backup_id)
    except PostgresBackupError as exc:
        return BackupInfo(backup_id, "incomplete", created, size, note=str(exc))
    return BackupInfo(
        backup_id=backup_id,
        status="ok",
        created_at=created,
        size_bytes=size,
        sha256=manifest.sha256,
        alembic_revision=manifest.alembic_revision,
        audit_tip_sequence=manifest.audit_tip_sequence,
        audit_tip_hash=manifest.audit_tip_hash,
        table_count=len(manifest.tables),
        total_rows=sum(manifest.tables.values()),
        toc_entries=manifest.toc_entries,
    )


async def verify_backup(
    root: Path, backup_id: str, *, database_url: str, timeout_seconds: float = 300.0
) -> Verification:
    """Re-reads the dump and checks it against its manifest: the size, the SHA-256, and that
    `pg_restore --list` still reads it and finds what it found when the backup was made. A bit
    flipped on disk, a truncated copy, a swapped file all show up here."""
    try:
        manifest = load_manifest(root, backup_id)
    except PostgresBackupError as exc:
        return Verification(False, (str(exc),))
    dump = dump_path(root, backup_id)
    if not dump.exists():
        return Verification(False, (f"{dump.name} is missing",))
    problems: list[str] = []
    if dump.stat().st_size != manifest.size_bytes:
        problems.append(
            f"size {dump.stat().st_size} does not match the manifest's {manifest.size_bytes}"
        )
    actual = await asyncio.to_thread(_sha256_of, dump)
    if actual != manifest.sha256:
        problems.append(f"checksum mismatch (expected {manifest.sha256}, got {actual})")
    try:
        listing, _ = await _run(
            ["pg_restore", "--list", str(dump)],
            env=_connection_env(database_url),
            timeout=timeout_seconds,
            what="backup check",
        )
    except PostgresBackupError as exc:
        problems.append(f"the dump cannot be read: {exc}")
    else:
        entries, digest = _toc_summary(listing)
        if entries != manifest.toc_entries or digest != manifest.toc_sha256:
            problems.append(
                f"its contents differ from the manifest's ({entries} objects, expected "
                f"{manifest.toc_entries})"
            )
    return Verification(not problems, tuple(problems))


# ---- retention ----


def prune_backups(
    root: Path, *, keep_count: int, retention_days: int, now: datetime | None = None
) -> list[str]:
    """Removes old backups and returns their ids. A backup is kept if it is one of the newest
    `keep_count` or younger than `retention_days` -- and the newest complete backup is always kept,
    whatever the settings say. Unfinished leftovers are removed once a day old (so a dump that is
    still being written is never touched). Files that are not ours are left alone."""
    now = now or datetime.now(UTC)
    complete = [info for info in list_backups(root) if info.status == "ok"]
    keep = {info.backup_id for info in complete[: max(keep_count, 1)]}
    cutoff = now - timedelta(days=retention_days)
    keep |= {info.backup_id for info in complete if info.created_at >= cutoff}
    removed: list[str] = []
    for info in list_backups(root):
        if info.backup_id in keep:
            continue
        if info.status != "ok" and now - info.created_at < _STALE_LEFTOVER:
            continue
        dump_path(root, info.backup_id).unlink(missing_ok=True)
        manifest_path(root, info.backup_id).unlink(missing_ok=True)
        removed.append(info.backup_id)
    for leftover in root.glob(f"{_PREFIX}*{PARTIAL_SUFFIX}") if root.is_dir() else []:
        age = now - datetime.fromtimestamp(leftover.stat().st_mtime, tz=UTC)
        if age >= _STALE_LEFTOVER:
            leftover.unlink(missing_ok=True)
    if removed:
        logger.info("pg_backup.pruned", removed=removed)
    return removed


# ---- restoring ----


async def restore_postgres_backup(
    root: Path,
    backup_id: str,
    *,
    target_url: str,
    timeout_seconds: float = 3600.0,
) -> None:
    """Restores the backup into the database `target_url` names: the database is dropped
    (connections to it terminated) and created empty, then loaded by `pg_restore`. The backup is
    verified first; a damaged one is refused before anything is dropped. A schema older than the
    code's is brought up to date by the caller (`alembic upgrade head`), which the restore script
    does.

    This replaces whatever is in that database. The script that calls it asks for confirmation when
    the database is the live one."""
    check = await verify_backup(root, backup_id, database_url=target_url)
    if not check.ok:
        raise PostgresBackupError(
            f"backup {backup_id} is damaged and was not restored: {'; '.join(check.problems)}"
        )
    url = make_url(target_url)
    name = url.database or ""
    if not _DB_NAME.match(name):
        raise PostgresBackupError(f"{name!r} is not a database name this restore will touch")
    env = _connection_env(target_url)

    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    await _run(
        [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            f"--dbname={name}",
            str(dump_path(root, backup_id)),
        ],
        env=env,
        timeout=timeout_seconds,
        what="restore",
    )
    logger.info("pg_backup.restored", backup_id=backup_id, database=name)


async def verify_restored_database(
    root: Path, backup_id: str, *, database_url: str
) -> RestoreVerification:
    """Checks a restored database against the backup's manifest: every table has the rows the
    backup recorded, the audit chain still verifies end to end, its tip is the recorded one, and the
    trigger that makes it append-only is in place. (If the schema has since been migrated forward,
    new tables simply are not in the manifest; the revision is reported, not compared.)"""
    manifest = load_manifest(root, backup_id)
    engine = create_async_engine(database_url)
    problems: list[str] = []
    checked = 0
    entries = 0
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            for table, expected in manifest.tables.items():
                counted = await db.execute(text(f'SELECT count(*) FROM "{table}"'))  # nosec B608
                actual = int(counted.scalar_one())
                checked += 1
                if actual != expected:
                    problems.append(f"{table}: {actual} rows, the backup recorded {expected}")
            chain = await verify_db_chain(db)
            entries = chain.entries_checked
            if not chain.valid:
                problems.append(f"the audit chain does not verify: {chain.reason}")
            tip = (
                await db.execute(
                    text("SELECT sequence, hash FROM audit_log ORDER BY sequence DESC LIMIT 1")
                )
            ).first()
            if (int(tip[0]) if tip else None) != manifest.audit_tip_sequence or (
                str(tip[1]) if tip else None
            ) != manifest.audit_tip_hash:
                problems.append("the audit log's tip is not the one the backup recorded")
            trigger = (
                await db.execute(
                    text("SELECT 1 FROM pg_trigger WHERE tgname = 'audit_log_append_only'")
                )
            ).first()
            if trigger is None:
                problems.append("the audit log's append-only trigger is missing")
    finally:
        await engine.dispose()
    return RestoreVerification(not problems, tuple(problems), entries, checked)
