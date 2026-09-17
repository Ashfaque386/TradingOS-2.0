"""Orchestration-level tests for the Build Spec §14 pipelines: ingestion
idempotency (writing the same day twice never duplicates provenance/
freshness state or lake rows), the holiday skip, corporate-actions and
instrument-master upsert idempotency, the Bhavcopy fallback's real-vs-
synthetic source honesty, and -- the phase's other explicit requirement --
that the freshness gate correctly reflects real pipeline state.
"""

from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data import lake
from src.data.freshness_snapshot import load_freshness_snapshot
from src.data.providers import FakeMarketDataProvider
from src.engine.backtest.freshness import check_data_freshness
from src.models.corporate_action import CorporateAction
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.instrument import Instrument
from src.models.market_data_provenance import MarketDataProvenance
from src.orchestration import market_data as md

# A real NSE trading Wednesday -- never a weekend/fixed-holiday in this
# codebase's calendar, so every ingestion test below has a stable,
# unambiguous "this should NOT be skipped" date.
TRADING_DAY = date(2026, 9, 16)
REPUBLIC_DAY = date(2026, 1, 26)


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


@pytest.fixture
def provider() -> FakeMarketDataProvider:
    return FakeMarketDataProvider()


async def test_incremental_daily_ingestion_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
    lake_root: Path,
    provider: FakeMarketDataProvider,
):
    async with db_session_factory() as db:
        p1 = await md.run_incremental_daily_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], as_of=TRADING_DAY
        )
    assert p1.status == "success"
    assert p1.rows_ingested == 1

    async with db_session_factory() as db:
        p2 = await md.run_incremental_daily_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], as_of=TRADING_DAY
        )
    assert p2.status == "success"

    async with db_session_factory() as db:
        result = await db.execute(
            select(DatasetFreshnessRecord).where(
                DatasetFreshnessRecord.symbol == "DEMOSTOCK",
                DatasetFreshnessRecord.data_type == "daily_ohlcv",
                DatasetFreshnessRecord.data_date == TRADING_DAY,
            )
        )
        rows = result.scalars().all()
    assert len(rows) == 1  # upserted, never duplicated

    read_back = lake.read_daily_bars(lake_root, "DEMOSTOCK", TRADING_DAY, TRADING_DAY)
    assert len(read_back) == 1  # the lake write itself never duplicated either


async def test_incremental_daily_ingestion_skips_nse_holiday(
    db_session_factory: async_sessionmaker[AsyncSession],
    lake_root: Path,
    provider: FakeMarketDataProvider,
):
    async with db_session_factory() as db:
        result = await md.run_incremental_daily_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], as_of=REPUBLIC_DAY
        )
    assert result.status == "success"
    assert result.symbols_processed == 0
    assert result.details["skipped_reason"] == "not an NSE trading day"
    assert lake.has_data_for(lake_root, "DEMOSTOCK", REPUBLIC_DAY) is False

    # Re-query from a FRESH session -- the caller's session already saw
    # this row via the same in-memory identity map regardless of whether
    # it was ever actually committed, so only a fresh session proves the
    # write really persisted (this caught a real bug: the holiday-skip
    # early return used to flush but never commit).
    async with db_session_factory() as verify_db:
        persisted = (
            await verify_db.execute(
                select(MarketDataProvenance).where(MarketDataProvenance.id == result.id)
            )
        ).scalar_one_or_none()
    assert persisted is not None
    assert persisted.details["skipped_reason"] == "not an NSE trading day"


async def test_intraday_ingestion_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
    lake_root: Path,
    provider: FakeMarketDataProvider,
):
    async with db_session_factory() as db:
        p1 = await md.run_intraday_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], day=TRADING_DAY
        )
    assert p1.status == "success"
    rows_first = p1.rows_ingested
    assert rows_first > 0

    async with db_session_factory() as db:
        p2 = await md.run_intraday_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], day=TRADING_DAY
        )
    assert p2.status == "success"
    assert p2.rows_ingested == rows_first  # same rows re-confirmed, nothing new

    write_result = lake.write_intraday_bars(
        lake_root, "DEMOSTOCK", provider.intraday_minute_bars("DEMOSTOCK", TRADING_DAY)
    )
    assert (
        write_result.rows_added == 0
    )  # re-writing the identical batch adds nothing at the storage layer


async def test_corporate_actions_ingestion_upsert_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession], provider: FakeMarketDataProvider
):
    since = date(2025, 1, 1)
    async with db_session_factory() as db:
        p1 = await md.run_corporate_actions_ingestion(
            db, provider=provider, symbols=["DEMOSTOCK"], since=since
        )
    async with db_session_factory() as db:
        p2 = await md.run_corporate_actions_ingestion(
            db, provider=provider, symbols=["DEMOSTOCK"], since=since
        )

    assert p1.status == "success"
    assert p2.status == "success"

    async with db_session_factory() as db:
        result = await db.execute(
            select(CorporateAction).where(CorporateAction.symbol == "DEMOSTOCK")
        )
        rows = result.scalars().all()
    # Whatever the deterministic provider produced (zero or one action for
    # this seed), a second identical run must not have duplicated it.
    assert len(rows) == p1.rows_ingested


