"""Data lake storage layer tests (Build Spec §14): idempotent partitioned
writes, catalog view refresh (including the empty-lake case), and backup
creation/validation -- including a deliberately corrupted file, per the
phase's explicit test requirement.
"""

import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.data import backup as backup_module
from src.data import lake


def _bars(n: int = 5, start: str = "2026-09-01") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def test_write_daily_bars_twice_does_not_duplicate_rows(lake_root: Path):
    bars = _bars()
    r1 = lake.write_daily_bars(lake_root, "DEMOSTOCK", bars)
    assert r1.rows_added == 5

    r2 = lake.write_daily_bars(lake_root, "DEMOSTOCK", bars)
    assert r2.rows_added == 0

    read_back = lake.read_daily_bars(lake_root, "DEMOSTOCK", date(2026, 9, 1), date(2026, 9, 30))
    assert len(read_back) == 5


def test_write_overlapping_batch_with_corrected_value_replaces_not_duplicates(lake_root: Path):
    bars = _bars()
    lake.write_daily_bars(lake_root, "DEMOSTOCK", bars)

    corrected = bars.copy()
    corrected.iloc[0, corrected.columns.get_loc("close")] = 999.0
    result = lake.write_daily_bars(lake_root, "DEMOSTOCK", corrected)
    assert result.rows_added == 0  # same dates, no new rows -- a correction, not an addition

    read_back = lake.read_daily_bars(lake_root, "DEMOSTOCK", date(2026, 9, 1), date(2026, 9, 30))
    assert len(read_back) == 5
    assert read_back.iloc[0]["close"] == 999.0


def test_has_data_for_reflects_real_written_state(lake_root: Path):
    bars = _bars()
    lake.write_daily_bars(lake_root, "DEMOSTOCK", bars)
    assert lake.has_data_for(lake_root, "DEMOSTOCK", date(2026, 9, 1)) is True
    assert lake.has_data_for(lake_root, "DEMOSTOCK", date(2026, 9, 30)) is False
    assert lake.has_data_for(lake_root, "OTHERSTOCK", date(2026, 9, 1)) is False


def test_refresh_catalog_views_on_empty_lake_does_not_raise(lake_root: Path):
    result = lake.refresh_catalog_views(lake_root)
    assert result.ohlcv_daily_rows == 0
    assert result.ohlcv_intraday_rows == 0


def test_refresh_catalog_views_counts_written_rows(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars())
    result = lake.refresh_catalog_views(lake_root)
    assert result.ohlcv_daily_rows == 5


def test_backup_creation_and_successful_validation(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars())
    backup_root = Path(tempfile.mkdtemp())

    manifest = backup_module.create_backup(lake_root, backup_root)
    assert len(manifest.files) == 1

    validation = backup_module.validate_backup(backup_root / manifest.backup_id)
    assert validation.valid is True
    assert validation.checked_files == 1
    assert validation.problems == ()


def test_backup_validation_catches_deliberately_corrupted_file(lake_root: Path):
    lake.write_daily_bars(lake_root, "DEMOSTOCK", _bars())
    backup_root = Path(tempfile.mkdtemp())
    manifest = backup_module.create_backup(lake_root, backup_root)

    backed_up_file = backup_root / manifest.backup_id / manifest.files[0].relative_path
    with backed_up_file.open("r+b") as f:
        f.seek(0)
        f.write(b"\x00\x00\x00\x00CORRUPTED")

    validation = backup_module.validate_backup(backup_root / manifest.backup_id)
    assert validation.valid is False
    assert validation.checked_files == 1
    assert len(validation.problems) == 1
    assert manifest.files[0].relative_path in validation.problems[0]


def test_backup_validation_reports_missing_manifest(tmp_path: Path):
    result = backup_module.validate_backup(tmp_path / "nonexistent-backup")
    assert result.valid is False
    assert "manifest.json missing" in result.problems[0]
