"""Backup, verification, retention and restore of the database (src.data.pg_backup).

The round trips use the real `pg_dump`/`pg_restore` against the test database and restore into a
scratch database of their own; they are skipped where the Postgres client tools are not installed
(CI installs them). The retention and listing logic is exercised on hand-made files, with no
Postgres at all.
"""

import asyncio
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.audit.service import write_audit_entry
from src.core.config import get_settings
from src.data import pg_backup
from src.data.pg_backup import (
    PostgresBackupError,
    create_postgres_backup,
    dump_path,
    list_backups,
    load_manifest,
    manifest_path,
    prune_backups,
    restore_postgres_backup,
    verify_backup,
    verify_restored_database,
)
from src.models.base import Base
from src.models.user import User

needs_pg_tools = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="the PostgreSQL client tools are not installed",
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_in_ci_the_client_tools_are_there_so_the_round_trips_cannot_be_skipped_unseen():
    if not os.environ.get("CI"):
        pytest.skip("only CI must have the tools; a developer's machine may not")

    assert shutil.which("pg_dump") and shutil.which(
        "pg_restore"
    ), "install postgresql-client-16 in the CI job (see the step in ci.yml)"


def _id(days_ago: float, *, hour: int = 17) -> str:
    moment = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=15, second=0)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _fake_backup(root: Path, backup_id: str, *, manifest: bool = True, dump: bool = True) -> None:
    """A backup as `list_backups` finds it, without Postgres: the files are not real dumps."""
    root.mkdir(parents=True, exist_ok=True)
    if dump:
        dump_path(root, backup_id).write_bytes(b"not a real dump")
    if manifest:
        manifest_path(root, backup_id).write_text(
            json.dumps(
                {
                    "backup_id": backup_id,
                    "created_at": "x",
                    "dump_file": f"postgres-{backup_id}.dump",
                    "size_bytes": 15,
                    "sha256": "ab" * 32,
                    "alembic_revision": "rev1",
                    "audit_tip_sequence": 7,
                    "audit_tip_hash": "cd" * 32,
                    "tables": {"users": 2, "audit_log": 5},
                    "toc_entries": 40,
                    "toc_sha256": "ef" * 32,
                    "pg_dump_version": "pg_dump (PostgreSQL) 16.3",
                    "server_version": "16.3",
                }
            ),
            encoding="utf-8",
        )


# ---- listing, on hand-made files ----


