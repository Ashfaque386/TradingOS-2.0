"""The lake records where each daily bar came from, and only real bars may drive a signal.

The lake used to be filled by a generated-data provider, so a row could not say whether it rested on
the market. Rows now carry a `source`; one without it is "not real" (nobody can say what it rests
on), `real_only` reads leave it out, a symbol's history is pinned to the broker it started with, and
real history is written over a symbol only after its generated rows are removed.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.data import lake


def _bars(days: list[str], *, close: float = 100.0, source: str | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": [close + i for i in range(len(days))],
            "volume": 1000,
        },
        index=pd.DatetimeIndex(pd.to_datetime(days)),
    )
    if source is not None:
        frame["source"] = source
    return frame


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def _all(root: Path, symbol: str, *, real_only: bool):
    return lake.read_daily_bars(
        root, symbol, date(2026, 1, 1), date(2026, 12, 31), real_only=real_only
    )


# ---- tagging and real_only reads ----


def test_a_tagged_bar_is_stored_with_its_source_and_read_back_as_real(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01", "2026-09-02"], source="zerodha"))

    assert len(_all(root, "INFY", real_only=True)) == 2
    assert lake.pinned_source(root, "INFY") == "zerodha"


@pytest.mark.parametrize("source", [None, "synthetic", ""])
def test_a_bar_with_no_real_source_is_invisible_to_a_real_only_read(root, source):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], source=source))

    assert len(_all(root, "INFY", real_only=False)) == 1  # still stored, still readable
    assert _all(root, "INFY", real_only=True).empty  # but never fed to a signal
    assert lake.pinned_source(root, "INFY") is None


def test_the_default_read_still_returns_every_row_so_existing_callers_are_unchanged(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"]))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-02"], source="zerodha"))

    assert len(_all(root, "INFY", real_only=False)) == 2
    assert len(_all(root, "INFY", real_only=True)) == 1


def test_real_rows_added_beside_old_untagged_ones_in_one_partition_keep_their_own_status(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01", "2026-09-02"]))  # the old lake
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-03"], source="upstox"))

    real = _all(root, "INFY", real_only=True)
    assert [d.date().isoformat() for d in real.index] == ["2026-09-03"]
    assert len(_all(root, "INFY", real_only=False)) == 3


def test_a_real_bar_for_a_day_replaces_the_generated_one_for_that_day(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], close=50.0, source="synthetic"))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], close=1500.0, source="zerodha"))

    row = _all(root, "INFY", real_only=True)
    assert len(row) == 1 and float(row["close"].iloc[0]) == 1500.0


def test_the_extra_column_never_leaks_into_what_callers_get(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], source="zerodha"))

    assert list(_all(root, "INFY", real_only=True).columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]


# ---- the source pin ----


def test_a_symbol_with_no_stored_history_is_not_pinned(root):
    assert lake.pinned_source(root, "NEVERSEEN") is None


def test_the_pin_is_the_source_of_the_newest_real_bar_across_months(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-07-15"], source="upstox"))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-15"], source="zerodha"))
    lake.write_daily_bars(root, "INFY", _bars(["2026-10-01"], source="synthetic"))  # not real

    assert lake.pinned_source(root, "INFY") == "zerodha"


def test_pins_are_per_symbol(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], source="zerodha"))
    lake.write_daily_bars(root, "TCS", _bars(["2026-09-01"], source="upstox"))

    assert lake.pinned_source(root, "INFY") == "zerodha"
    assert lake.pinned_source(root, "TCS") == "upstox"


# ---- purging the unreal rows ----


def test_purge_removes_generated_and_untagged_rows_and_keeps_real_ones(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01", "2026-09-02"]))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-03"], source="synthetic"))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-04"], source="zerodha"))

    removed = lake.purge_unreal_daily_rows(root, "INFY")

    assert removed == 3
    assert [d.date().isoformat() for d in _all(root, "INFY", real_only=False).index] == [
        "2026-09-04"
    ]


def test_purge_deletes_a_partition_that_held_only_unreal_rows(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-08-10"]))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-04"], source="zerodha"))
    august = lake._partition_path(root, lake.DAILY_SUBDIR, "INFY", 2026, 8)
    assert august.exists()

    lake.purge_unreal_daily_rows(root, "INFY")

    assert not august.exists()
    assert lake._partition_path(root, lake.DAILY_SUBDIR, "INFY", 2026, 9).exists()


def test_purge_touches_only_the_named_symbol(root):
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"]))
    lake.write_daily_bars(root, "TCS", _bars(["2026-09-01"]))

    lake.purge_unreal_daily_rows(root, "INFY")

    assert len(_all(root, "TCS", real_only=False)) == 1


def test_purge_with_nothing_stored_or_nothing_unreal_removes_nothing(root):
    assert lake.purge_unreal_daily_rows(root, "NEVERSEEN") == 0
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"], source="zerodha"))
    assert lake.purge_unreal_daily_rows(root, "INFY") == 0
    assert len(_all(root, "INFY", real_only=True)) == 1


def test_a_read_that_does_not_ask_for_real_only_returns_every_stored_row(root):
    """Callers that predate sources (the dashboards, the freshness checks) call read_daily_bars
    with no flag: they must keep seeing every row, so `real_only` has to default to off."""
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-01"]))  # untagged
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-02"], source="synthetic"))
    lake.write_daily_bars(root, "INFY", _bars(["2026-09-03"], source="zerodha"))

    everything = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), date(2026, 12, 31))

    assert len(everything) == 3