async def test_instrument_master_sync_upsert_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession], provider: FakeMarketDataProvider
):
    async with db_session_factory() as db:
        await md.run_instrument_master_sync(db, provider=provider, symbols=["DEMOSTOCK"])
    async with db_session_factory() as db:
        await md.run_instrument_master_sync(db, provider=provider, symbols=["DEMOSTOCK"])

    async with db_session_factory() as db:
        result = await db.execute(select(Instrument).where(Instrument.symbol == "DEMOSTOCK"))
        rows = result.scalars().all()
    assert len(rows) == 1


async def test_bhavcopy_fallback_falls_back_to_synthetic_on_real_fetch_failure(
    db_session_factory: async_sessionmaker[AsyncSession], lake_root: Path
):
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    async with db_session_factory() as db:
        result = await md.run_bhavcopy_fallback(
            db,
            root=lake_root,
            symbols=["DEMOSTOCK"],
            day=TRADING_DAY,
            transport=httpx.MockTransport(fail_handler),
        )

    assert result.status == "success"
    assert result.source == "bhavcopy_fallback_synthetic"
    assert result.details["real_fetch_error"] is not None
    assert lake.has_data_for(lake_root, "DEMOSTOCK", TRADING_DAY) is True


async def test_bhavcopy_fallback_uses_real_data_when_fetch_succeeds(
    db_session_factory: async_sessionmaker[AsyncSession], lake_root: Path
):
    csv_body = (
        "SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,CLOSE_PRICE,"
        "AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY,DELIV_PER\n"
        "DEMOSTOCK,EQ,16-SEP-2026,100.0,101.0,105.0,99.0,103.0,102.5,102.0,10000,1000.0,500,5000,50.0\n"
    )

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=csv_body)

    async with db_session_factory() as db:
        result = await md.run_bhavcopy_fallback(
            db,
            root=lake_root,
            symbols=["DEMOSTOCK"],
            day=TRADING_DAY,
            transport=httpx.MockTransport(ok_handler),
        )

    assert result.status == "success"
    assert result.source == "nse_bhavcopy_real"
    assert result.details["real_fetch_error"] is None
    bars = lake.read_daily_bars(lake_root, "DEMOSTOCK", TRADING_DAY, TRADING_DAY)
    assert bars.iloc[0]["close"] == 102.5


async def test_catalog_refresh_and_backup_pipelines_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    lake_root: Path,
    provider: FakeMarketDataProvider,
    tmp_path: Path,
):
    async with db_session_factory() as db:
        await md.run_incremental_daily_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], as_of=TRADING_DAY
        )

    async with db_session_factory() as db:
        catalog_result = await md.run_catalog_refresh(db, root=lake_root)
    assert catalog_result.status == "success"
    assert catalog_result.details["ohlcv_daily_rows"] == 1

    async with db_session_factory() as db:
        backup_result = await md.run_nightly_backup(
            db, root=lake_root, backup_root=tmp_path / "backups"
        )
    assert backup_result.status == "success"
    assert backup_result.symbols_processed == 1  # one parquet file backed up


async def test_freshness_gate_correctly_reflects_real_pipeline_state(
    db_session_factory: async_sessionmaker[AsyncSession],
    lake_root: Path,
    provider: FakeMarketDataProvider,
):
    async with db_session_factory() as db:
        snapshot_before = await load_freshness_snapshot(db, ["DEMOSTOCK"])
    result_before = check_data_freshness(
        symbol="DEMOSTOCK", as_of=date(2026, 9, 17), data_lake=snapshot_before
    )
    assert result_before.fresh is False  # nothing ingested yet

    async with db_session_factory() as db:
        await md.run_incremental_daily_ingestion(
            db, provider=provider, root=lake_root, symbols=["DEMOSTOCK"], as_of=TRADING_DAY
        )

    async with db_session_factory() as db:
        snapshot_after = await load_freshness_snapshot(db, ["DEMOSTOCK"])
    # 2026-09-17's prior NSE trading day is 2026-09-16 (TRADING_DAY) --
    # now genuinely ingested, so the gate must now report fresh.
    result_after = check_data_freshness(
        symbol="DEMOSTOCK", as_of=date(2026, 9, 17), data_lake=snapshot_after
    )
    assert result_after.fresh is True
    assert result_after.required_date == TRADING_DAY