def test_backups_are_listed_newest_first_with_what_the_manifest_says(tmp_path):
    for days in (3, 1, 2):
        _fake_backup(tmp_path, _id(days))

    found = list_backups(tmp_path)

    assert [b.backup_id for b in found] == [_id(1), _id(2), _id(3)]
    newest = found[0]
    assert newest.status == "ok"
    assert newest.created_at == datetime.strptime(_id(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    assert (newest.sha256, newest.alembic_revision) == ("ab" * 32, "rev1")
    assert (newest.audit_tip_sequence, newest.audit_tip_hash) == (7, "cd" * 32)
    assert (newest.table_count, newest.total_rows, newest.toc_entries) == (2, 7, 40)
    assert newest.size_bytes == len(b"not a real dump")


def test_a_dump_with_no_manifest_is_an_unfinished_backup_not_a_backup(tmp_path):
    _fake_backup(tmp_path, _id(1), manifest=False)

    [info] = list_backups(tmp_path)

    assert info.status == "incomplete"
    assert info.note == "the dump has no manifest: it did not finish"
    assert info.sha256 is None


def test_a_manifest_with_no_dump_is_incomplete_too(tmp_path):
    _fake_backup(tmp_path, _id(1), dump=False)

    [info] = list_backups(tmp_path)

    assert (info.status, info.note, info.size_bytes) == (
        "incomplete",
        "the dump file is missing",
        0,
    )


def test_a_manifest_that_cannot_be_read_makes_the_backup_incomplete(tmp_path):
    _fake_backup(tmp_path, _id(1))
    manifest_path(tmp_path, _id(1)).write_text("{not json", encoding="utf-8")

    [info] = list_backups(tmp_path)

    assert info.status == "incomplete"
    assert info.note.startswith(f"the manifest for {_id(1)} cannot be read")


def test_files_that_are_not_backups_are_ignored(tmp_path):
    _fake_backup(tmp_path, _id(1))
    for name in (
        "notes.txt",
        "postgres-latest.dump",
        "postgres-20261110.dump",
        f"postgres-{_id(1)}.dump.partial",
        f"other-{_id(1)}.dump",
    ):
        (tmp_path / name).write_bytes(b"x")

    assert [b.backup_id for b in list_backups(tmp_path)] == [_id(1)]


def test_a_folder_that_does_not_exist_has_no_backups(tmp_path):
    assert list_backups(tmp_path / "nothing-here") == []


def test_a_manifest_that_is_not_for_this_version_is_incomplete_not_a_crash(tmp_path):
    _fake_backup(tmp_path, _id(1))
    manifest_path(tmp_path, _id(1)).write_text('{"backup_id": "x"}', encoding="utf-8")

    assert list_backups(tmp_path)[0].status == "incomplete"


# ---- retention, on hand-made files ----


def _ids_left(root: Path) -> list[str]:
    return [b.backup_id for b in list_backups(root)]


def test_retention_keeps_the_newest_few_and_anything_recent_and_removes_the_rest(tmp_path):
    for days in range(10):
        _fake_backup(tmp_path, _id(days))

    removed = prune_backups(tmp_path, keep_count=3, retention_days=2, now=NOW)

    assert _ids_left(tmp_path) == [_id(0), _id(1), _id(2)]
    assert sorted(removed) == sorted(_id(d) for d in range(3, 10))
    assert not dump_path(tmp_path, _id(5)).exists()
    assert not manifest_path(tmp_path, _id(5)).exists()


def test_a_backup_younger_than_the_retention_age_is_kept_even_beyond_the_count(tmp_path):
    for days in range(6):
        _fake_backup(tmp_path, _id(days))

    prune_backups(tmp_path, keep_count=2, retention_days=4, now=NOW)

    assert _ids_left(tmp_path) == [_id(d) for d in range(5)]  # the sixth is over 4 days old


def test_the_count_is_a_minimum_even_for_old_backups(tmp_path):
    for days in (30, 40, 50, 60):
        _fake_backup(tmp_path, _id(days))

    prune_backups(tmp_path, keep_count=2, retention_days=1, now=NOW)

    assert _ids_left(tmp_path) == [_id(30), _id(40)]


def test_the_newest_complete_backup_is_never_removed_whatever_the_settings(tmp_path):
    _fake_backup(tmp_path, _id(100))

    removed = prune_backups(tmp_path, keep_count=0, retention_days=0, now=NOW)

    assert removed == []
    assert _ids_left(tmp_path) == [_id(100)]


def test_an_unfinished_backup_does_not_count_as_the_newest_one_to_keep(tmp_path):
    _fake_backup(tmp_path, _id(0, hour=11), manifest=False)  # newer, but never finished
    _fake_backup(tmp_path, _id(40))

    prune_backups(tmp_path, keep_count=0, retention_days=0, now=NOW)

    assert dump_path(tmp_path, _id(40)).exists()  # the real one survives


def test_an_unfinished_backup_is_left_alone_until_it_is_a_day_old(tmp_path):
    _fake_backup(tmp_path, _id(0, hour=11), manifest=False)  # an hour old: maybe still running
    _fake_backup(tmp_path, _id(3), manifest=False)  # days old: it died
    _fake_backup(tmp_path, _id(0))

    removed = prune_backups(tmp_path, keep_count=7, retention_days=14, now=NOW)

    assert removed == [_id(3)]
    assert dump_path(tmp_path, _id(0, hour=11)).exists()


def test_a_stale_partial_file_is_removed_and_a_fresh_one_is_not(tmp_path):
    _fake_backup(tmp_path, _id(0))
    stale = tmp_path / f"postgres-{_id(3)}.dump.partial"
    fresh = tmp_path / f"postgres-{_id(0, hour=11)}.dump.partial"
    for path, age_hours in ((stale, 72), (fresh, 1)):
        path.write_bytes(b"x")
        stamp = (NOW - timedelta(hours=age_hours)).timestamp()
        os.utime(path, (stamp, stamp))

    prune_backups(tmp_path, keep_count=7, retention_days=14, now=NOW)

    assert not stale.exists() and fresh.exists()


def test_pruning_never_touches_files_that_are_not_ours(tmp_path):
    _fake_backup(tmp_path, _id(0))
    _fake_backup(tmp_path, _id(90))
    other = tmp_path / "keep-me.txt"
    other.write_bytes(b"x")

    prune_backups(tmp_path, keep_count=1, retention_days=1, now=NOW)

    assert other.exists()


def test_pruning_a_folder_that_does_not_exist_is_nothing(tmp_path):
    assert prune_backups(tmp_path / "nope", keep_count=3, retention_days=3, now=NOW) == []


# ---- running the tools ----


def test_the_connection_comes_from_the_database_url_and_the_password_stays_out_of_the_arguments():
    env = pg_backup._connection_env("postgresql+asyncpg://alice:s3cr3t@db.internal:5544/tradingos")

    assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGDATABASE"]) == (
        "db.internal",
        "5544",
        "alice",
        "tradingos",
    )
    assert env["PGPASSWORD"] == "s3cr3t"


def test_a_url_without_a_port_or_password_sets_only_what_it_has(monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "require")  # someone else's setting must not leak in
    monkeypatch.setenv("PGPASSWORD", "from-the-environment")

    env = pg_backup._connection_env("postgresql+asyncpg://bob@localhost/tradingos")

    assert "PGPORT" not in env and "PGPASSWORD" not in env and "PGSSLMODE" not in env
    assert (env["PGHOST"], env["PGUSER"]) == ("localhost", "bob")


async def test_a_tool_that_is_not_installed_is_said_plainly():
    with pytest.raises(PostgresBackupError, match="pg_dump_that_is_not_here is not installed"):
        await pg_backup._run(["pg_dump_that_is_not_here"], env={}, timeout=5, what="backup")


async def test_a_tool_that_fails_reports_what_it_printed():
    with pytest.raises(PostgresBackupError) as raised:
        await pg_backup._run(
            ["python", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
            env=dict(os.environ),
            timeout=30,
            what="backup",
        )

    assert str(raised.value) == "backup: python exited with 3: boom"


async def test_a_tool_that_does_not_finish_is_stopped():
    with pytest.raises(PostgresBackupError, match=r"python did not finish in 1s"):
        await pg_backup._run(
            ["python", "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            timeout=1,
            what="backup",
        )


def test_the_listing_of_a_dump_is_counted_without_its_header_comments():
    listing = (
        "; Archive created at 2026\n; dbname: x\n\n1; 1259 16384 TABLE public users x\n2; 0 0 ...\n"
    )

    entries, digest = pg_backup._toc_summary(listing)

    assert entries == 2
    assert len(digest) == 64


# ---- the real thing ----


async def _populate(db_session_factory) -> None:
    async with db_session_factory() as db:
        for n in range(3):
            await write_audit_entry(
                db, actor="tester", action=f"test.action.{n}", entity_type="x", entity_id=str(n)
            )
        db.add(User(email="who@example.com", hashed_password="x", role="ReadOnlyAuditor"))
        await db.commit()


def _database_url() -> str:
    return get_settings().database_url


@needs_pg_tools
async def test_a_backup_is_a_checked_dump_and_a_manifest_that_describes_it(
    db_session_factory, tmp_path
):
    await _populate(db_session_factory)

    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )

    dump = dump_path(tmp_path, manifest.backup_id)
    assert dump.exists() and manifest_path(tmp_path, manifest.backup_id).exists()
    assert manifest.dump_file == dump.name
    assert manifest.size_bytes == dump.stat().st_size > 0
    assert manifest.sha256 == pg_backup._sha256_of(dump)
    assert manifest.audit_tip_sequence == 3
    assert len(manifest.audit_tip_hash or "") == 64
    assert manifest.tables["users"] == 1 and manifest.tables["audit_log"] == 3
    assert set(manifest.tables) == {t.name for t in Base.metadata.sorted_tables}
    assert manifest.toc_entries > 20
    assert manifest.pg_dump_version.startswith("pg_dump (PostgreSQL)")
    assert manifest.server_version.startswith("16")
    assert manifest.alembic_revision is None  # the test schema is built without migrations
    assert not list(tmp_path.glob("*.partial"))
    assert load_manifest(tmp_path, manifest.backup_id) == manifest


@needs_pg_tools
async def test_the_files_can_be_read_by_their_owner_only(db_session_factory, tmp_path):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )

    for path in (
        dump_path(tmp_path, manifest.backup_id),
        manifest_path(tmp_path, manifest.backup_id),
    ):
        assert path.stat().st_mode & 0o077 == 0, f"{path.name} is readable by others"


@needs_pg_tools
async def test_an_empty_audit_log_has_no_tip(db_session_factory, tmp_path):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )

    assert (manifest.audit_tip_sequence, manifest.audit_tip_hash) == (None, None)


