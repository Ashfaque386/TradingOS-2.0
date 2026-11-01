"""Data the app writes must live on a volume, not in the container's own filesystem.

`docker compose down && docker compose up --build` is the documented way to apply an update, and it
deletes the container's filesystem. The data lake, its nightly backups, the audit archive and the
Postgres backups are written under the working directory (`/app/...`, see `src/core/config.py`), so
each needs a volume or it is lost on every update, the backups along with the data they protect.

The paths are read from the settings, not typed here, so changing a default without moving its
volume fails this test.
"""

from pathlib import Path

import yaml

from src.core.config import Settings

COMPOSE_PATH = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
APP_DIR = Path("/app")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _mounts_of(service: str) -> dict[str, str]:
    """container path -> the named volume or host path mounted there."""
    mounts: dict[str, str] = {}
    for entry in _compose()["services"][service]["volumes"]:
        source, target = str(entry).split(":")[:2]
        mounts[target] = source
    return mounts


def _container_path(setting: str) -> str:
    relative = Settings.model_fields[setting].default
    return (APP_DIR / Path(relative)).as_posix()


def test_the_data_lake_the_backups_and_the_audit_archive_are_on_named_volumes():
    mounts = _mounts_of("backend")

    for setting in (
        "data_lake_path",
        "data_lake_backup_path",
        "audit_archive_path",
        "postgres_backup_path",
    ):
        target = _container_path(setting)
        assert target in mounts, f"{setting} ({target}) is not on any volume"
        source = mounts[target]
        assert not source.startswith((".", "/")), f"{target} is a host path, not a named volume"


def test_the_encrypted_secret_stores_stay_on_a_named_volume():
    secrets_dir = Path(_container_path("secrets_store_path")).parent.as_posix()

    assert secrets_dir in _mounts_of("backend")


def test_the_database_backups_are_not_on_the_volume_of_the_database_they_protect():
    mounts = _mounts_of("backend")

    assert mounts[_container_path("postgres_backup_path")] != mounts["/var/lib/postgresql/data"]


def test_every_named_volume_the_backend_uses_is_declared():
    declared = set(_compose()["volumes"])
    named = {
        source for source in _mounts_of("backend").values() if not source.startswith((".", "/"))
    }

    assert named <= declared, f"used but not declared: {sorted(named - declared)}"


def test_no_two_paths_share_a_volume():
    named = [
        source for source in _mounts_of("backend").values() if not source.startswith((".", "/"))
    ]

    assert len(named) == len(set(named))
