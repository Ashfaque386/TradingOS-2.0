"""The restore command-line tool (scripts/restore_postgres.py): what it refuses, what it reports,
and one real round trip through it.
"""

import json
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts import restore_postgres
from src.audit.service import write_audit_entry
from src.core.config import get_settings
from src.data import pg_backup
from src.data.pg_backup import PostgresBackupError, PostgresBackupManifest, dump_path, manifest_path
from src.models.user import User

needs_pg_tools = pytest.mark.skipif(
    shutil.which("pg_dump") is None or shutil.which("pg_restore") is None,
    reason="the PostgreSQL client tools are not installed",
)


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")


def _fake(root: Path, hours_ago: float, *, finished: bool = True, revision: str | None = "rev1"):
    backup_id = _stamp(hours_ago)
    root.mkdir(parents=True, exist_ok=True)
    dump_path(root, backup_id).write_bytes(b"not a real dump")
    if finished:
        manifest = PostgresBackupManifest(
            backup_id=backup_id,
            created_at="x",
            dump_file=f"postgres-{backup_id}.dump",
            size_bytes=15,
            sha256="ab" * 32,
            alembic_revision=revision,
            audit_tip_sequence=7,
            audit_tip_hash="cd" * 32,
            tables={"users": 2},
            toc_entries=40,
            toc_sha256="ef" * 32,
            pg_dump_version="x",
            server_version="16",
        )
        manifest_path(root, backup_id).write_text(json.dumps(asdict(manifest)), encoding="utf-8")
    return backup_id


def _scratch_target() -> tuple[str, str]:
    name = f"restore_cli_{uuid.uuid4().hex[:8]}"
    url = make_url(get_settings().database_url).set(database=name)
    return name, url.render_as_string(hide_password=False)