@needs_pg_tools
async def test_the_manifest_and_the_dump_are_the_same_moment(db_session_factory, tmp_path):
    """Rows added while the dump runs must not be in the dump but missing from the manifest (or the
    other way round): both come from one exported snapshot."""
    await _populate(db_session_factory)
    real_run = pg_backup._run

    async def writes_while_dumping(args, **kwargs):
        if args[0] == "pg_dump" and "--version" not in args:  # the dump itself, after the snapshot
            async with db_session_factory() as db:
                await write_audit_entry(db, actor="late", action="test.late")
                await db.commit()
        return await real_run(args, **kwargs)

    pg_backup._run = writes_while_dumping  # type: ignore[assignment]
    try:
        manifest = await create_postgres_backup(
            db_session_factory, tmp_path, database_url=_database_url()
        )
    finally:
        pg_backup._run = real_run  # type: ignore[assignment]

    assert manifest.audit_tip_sequence == 3  # the late entry (a fourth) is in neither
    assert manifest.tables["audit_log"] == 3
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    try:
        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)
        result = await verify_restored_database(tmp_path, manifest.backup_id, database_url=target)
    finally:
        await _drop(scratch)
    assert result.ok, result.problems


async def _drop(name: str) -> None:
    url = make_url(_database_url()).set(database="postgres")
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@needs_pg_tools
async def test_a_verified_backup_passes_and_a_changed_one_does_not(db_session_factory, tmp_path):
    await _populate(db_session_factory)
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    assert (await verify_backup(tmp_path, manifest.backup_id, database_url=_database_url())).ok

    dump = dump_path(tmp_path, manifest.backup_id)
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF  # one flipped byte
    dump.write_bytes(bytes(data))
    flipped = await verify_backup(tmp_path, manifest.backup_id, database_url=_database_url())

    assert flipped.ok is False
    assert any("checksum mismatch" in p for p in flipped.problems)

    dump.write_bytes(bytes(data[: len(data) // 2]))  # and a truncated copy
    cut = await verify_backup(tmp_path, manifest.backup_id, database_url=_database_url())
    assert cut.ok is False
    assert any(p.startswith("size ") for p in cut.problems)
    assert any("the dump cannot be read" in p for p in cut.problems)


@needs_pg_tools
async def test_verification_names_what_is_missing_or_unreadable(db_session_factory, tmp_path):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    url = _database_url()

    manifest_path(tmp_path, manifest.backup_id).write_text("{", encoding="utf-8")
    broken = await verify_backup(tmp_path, manifest.backup_id, database_url=url)
    assert broken.ok is False and "cannot be read" in broken.problems[0]

    assert (await verify_backup(tmp_path, _id(1), database_url=url)).ok is False

    _fake_backup(tmp_path, _id(2), dump=False)
    gone = await verify_backup(tmp_path, _id(2), database_url=url)
    assert gone.problems == (f"postgres-{_id(2)}.dump is missing",)


@needs_pg_tools
async def test_contents_that_differ_from_the_manifest_are_caught(db_session_factory, tmp_path):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    edited = json.loads(manifest_path(tmp_path, manifest.backup_id).read_text(encoding="utf-8"))
    edited["toc_entries"] += 1
    manifest_path(tmp_path, manifest.backup_id).write_text(json.dumps(edited), encoding="utf-8")

    result = await verify_backup(tmp_path, manifest.backup_id, database_url=_database_url())

    assert result.ok is False
    assert any("its contents differ from the manifest's" in p for p in result.problems)


@needs_pg_tools
async def test_a_dump_that_fails_leaves_nothing_that_looks_like_a_backup(
    db_session_factory, tmp_path, monkeypatch
):
    real_run = pg_backup._run

    async def fails(args, **kwargs):
        if args[0] == "pg_dump" and "--version" not in args:
            raise PostgresBackupError("backup: pg_dump exited with 1: disk full")
        return await real_run(args, **kwargs)

    monkeypatch.setattr(pg_backup, "_run", fails)

    with pytest.raises(PostgresBackupError, match="disk full"):
        await create_postgres_backup(db_session_factory, tmp_path, database_url=_database_url())

    assert list(tmp_path.iterdir()) == []


@needs_pg_tools
async def test_a_second_backup_in_the_same_second_is_refused_not_overwritten(
    db_session_factory, tmp_path
):
    moment = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
    await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url(), now=moment
    )

    with pytest.raises(PostgresBackupError, match="already exists"):
        await create_postgres_backup(
            db_session_factory, tmp_path, database_url=_database_url(), now=moment
        )


@needs_pg_tools
async def test_a_backup_restores_into_a_scratch_database_and_checks_out(
    db_session_factory, tmp_path
):
    await _populate(db_session_factory)
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    try:
        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)
        result = await verify_restored_database(tmp_path, manifest.backup_id, database_url=target)

        engine = create_async_engine(target)
        try:
            async with async_sessionmaker(engine)() as db:
                emails = (await db.execute(text("SELECT email FROM users"))).scalars().all()
        finally:
            await engine.dispose()
    finally:
        await _drop(scratch)

    assert result.ok, result.problems
    assert result.audit_entries_checked == 3
    assert result.tables_compared == len(manifest.tables)
    assert emails == ["who@example.com"]


@needs_pg_tools
async def test_restoring_over_an_existing_database_replaces_it(db_session_factory, tmp_path):
    await _populate(db_session_factory)
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    try:
        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)
        engine = create_async_engine(target)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM users"))
        finally:
            await engine.dispose()

        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)  # again
        result = await verify_restored_database(tmp_path, manifest.backup_id, database_url=target)
    finally:
        await _drop(scratch)

    assert result.ok, result.problems  # the deleted user is back


