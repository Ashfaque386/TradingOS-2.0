"""Nightly data lake backup with checksum + row-count validation (Build
Spec §14). `create_backup` copies every Parquet partition file to a
timestamped directory and records a SHA-256 checksum and row count for
each in a `manifest.json`; `validate_backup` recomputes both and flags any
mismatch -- a bit-flipped or truncated file fails on checksum, a file
whose Parquet footer got corrupted fails to even open (caught, not raised)
and is reported the same way, not silently skipped.

`catalog.duckdb` is deliberately NOT backed up: it holds only the two view
definitions, entirely regenerable from the Parquet files themselves via
`src.data.lake.refresh_catalog_views`, so backing it up would mean
validating a derived artifact instead of the source data that actually
matters.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2

from src.data import lake

MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class FileManifestEntry:
    relative_path: str
    sha256: str
    row_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    created_at: str
    files: tuple[FileManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    checked_files: int
    problems: tuple[str, ...]


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(root: Path, backup_root: Path) -> BackupManifest:
    files = lake.all_parquet_files(root)
    backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = backup_root / backup_id

    entries = []
    for source in files:
        relative = source.relative_to(root)
        dest = backup_dir / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy2(source, dest)
        entries.append(
            FileManifestEntry(
                relative_path=str(relative),
                sha256=_sha256_of(dest),
                row_count=lake.row_count(dest),
                size_bytes=dest.stat().st_size,
            )
        )

    manifest = BackupManifest(
        backup_id=backup_id, created_at=datetime.now(UTC).isoformat(), files=tuple(entries)
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / MANIFEST_FILENAME).write_text(
        json.dumps({**asdict(manifest), "files": [asdict(e) for e in manifest.files]}, indent=2),
        encoding="utf-8",
    )
    return manifest


def validate_backup(backup_dir: Path) -> ValidationResult:
    manifest_path = backup_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return ValidationResult(
            valid=False, checked_files=0, problems=(f"{MANIFEST_FILENAME} missing",)
        )

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    checked = 0
    for entry in data["files"]:
        checked += 1
        path = backup_dir / entry["relative_path"]
        if not path.exists():
            problems.append(f"{entry['relative_path']}: file missing")
            continue

        actual_checksum = _sha256_of(path)
        if actual_checksum != entry["sha256"]:
            problems.append(
                f"{entry['relative_path']}: checksum mismatch "
                f"(expected {entry['sha256']}, got {actual_checksum})"
            )
            continue

        try:
            actual_rows = lake.row_count(path)
        except Exception as exc:  # noqa: BLE001 - a corrupted Parquet footer must be reported, not raised
            problems.append(f"{entry['relative_path']}: unreadable ({exc})")
            continue

        if actual_rows != entry["row_count"]:
            problems.append(
                f"{entry['relative_path']}: row-count mismatch "
                f"(expected {entry['row_count']}, got {actual_rows})"
            )

    return ValidationResult(valid=not problems, checked_files=checked, problems=tuple(problems))
