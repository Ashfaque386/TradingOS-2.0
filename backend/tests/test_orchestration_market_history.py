"""Fetching a symbol's real price history: the backfill and the 07:30 IST gap repair.

A new paper subscription had no real history -- the daily signal job then fell back to generated
bars. These pipelines fetch it from the provider, store it tagged with its source, replace the
symbol's generated rows (never blending them in), record a provenance row either way, and never
raise: a failed fetch is an outcome with a reason.
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from src.data import lake
from src.data.providers import FakeMarketDataProvider
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.market_data_provenance import MarketDataProvenance
from src.orchestration import market_data as md

AS_OF = date(2026, 10, 16)  # a Friday


def _frame(start: date, days: int, *, close: float = 100.0, source: str = "zerodha"):
    index = pd.bdate_range(start=start, periods=days)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": [close + i for i in range(days)],
            "volume": 1000,
            "source": source,
        },
        index=index,
    )


class _Provider:
    """An async provider that records what it was asked and can be told what to return."""

    source_name = "broker_market_data"

    def __init__(self, frames=None, *, raises: Exception | None = None):
        self.frames = frames or {}
        self.raises = raises
        self.calls: list[tuple[str, date, date]] = []

    async def daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        if self.raises is not None:
            raise self.raises
        return self.frames.get(symbol, pd.DataFrame())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


async def _provenance(db_session_factory):
    async with db_session_factory() as db:
        rows = (await db.execute(select(MarketDataProvenance))).scalars().all()
    return rows


# ---- backfill ----


async def test_a_backfill_stores_tagged_real_bars_and_records_what_it_did(db_session_factory, root):
    provider = _Provider({"INFY": _frame(date(2025, 9, 1), 80)})

    async with db_session_factory() as db:
        row = await md.run_symbol_history_backfill(
            db, provider=provider, root=root, symbol="INFY", as_of=AS_OF
        )

    assert row.status == "success" and row.pipeline == "history_backfill"
    assert row.source == "broker_market_data" and row.rows_ingested == 80
    assert row.details["source"] == "zerodha" and row.details["rows"] == 80
    assert row.details["first_date"] == "2025-09-01" and row.details["unreal_rows_removed"] == 0
    stored = lake.read_daily_bars(root, "INFY", date(2025, 1, 1), AS_OF, real_only=True)
    assert len(stored) == 80
    assert lake.pinned_source(root, "INFY") == "zerodha"
    async with db_session_factory() as db:  # freshness points at the newest stored day
        fresh = (await db.execute(select(DatasetFreshnessRecord))).scalar_one()
        assert fresh.symbol == "INFY" and fresh.data_date == stored.index.max().date()


async def test_the_backfill_asks_for_about_a_year_ending_on_the_given_day(db_session_factory, root):
    provider = _Provider({"INFY": _frame(date(2025, 9, 1), 5)})

    async with db_session_factory() as db:
        await md.run_symbol_history_backfill(
            db, provider=provider, root=root, symbol="INFY", as_of=AS_OF
        )

    assert provider.calls == [("INFY", AS_OF - timedelta(days=md.BACKFILL_DAYS), AS_OF)]


async def test_a_backfill_replaces_the_symbols_generated_rows_and_keeps_real_ones(
    db_session_factory, root
):
    fake = FakeMarketDataProvider()
    lake.write_daily_bars(
        root,
        "INFY",
        fake.daily_bars("INFY", date(2026, 9, 1), date(2026, 9, 10)).assign(source="synthetic"),
    )
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 8, 3), 3, source="zerodha"))
    provider = _Provider({"INFY": _frame(date(2026, 9, 1), 8, close=1500.0)})

    async with db_session_factory() as db:
        row = await md.run_symbol_history_backfill(
            db, provider=provider, root=root, symbol="INFY", as_of=AS_OF
        )

    assert row.details["unreal_rows_removed"] > 0
    everything = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=False)
    real = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=True)
    assert len(everything) == len(real)  # not one unreal row left in the series
    assert float(real.loc["2026-09-01", "close"]) == 1500.0


async def test_the_replace_can_be_switched_off_for_a_repair(db_session_factory, root):
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 9, 1), 3, source="synthetic"))
    provider = _Provider({"INFY": _frame(date(2026, 10, 12), 3)})

    async with db_session_factory() as db:
        await md.run_symbol_history_backfill(
            db, provider=provider, root=root, symbol="INFY", as_of=AS_OF, replace_unreal=False
        )

    assert len(lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=False)) == 6


async def test_a_provider_failure_is_a_recorded_outcome_not_an_exception(db_session_factory, root):
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 9, 1), 3, source="synthetic"))
    provider = _Provider(raises=RuntimeError("zerodha rejected the stored token"))

    async with db_session_factory() as db:
        row = await md.run_symbol_history_backfill(
            db, provider=provider, root=root, symbol="INFY", as_of=AS_OF
        )

    assert row.status == "failed" and row.symbols_processed == 0 and row.rows_ingested == 0
    assert "RuntimeError: zerodha rejected the stored token" in row.error_message
    # The fetch failed before anything was deleted: the old rows are untouched.
    assert len(lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=False)) == 3


async def test_an_empty_answer_is_a_failure_not_a_success_with_nothing(db_session_factory, root):
    async with db_session_factory() as db:
        row = await md.run_symbol_history_backfill(
            db, provider=_Provider({}), root=root, symbol="INFY", as_of=AS_OF
        )

    assert row.status == "failed" and "returned no daily bars" in row.error_message
    assert lake.pinned_source(root, "INFY") is None


async def test_a_synchronous_generated_provider_still_works_and_is_stored_as_synthetic(
    db_session_factory, root
):
    async with db_session_factory() as db:
        row = await md.run_symbol_history_backfill(
            db, provider=FakeMarketDataProvider(), root=root, symbol="DEMO", as_of=AS_OF, days=30
        )

    assert row.status == "success" and row.source == "fake_market_data_provider"
    stored = lake.read_daily_bars(root, "DEMO", date(2026, 1, 1), AS_OF, real_only=False)
    assert len(stored) > 10
    assert lake.read_daily_bars(root, "DEMO", date(2026, 1, 1), AS_OF, real_only=True).empty
    assert lake.pinned_source(root, "DEMO") is None  # generated data pins nothing


# ---- gap repair ----


async def test_a_symbol_with_too_little_real_history_gets_a_full_backfill(db_session_factory, root):
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 10, 1), 5))  # 5 real rows: not enough
    provider = _Provider({"INFY": _frame(date(2025, 9, 1), 120)})

    async with db_session_factory() as db:
        rows = await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["INFY"], as_of=AS_OF
        )

    assert [r.pipeline for r in rows] == ["history_gap_repair"] and rows[0].status == "success"
    assert provider.calls[0][1] == AS_OF - timedelta(days=md.BACKFILL_DAYS)
    # 120 fetched + the 5 real rows already stored (a backfill keeps real rows).
    assert len(lake.read_daily_bars(root, "INFY", date(2025, 1, 1), AS_OF, real_only=True)) == 125


async def test_a_symbol_with_enough_history_only_gets_the_recent_window_refetched(
    db_session_factory, root
):
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 6, 1), md.MIN_HISTORY_ROWS + 20))
    newest = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=True).index.max()
    fresh = _frame(newest.date() - timedelta(days=2), 3, close=9999.0)
    assert newest in fresh.index  # the window covers the newest stored day
    provider = _Provider({"INFY": fresh})

    async with db_session_factory() as db:
        await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["INFY"], as_of=AS_OF
        )

    _, start, end = provider.calls[0]
    assert end == AS_OF
    assert (AS_OF - start).days >= md.REPAIR_WINDOW_DAYS  # a week at least, not a year
    assert (AS_OF - start).days < 100
    repaired = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=True)
    # The refetched bar replaced the stored one for that day.
    assert float(repaired.loc[newest, "close"]) == float(fresh.loc[newest, "close"]) > 9000


async def test_the_repair_window_widens_to_cover_a_long_gap(db_session_factory, root):
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 3, 2), md.MIN_HISTORY_ROWS + 5))
    newest = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), AS_OF, real_only=True).index.max()
    provider = _Provider({"INFY": _frame(AS_OF - timedelta(days=3), 2)})

    async with db_session_factory() as db:
        await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["INFY"], as_of=AS_OF
        )

    _, start, _ = provider.calls[0]
    assert start <= newest.date()  # reaches back to the last day we have


async def test_one_symbol_failing_does_not_stop_the_others(db_session_factory, root):
    class _Mixed(_Provider):
        async def daily_bars(self, symbol, start, end):
            if symbol == "BAD":
                raise RuntimeError("not listed")
            return await super().daily_bars(symbol, start, end)

    provider = _Mixed({"GOOD": _frame(date(2025, 9, 1), 100)})

    async with db_session_factory() as db:
        rows = await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["BAD", "GOOD"], as_of=AS_OF
        )

    by_symbol = {r.details["symbol"]: r for r in rows}
    assert by_symbol["BAD"].status == "failed" and by_symbol["GOOD"].status == "success"


async def test_every_repair_leaves_a_provenance_row(db_session_factory, root):
    provider = _Provider({"A": _frame(date(2025, 9, 1), 100), "B": _frame(date(2025, 9, 1), 100)})

    async with db_session_factory() as db:
        await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["A", "B"], as_of=AS_OF
        )

    rows = await _provenance(db_session_factory)
    assert sorted(r.details["symbol"] for r in rows) == ["A", "B"]
    assert {r.pipeline for r in rows} == {"history_gap_repair"}


async def test_a_repair_of_a_symbol_with_real_history_leaves_its_other_rows_alone(
    db_session_factory, root
):
    """Replacing a symbol's generated rows belongs to the explicit full backfill. A routine
    repair must never delete anything: a transient provider hiccup is not a reason to."""
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 6, 1), md.MIN_HISTORY_ROWS + 20))
    lake.write_daily_bars(root, "INFY", _frame(date(2026, 1, 5), 3, source="synthetic"))
    provider = _Provider({"INFY": _frame(AS_OF - timedelta(days=4), 3)})

    async with db_session_factory() as db:
        rows = await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["INFY"], as_of=AS_OF
        )

    assert rows[0].details["unreal_rows_removed"] == 0
    kept = lake.read_daily_bars(root, "INFY", date(2026, 1, 1), date(2026, 1, 31), real_only=False)
    assert len(kept) == 3  # the generated rows are still there (and still never read as real)


async def test_the_repair_window_is_a_full_week_when_history_is_current(db_session_factory, root):
    newest = AS_OF - timedelta(days=1)  # Thursday: yesterday's bar is already stored
    history = _frame(date(2026, 1, 5), md.MIN_HISTORY_ROWS + 10)
    history.index = pd.bdate_range(end=newest, periods=len(history))
    lake.write_daily_bars(root, "INFY", history)
    provider = _Provider({"INFY": _frame(newest, 1)})

    async with db_session_factory() as db:
        await md.run_history_gap_repair(
            db, provider=provider, root=root, symbols=["INFY"], as_of=AS_OF
        )

    _, start, end = provider.calls[0]
    assert end == AS_OF
    assert (end - start).days == md.REPAIR_WINDOW_DAYS == 7  # not 3 (the gap + 2): a week