@needs_pg_tools
async def test_a_restored_database_that_differs_from_the_backup_is_reported(
    db_session_factory, tmp_path
):
    await _populate(db_session_factory)
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    try:
        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)
        engine = create_async_engine(target)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM users"))
                # The append-only trigger is what stops this in the live database; a restore that
                # lost it, or a chain edited behind its back, must be seen.
                await conn.execute(
                    text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
                )
                await conn.execute(text("UPDATE audit_log SET actor = 'forged' WHERE sequence = 2"))
                await conn.execute(text("DROP TRIGGER audit_log_append_only ON audit_log"))
        finally:
            await engine.dispose()
        result = await verify_restored_database(tmp_path, manifest.backup_id, database_url=target)
    finally:
        await _drop(scratch)

    assert result.ok is False
    assert "users: 0 rows, the backup recorded 1" in result.problems
    assert any(p.startswith("the audit chain does not verify") for p in result.problems)
    assert "the audit log's append-only trigger is missing" in result.problems


@needs_pg_tools
async def test_a_damaged_backup_is_refused_before_anything_is_dropped(db_session_factory, tmp_path):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    admin = create_async_engine(
        make_url(_database_url()).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        precious = create_async_engine(target)
        async with precious.begin() as conn:
            await conn.execute(text("CREATE TABLE precious (n int)"))
            await conn.execute(text("INSERT INTO precious VALUES (42)"))
        dump = dump_path(tmp_path, manifest.backup_id)
        dump.write_bytes(dump.read_bytes()[:-50])

        with pytest.raises(PostgresBackupError, match="is damaged and was not restored"):
            await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)

        async with precious.connect() as conn:
            assert (await conn.execute(text("SELECT n FROM precious"))).scalar_one() == 42
        await precious.dispose()
    finally:
        await admin.dispose()
        await _drop(scratch)


