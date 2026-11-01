"""GET /system/backups and POST /system/postgres-backup: what Settings shows an administrator about
the backups of the database and of the data lake, and the "Back up now" button.
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.core.config import get_settings
from src.core.roles import Role
from src.data.pg_backup import (
    PostgresBackupError,
    PostgresBackupManifest,
    dump_path,
    manifest_path,
)
from src.orchestration import backup_scheduler
from src.orchestration.backup_scheduler import BackupAlreadyRunningError

PASSWORD = "supersecret1"


async def _login(client, email: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def folders(tmp_path, monkeypatch):
    settings = get_settings()
    postgres, lake = tmp_path / "pg", tmp_path / "lake"
    monkeypatch.setattr(settings, "postgres_backup_path", str(postgres))
    monkeypatch.setattr(settings, "data_lake_backup_path", str(lake))
    monkeypatch.setattr(settings, "backup_stale_after_hours", 36.0)
    return postgres, lake


@pytest.fixture
async def admin(client, make_user):
    await make_user("admin@example.com", PASSWORD, Role.SYSTEM_ADMINISTRATOR)
    return await _login(client, "admin@example.com")


def _stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y%m%dT%H%M%SZ")


def _pg_backup(root: Path, hours_ago: float, *, finished: bool = True) -> str:
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
            alembic_revision="b4f7d2a8c1e3",
            audit_tip_sequence=41,
            audit_tip_hash="cd" * 32,
            tables={"users": 2, "audit_log": 41},
            toc_entries=40,
            toc_sha256="ef" * 32,
            pg_dump_version="pg_dump (PostgreSQL) 16.3",
            server_version="16.3",
        )
        manifest_path(root, backup_id).write_text(json.dumps(asdict(manifest)), encoding="utf-8")
    return backup_id


def _lake_backup(root: Path, hours_ago: float, *, manifest: bool = True) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    backup_id = moment.strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / backup_id
    directory.mkdir(parents=True)
    if manifest:
        files = [
            {
                "relative_path": "a.parquet",
                "sha256": "11" * 32,
                "row_count": 100,
                "size_bytes": 900,
            },
            {"relative_path": "b.parquet", "sha256": "22" * 32, "row_count": 50, "size_bytes": 400},
        ]
        (directory / "manifest.json").write_text(
            json.dumps({"backup_id": backup_id, "created_at": "x", "files": files}),
            encoding="utf-8",
        )
    return backup_id


# ---- GET /system/backups ----


async def test_nothing_backed_up_yet_is_stale_with_empty_lists(client, admin, folders):
    body = (await client.get("/api/v1/system/backups", headers=admin)).json()

    for kind in ("postgres", "lake"):
        assert body[kind]["backups"] == []
        assert body[kind]["newest_ok_age_seconds"] is None
        assert body[kind]["stale"] is True
    assert body["postgres"]["kind"] == "postgres" and body["lake"]["kind"] == "lake"
    assert body["postgres"]["directory"] == str(folders[0])
    assert (body["stale_after_hours"], body["keep_count"], body["retention_days"]) == (36.0, 7, 14)
    assert body["schedule"] == (
        "Postgres every day at 22:45 IST; the data lake every day at 22:15 IST"
    )


async def test_the_listing_describes_each_backup_from_its_manifest(client, admin, folders):
    newest = _pg_backup(folders[0], hours_ago=3)
    _pg_backup(folders[0], hours_ago=27)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()["postgres"]

    assert [b["backup_id"] for b in body["backups"]][0] == newest
    first = body["backups"][0]
    assert first["status"] == "ok" and first["note"] is None
    assert first["size_bytes"] == len(b"not a real dump")
    assert first["sha256"] == "ab" * 32
    assert (first["alembic_revision"], first["audit_tip_sequence"]) == ("b4f7d2a8c1e3", 41)
    assert first["audit_tip_hash"] == "cd" * 32
    assert (first["table_count"], first["total_rows"]) == (2, 43)
    assert first["files"] is None  # a lake-only field
    assert 3 * 3600 - 5 < first["age_seconds"] < 3 * 3600 + 120
    assert body["newest_ok_age_seconds"] == pytest.approx(first["age_seconds"], abs=1)
    assert body["stale"] is False


async def test_a_backup_older_than_the_limit_makes_the_kind_stale(client, admin, folders):
    _pg_backup(folders[0], hours_ago=40)
    _lake_backup(folders[1], hours_ago=2)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()

    assert body["postgres"]["stale"] is True
    assert body["lake"]["stale"] is False  # each kind is judged on its own


async def test_an_unfinished_dump_is_listed_but_does_not_count_as_a_backup(client, admin, folders):
    _pg_backup(folders[0], hours_ago=1, finished=False)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()["postgres"]

    assert [b["status"] for b in body["backups"]] == ["incomplete"]
    assert body["backups"][0]["note"] == "the dump has no manifest: it did not finish"
    assert body["newest_ok_age_seconds"] is None and body["stale"] is True


async def test_a_good_backup_behind_an_unfinished_one_still_counts(client, admin, folders):
    _pg_backup(folders[0], hours_ago=1, finished=False)
    _pg_backup(folders[0], hours_ago=5)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()["postgres"]

    assert [b["status"] for b in body["backups"]] == ["incomplete", "ok"]
    assert body["newest_ok_age_seconds"] == pytest.approx(5 * 3600, abs=120)
    assert body["stale"] is False


async def test_lake_backups_are_listed_with_their_files_and_rows(client, admin, folders):
    _lake_backup(folders[1], hours_ago=4)
    _lake_backup(folders[1], hours_ago=30, manifest=False)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()["lake"]

    ok, broken = body["backups"]
    assert (ok["status"], ok["files"], ok["total_rows"], ok["size_bytes"]) == ("ok", 2, 150, 1300)
    assert ok["sha256"] and len(ok["sha256"]) == 64
    assert ok["table_count"] is None and ok["alembic_revision"] is None  # Postgres-only fields
    assert (broken["status"], broken["note"]) == ("incomplete", "the manifest is missing")


async def test_only_the_newest_ten_are_listed(client, admin, folders):
    for n in range(12):
        _pg_backup(folders[0], hours_ago=1 + n * 2)

    body = (await client.get("/api/v1/system/backups", headers=admin)).json()["postgres"]

    assert len(body["backups"]) == 10
    ids = [b["backup_id"] for b in body["backups"]]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.parametrize("path", ["/system/backups"])
async def test_listing_is_for_administrators_only(client, make_user, folders, path):
    await make_user("pm@example.com", PASSWORD, Role.PORTFOLIO_MANAGER)
    await make_user("aud@example.com", PASSWORD, Role.READ_ONLY_AUDITOR)

    for email in ("pm@example.com", "aud@example.com"):
        headers = await _login(client, email)
        assert (await client.get(f"/api/v1{path}", headers=headers)).status_code == 403
    assert (await client.get(f"/api/v1{path}")).status_code == 401


# ---- POST /system/postgres-backup ----


def _fake_backup_run(monkeypatch, *, fail_with: Exception | None = None):
    async def fake_create(session_factory, root, *, database_url, timeout_seconds):
        if fail_with is not None:
            raise fail_with
        backup_id = _pg_backup(root, 0.001)
        return PostgresBackupManifest(
            backup_id=backup_id,
            created_at="x",
            dump_file=f"postgres-{backup_id}.dump",
            size_bytes=15,
            sha256="ab" * 32,
            alembic_revision="b4f7d2a8c1e3",
            audit_tip_sequence=41,
            audit_tip_hash="cd" * 32,
            tables={"users": 2, "audit_log": 41},
            toc_entries=40,
            toc_sha256="ef" * 32,
            pg_dump_version="x",
            server_version="16",
        )

    monkeypatch.setattr(backup_scheduler, "create_postgres_backup", fake_create)


async def test_back_up_now_makes_a_backup_and_returns_it_with_what_was_pruned(
    client, admin, folders, monkeypatch
):
    monkeypatch.setattr(get_settings(), "postgres_backup_keep_count", 1)
    monkeypatch.setattr(get_settings(), "postgres_backup_retention_days", 1)
    old = _pg_backup(folders[0], hours_ago=24 * 5)
    _fake_backup_run(monkeypatch)

    response = await client.post("/api/v1/system/postgres-backup", headers=admin)

    assert response.status_code == 200
    body = response.json()
    assert body["backup"]["status"] == "ok"
    assert body["backup"]["sha256"] == "ab" * 32
    assert body["backup"]["audit_tip_sequence"] == 41
    assert body["pruned"] == [old]
    assert not dump_path(folders[0], old).exists()


async def test_back_up_now_is_refused_while_a_backup_is_running(
    client, admin, folders, monkeypatch
):
    async def busy(*args, **kwargs):
        raise BackupAlreadyRunningError("a backup is already running")

    monkeypatch.setattr(backup_scheduler, "create_postgres_backup", busy)

    response = await client.post("/api/v1/system/postgres-backup", headers=admin)

    assert (response.status_code, response.json()["detail"]) == (409, "a backup is already running")


async def test_a_backup_that_fails_is_a_bad_gateway_with_the_reason(
    client, admin, folders, monkeypatch
):
    _fake_backup_run(monkeypatch, fail_with=PostgresBackupError("backup: pg_dump exited with 1"))

    response = await client.post("/api/v1/system/postgres-backup", headers=admin)

    assert response.status_code == 502
    assert response.json()["detail"] == "backup: pg_dump exited with 1"


async def test_back_up_now_is_for_administrators_only(client, make_user, folders, monkeypatch):
    await make_user("pm@example.com", PASSWORD, Role.PORTFOLIO_MANAGER)
    _fake_backup_run(monkeypatch)
    headers = await _login(client, "pm@example.com")

    assert (await client.post("/api/v1/system/postgres-backup", headers=headers)).status_code == 403
    assert (await client.post("/api/v1/system/postgres-backup")).status_code == 401
    assert not folders[0].exists()  # nothing was made