async def _drop(name: str) -> None:
    admin = create_async_engine(
        make_url(get_settings().database_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


# ---- listing and choosing ----


def test_the_listing_shows_each_backup_and_marks_the_unusable_ones(tmp_path, capsys):
    good = _fake(tmp_path, 3)
    broken = _fake(tmp_path, 1, finished=False)

    assert restore_postgres.main(["--root", str(tmp_path), "list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Backups in {tmp_path} (newest first):"
    assert lines[1].startswith(f"  {broken}  NOT USABLE  the dump has no manifest")
    assert lines[2].startswith(f"  {good}  ok  ")
    assert "1 tables, 2 rows" in lines[2]
    assert "audit tip 7" in lines[2] and "schema rev1" in lines[2]


def test_an_empty_folder_is_said_not_failed(tmp_path, capsys):
    assert restore_postgres.main(["--root", str(tmp_path / "none"), "list"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "  none"


def test_latest_is_the_newest_backup_that_finished(tmp_path):
    older = _fake(tmp_path, 30)
    _fake(tmp_path, 1, finished=False)

    assert restore_postgres._resolve(tmp_path, "latest") == older
    assert restore_postgres._resolve(tmp_path, "20260101T000000Z") == "20260101T000000Z"


def test_latest_with_nothing_finished_is_an_error_not_a_guess(tmp_path):
    _fake(tmp_path, 1, finished=False)

    with pytest.raises(PostgresBackupError, match="there is no finished backup in"):
        restore_postgres._resolve(tmp_path, "latest")


def test_a_failure_is_reported_on_stderr_with_a_nonzero_exit(tmp_path, capsys):
    code = restore_postgres.main(["--root", str(tmp_path), "verify", "latest"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Failed: there is no finished backup" in captured.err


# ---- what it refuses before touching anything ----


def test_the_target_must_be_typed_twice(tmp_path, capsys, monkeypatch):
    backup_id = _fake(tmp_path, 1)
    touched = []
    monkeypatch.setattr(
        restore_postgres, "restore_postgres_backup", lambda *a, **k: touched.append(a)
    )

    code = restore_postgres.main(
        [
            "--root",
            str(tmp_path),
            "restore",
            backup_id,
            "--into",
            "some_db",
            "--confirm",
            "other_db",
        ]
    )

    assert code == 2
    assert (
        "--confirm must repeat the target database name exactly ('some_db')"
        in capsys.readouterr().out
    )
    assert touched == []


def test_the_live_database_is_not_replaced_without_saying_so(tmp_path, capsys, monkeypatch):
    backup_id = _fake(tmp_path, 1)
    live = make_url(get_settings().database_url).database
    touched = []
    monkeypatch.setattr(
        restore_postgres, "restore_postgres_backup", lambda *a, **k: touched.append(a)
    )

    code = restore_postgres.main(
        ["--root", str(tmp_path), "restore", backup_id, "--into", live, "--confirm", live]
    )

    out = capsys.readouterr().out
    assert code == 2
    assert f"{live!r} is the database the application is configured to use" in out
    assert "stop the API first, then add --replace-live" in out
    assert touched == []


# ---- bringing an older schema up to date ----


def _head() -> str:
    config = Config(str(restore_postgres.BACKEND_DIR / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head
    return head


def test_a_backup_at_the_codes_revision_needs_no_migration(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("migrated needlessly"))

    message = restore_postgres._migrate("postgresql+asyncpg://x/y", _head())

    assert (
        message == f"The schema is already at the code's revision ({_head()}); nothing to migrate."
    )


def test_an_older_backup_is_migrated_in_the_restored_database_only(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    message = restore_postgres._migrate("postgresql+asyncpg://u:p@h/restored", "old_rev")

    [(args, kwargs)] = calls
    assert args[0] == [sys.executable, "-m", "alembic", "upgrade", "head"]
    assert kwargs["env"]["DATABASE_URL"] == "postgresql+asyncpg://u:p@h/restored"
    assert kwargs["cwd"] == restore_postgres.BACKEND_DIR and kwargs["check"] is True
    assert message == f"Migrated the schema from old_rev to {_head()}."


def test_a_backup_of_a_database_not_built_by_migrations_is_left_alone(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("migrated needlessly"))

    assert "records no schema revision" in restore_postgres._migrate("x", None)


# ---- the real thing ----


@needs_pg_tools
async def test_a_backup_is_made_and_restored_through_the_tool_and_checks_out(
    db_session_factory, tmp_path, capsys
):
    async with db_session_factory() as db:
        await write_audit_entry(db, actor="tester", action="test.action", entity_type="x")
        db.add(User(email="who@example.com", hashed_password="x", role="ReadOnlyAuditor"))
        await db.commit()
    manifest = await pg_backup.create_postgres_backup(
        db_session_factory, tmp_path, database_url=get_settings().database_url
    )
    name, url = _scratch_target()
    try:
        # asyncio.run inside a running loop is not allowed: the tool's own entry point is
        # exercised in a thread, as it would be from a shell.
        code = await _run_main_in_thread(
            ["--root", str(tmp_path), "restore", "latest", "--into", name, "--confirm", name]
        )
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                users = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
        finally:
            await engine.dispose()
    finally:
        await _drop(name)

    out = capsys.readouterr().out
    assert code == 0, out
    assert f"Checking {manifest.backup_id}, then restoring it into {name!r}" in out
    assert "Checked against the manifest:" in out and "1 audit entries chain correctly" in out
    assert "records no schema revision" in out
    assert "SECRETS_ENCRYPTION_KEY" in out
    assert users == 1


@needs_pg_tools
async def test_a_restore_that_does_not_match_the_backup_fails_the_command(
    db_session_factory, tmp_path, capsys
):
    manifest = await pg_backup.create_postgres_backup(
        db_session_factory, tmp_path, database_url=get_settings().database_url
    )
    # The manifest claims a table has rows the dump does not hold.
    path = manifest_path(tmp_path, manifest.backup_id)
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["tables"]["users"] = 5
    path.write_text(json.dumps(edited), encoding="utf-8")
    name, _ = _scratch_target()
    try:
        code = await _run_main_in_thread(
            [
                "--root",
                str(tmp_path),
                "restore",
                manifest.backup_id,
                "--into",
                name,
                "--confirm",
                name,
            ]
        )
    finally:
        await _drop(name)

    out = capsys.readouterr().out
    assert code == 1
    assert "The restored database does NOT match the backup:" in out
    assert "users: 0 rows, the backup recorded 5" in out


async def _run_main_in_thread(argv: list[str]) -> int:
    import asyncio

    return await asyncio.to_thread(restore_postgres.main, argv)