@needs_pg_tools
@pytest.mark.parametrize("name", ["bad-name", "x; DROP DATABASE postgres", "", "a" * 64, 'q"uote'])
async def test_a_database_name_that_is_not_plain_is_not_touched(db_session_factory, tmp_path, name):
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    url = make_url(_database_url()).set(database=name)

    with pytest.raises(PostgresBackupError, match="is not a database name this restore will touch"):
        await restore_postgres_backup(
            tmp_path, manifest.backup_id, target_url=url.render_as_string(hide_password=False)
        )


async def test_the_default_retention_is_a_week_of_backups_and_two_weeks_of_age():
    settings = get_settings()

    assert (settings.postgres_backup_keep_count, settings.postgres_backup_retention_days) == (7, 14)
    assert (settings.postgres_backup_hour_ist, settings.postgres_backup_minute_ist) == (22, 45)
    assert settings.backup_stale_after_hours == 36.0
    assert settings.postgres_backup_path == "backups"
    assert asyncio.iscoroutinefunction(create_postgres_backup)


@needs_pg_tools
async def test_the_schema_revision_is_recorded_when_the_database_is_migrated(
    db_session_factory, tmp_path
):
    async with db_session_factory() as db:
        await db.execute(text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)"))
        await db.execute(text("INSERT INTO alembic_version VALUES ('b4f7d2a8c1e3')"))
        await db.commit()
    try:
        manifest = await create_postgres_backup(
            db_session_factory, tmp_path, database_url=_database_url()
        )
    finally:
        async with db_session_factory() as db:
            await db.execute(text("DROP TABLE alembic_version"))
            await db.commit()

    assert manifest.alembic_revision == "b4f7d2a8c1e3"
    assert "alembic_version" not in manifest.tables  # the models' tables only


def test_a_file_system_without_modes_leaves_the_file_as_it_is_and_says_so(tmp_path, monkeypatch):
    target = tmp_path / "dump"
    target.write_bytes(b"x")
    said: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            said.append((event, fields))

    def refuse(path, mode):
        raise OSError("not supported here")

    monkeypatch.setattr(pg_backup.os, "chmod", refuse)
    monkeypatch.setattr(pg_backup, "logger", _Log())

    pg_backup._owner_only(target)  # does not raise

    assert said == [("pg_backup.could_not_restrict_file", {"path": str(target)})]


@needs_pg_tools
async def test_a_restored_database_whose_audit_log_moved_on_is_reported(
    db_session_factory, tmp_path
):
    await _populate(db_session_factory)
    manifest = await create_postgres_backup(
        db_session_factory, tmp_path, database_url=_database_url()
    )
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    target = make_url(_database_url()).set(database=scratch).render_as_string(hide_password=False)
    try:
        await restore_postgres_backup(tmp_path, manifest.backup_id, target_url=target)
        engine = create_async_engine(target)
        try:
            async with async_sessionmaker(engine)() as db:
                # a valid, chained entry: the chain verifies, but the tip is no longer the backup's
                await write_audit_entry(db, actor="later", action="test.after_the_backup")
                await db.commit()
        finally:
            await engine.dispose()
        result = await verify_restored_database(tmp_path, manifest.backup_id, database_url=target)
    finally:
        await _drop(scratch)

    assert result.ok is False
    assert "the audit log's tip is not the one the backup recorded" in result.problems
    assert "audit_log: 4 rows, the backup recorded 3" in result.problems
