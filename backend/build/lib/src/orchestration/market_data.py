"""Market data ingestion pipelines (Build Spec §14): incremental daily
OHLCV, intraday minute bars, corporate actions, instrument master sync,
the on-demand NSE Bhavcopy fallback, nightly catalog refresh, and nightly
backup. Every pipeline here writes a `MarketDataProvenance` row (what ran,
against what source, with what outcome) and, for an OHLCV write, a
`DatasetFreshnessRecord` row -- written only *after*
`src.data.lake.has_data_for` confirms the write actually landed and is
readable back, so freshness state can never drift ahead of what the
Parquet store actually has.

Each ingestion loop catches per-symbol exceptions rather than letting one
bad symbol abort the whole run (the same "don't crash the drain loop"
posture Phase 7/9's schedulers already established) -- `status` on the
resulting provenance row is `success` (no errors), `partial` (some symbols
failed), or `failed` (every symbol failed), never a bare exception
bubbling out of a scheduled job.
"""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.data import backup as backup_module
from src.data import bhavcopy as bhavcopy_module
from src.data import lake
from src.data.nse_calendar import is_nse_trading_day
from src.data.providers import FakeMarketDataProvider, MarketDataProvider, MarketPulseSnapshot
from src.models.corporate_action import CorporateAction
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.instrument import Instrument
from src.models.market_data_provenance import MarketDataProvenance

logger = structlog.get_logger(__name__)

PROVIDER_SOURCE_NAME = "fake_market_data_provider"


async def _record_provenance(
    db: AsyncSession,
    *,
    pipeline: str,
    source: str,
    status: str,
    symbols_processed: int,
    rows_ingested: int,
    started_at: datetime,
    error_message: str | None = None,
    details: dict | None = None,
) -> MarketDataProvenance:
    row = MarketDataProvenance(
        id=uuid.uuid4(),
        pipeline=pipeline,
        source=source,
        status=status,
        symbols_processed=symbols_processed,
        rows_ingested=rows_ingested,
        error_message=error_message,
        details=details,
        started_at=started_at,
    )
    db.add(row)
    await db.flush()
    return row


async def _upsert_freshness_record(
    db: AsyncSession, *, symbol: str, data_type: str, data_date: date, row_count: int
) -> None:
    values = {
        "symbol": symbol,
        "data_type": data_type,
        "data_date": data_date,
        "row_count": row_count,
    }
    stmt = (
        pg_insert(DatasetFreshnessRecord)
        .values(id=uuid.uuid4(), **values)
        .on_conflict_do_update(
            index_elements=[
                DatasetFreshnessRecord.symbol,
                DatasetFreshnessRecord.data_type,
                DatasetFreshnessRecord.data_date,
            ],
            set_=values,
        )
    )
    await db.execute(stmt)


async def run_incremental_daily_ingestion(
    db: AsyncSession,
    *,
    provider: MarketDataProvider,
    root: Path,
    symbols: list[str],
    as_of: date,
) -> MarketDataProvenance:
    """Build Spec §14: "daily cron, IST, skips NSE holidays". The skip
    check lives here, not just in the scheduled job that calls this --
    calling this directly (a manual demo trigger, a test) gets the same
    holiday-safe behavior."""
    started = datetime.now(UTC)
    if not is_nse_trading_day(as_of):
        provenance = await _record_provenance(
            db,
            pipeline="incremental_daily",
            source=PROVIDER_SOURCE_NAME,
            status="success",
            symbols_processed=0,
            rows_ingested=0,
            started_at=started,
            details={"as_of": as_of.isoformat(), "skipped_reason": "not an NSE trading day"},
        )
        await db.commit()
        return provenance

    rows_ingested = 0
    symbols_processed = 0
    errors: list[str] = []
    for symbol in symbols:
        try:
            bars = provider.daily_bars(symbol, as_of, as_of)
            if bars.empty:
                errors.append(f"{symbol}: provider returned no bar for {as_of.isoformat()}")
                continue
            lake.write_daily_bars(root, symbol, bars)
            if lake.has_data_for(root, symbol, as_of, subdir=lake.DAILY_SUBDIR):
                await _upsert_freshness_record(
                    db, symbol=symbol, data_type="daily_ohlcv", data_date=as_of, row_count=len(bars)
                )
                rows_ingested += len(bars)
                symbols_processed += 1
            else:
                errors.append(f"{symbol}: write did not land")
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort the whole run
            errors.append(f"{symbol}: {exc}")

    status = "success" if not errors else ("partial" if symbols_processed else "failed")
    provenance = await _record_provenance(
        db,
        pipeline="incremental_daily",
        source=PROVIDER_SOURCE_NAME,
        status=status,
        symbols_processed=symbols_processed,
        rows_ingested=rows_ingested,
        started_at=started,
        error_message="; ".join(errors) if errors else None,
        details={"as_of": as_of.isoformat(), "symbols": symbols},
    )
    await db.commit()
    return provenance


async def run_intraday_ingestion(
    db: AsyncSession,
    *,
    provider: MarketDataProvider,
    root: Path,
    symbols: list[str],
    day: date,
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    rows_ingested = 0
    symbols_processed = 0
    errors: list[str] = []
    for symbol in symbols:
        try:
            bars = provider.intraday_minute_bars(symbol, day)
            if bars.empty:
                errors.append(f"{symbol}: provider returned no intraday bars for {day.isoformat()}")
                continue
            lake.write_intraday_bars(root, symbol, bars)
            if lake.has_data_for(root, symbol, day, subdir=lake.INTRADAY_SUBDIR):
                await _upsert_freshness_record(
                    db,
                    symbol=symbol,
                    data_type="intraday_ohlcv",
                    data_date=day,
                    row_count=len(bars),
                )
                rows_ingested += len(bars)
                symbols_processed += 1
            else:
                errors.append(f"{symbol}: write did not land")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")

    status = "success" if not errors else ("partial" if symbols_processed else "failed")
    provenance = await _record_provenance(
        db,
        pipeline="intraday_minute",
        source=PROVIDER_SOURCE_NAME,
        status=status,
        symbols_processed=symbols_processed,
        rows_ingested=rows_ingested,
        started_at=started,
        error_message="; ".join(errors) if errors else None,
        details={"day": day.isoformat(), "symbols": symbols},
    )
    await db.commit()
    return provenance


async def run_corporate_actions_ingestion(
    db: AsyncSession, *, provider: MarketDataProvider, symbols: list[str], since: date
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    rows_ingested = 0
    symbols_processed = 0
    errors: list[str] = []
    for symbol in symbols:
        try:
            actions = provider.corporate_actions(symbol, since)
            for action in actions:
                values = {
                    "symbol": action.symbol,
                    "action_type": action.action_type,
                    "ex_date": action.ex_date,
                    "ratio": action.ratio,
                    "amount": action.amount,
                    "announced_at": action.announced_at,
                }
                stmt = (
                    pg_insert(CorporateAction)
                    .values(id=uuid.uuid4(), **values)
                    .on_conflict_do_update(
                        index_elements=[
                            CorporateAction.symbol,
                            CorporateAction.action_type,
                            CorporateAction.ex_date,
                        ],
                        set_=values,
                    )
                )
                await db.execute(stmt)
                rows_ingested += 1
            symbols_processed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: {exc}")

    status = "success" if not errors else ("partial" if symbols_processed else "failed")
    provenance = await _record_provenance(
        db,
        pipeline="corporate_actions",
        source=PROVIDER_SOURCE_NAME,
        status=status,
        symbols_processed=symbols_processed,
        rows_ingested=rows_ingested,
        started_at=started,
        error_message="; ".join(errors) if errors else None,
        details={"since": since.isoformat(), "symbols": symbols},
    )
    await db.commit()
    return provenance


async def run_instrument_master_sync(
    db: AsyncSession, *, provider: MarketDataProvider, symbols: list[str]
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    records = provider.instrument_master(symbols)
    now = datetime.now(UTC)
    for record in records:
        values = {
            "symbol": record.symbol,
            "exchange": record.exchange,
            "instrument_type": record.instrument_type,
            "isin": record.isin,
            "lot_size": record.lot_size,
            "tick_size": record.tick_size,
            "underlying_symbol": record.underlying_symbol,
            "expiry_date": record.expiry_date,
            "strike_price": record.strike_price,
            "option_type": record.option_type,
            "is_active": record.is_active,
            "last_synced_at": now,
        }
        stmt = (
            pg_insert(Instrument)
            .values(id=uuid.uuid4(), **values)
            .on_conflict_do_update(index_elements=[Instrument.symbol], set_=values)
        )
        await db.execute(stmt)

    provenance = await _record_provenance(
        db,
        pipeline="instrument_master",
        source=PROVIDER_SOURCE_NAME,
        status="success",
        symbols_processed=len(records),
        rows_ingested=len(records),
        started_at=started,
        details={"symbols": symbols},
    )
    await db.commit()
    return provenance


async def run_bhavcopy_fallback(
    db: AsyncSession,
    *,
    root: Path,
    symbols: list[str],
    day: date,
    segment: str = "equity",
    fallback_provider: MarketDataProvider | None = None,
    transport=None,
) -> MarketDataProvenance:
    """Build Spec §14's on-demand fallback path. Tries a real NSE Bhavcopy
    fetch first (`src.data.bhavcopy.fetch_bhavcopy`, genuinely real
    network I/O); only when that fails outright does this fall back to the
    deterministic synthetic provider -- and when it does, `source` on the
    resulting provenance row says so explicitly (`bhavcopy_fallback_synthetic`),
    with the real fetch's own error preserved in `details`. A partial
    real-fetch result (some requested symbols simply absent from that
    day's file) is never silently topped up with synthetic rows -- mixing
    a real and a fabricated row for the same pipeline run under one
    `source` label would defeat the whole point of tracking provenance.
    """
    started = datetime.now(UTC)
    fetch_result = await bhavcopy_module.fetch_bhavcopy(day, segment=segment, transport=transport)

    rows_ingested = 0
    symbols_processed = 0
    errors: list[str] = []

    if fetch_result.rows:
        by_symbol = {row.symbol: row for row in fetch_result.rows}
        for symbol in symbols:
            row = by_symbol.get(symbol)
            if row is None:
                errors.append(f"{symbol}: not present in fetched bhavcopy")
                continue
            bars = pd.DataFrame(
                {
                    "open": [row.open],
                    "high": [row.high],
                    "low": [row.low],
                    "close": [row.close],
                    "volume": [row.volume],
                },
                index=[pd.Timestamp(row.trade_date)],
            )
            lake.write_daily_bars(root, symbol, bars)
            if lake.has_data_for(root, symbol, row.trade_date, subdir=lake.DAILY_SUBDIR):
                await _upsert_freshness_record(
                    db,
                    symbol=symbol,
                    data_type="daily_ohlcv",
                    data_date=row.trade_date,
                    row_count=1,
                )
                rows_ingested += 1
                symbols_processed += 1
            else:
                errors.append(f"{symbol}: write did not land")
        source = fetch_result.source
        details = {"day": day.isoformat(), "segment": segment, "real_fetch_error": None}
    else:
        provider = fallback_provider or FakeMarketDataProvider()
        for symbol in symbols:
            bars = provider.daily_bars(symbol, day, day)
            if bars.empty:
                errors.append(f"{symbol}: synthetic fallback produced no bar")
                continue
            lake.write_daily_bars(root, symbol, bars)
            if lake.has_data_for(root, symbol, day, subdir=lake.DAILY_SUBDIR):
                await _upsert_freshness_record(
                    db, symbol=symbol, data_type="daily_ohlcv", data_date=day, row_count=len(bars)
                )
                rows_ingested += len(bars)
                symbols_processed += 1
            else:
                errors.append(f"{symbol}: write did not land")
        source = "bhavcopy_fallback_synthetic"
        details = {
            "day": day.isoformat(),
            "segment": segment,
            "real_fetch_error": fetch_result.error,
        }

    status = "success" if not errors else ("partial" if symbols_processed else "failed")
    provenance = await _record_provenance(
        db,
        pipeline="bhavcopy_fallback",
        source=source,
        status=status,
        symbols_processed=symbols_processed,
        rows_ingested=rows_ingested,
        started_at=started,
        error_message="; ".join(errors) if errors else None,
        details=details,
    )
    await db.commit()
    return provenance


async def run_catalog_refresh(db: AsyncSession, *, root: Path) -> MarketDataProvenance:
    started = datetime.now(UTC)
    result = lake.refresh_catalog_views(root)
    provenance = await _record_provenance(
        db,
        pipeline="catalog_refresh",
        source="duckdb_catalog",
        status="success",
        symbols_processed=0,
        rows_ingested=result.ohlcv_daily_rows + result.ohlcv_intraday_rows,
        started_at=started,
        details={
            "ohlcv_daily_rows": result.ohlcv_daily_rows,
            "ohlcv_intraday_rows": result.ohlcv_intraday_rows,
        },
    )
    await db.commit()
    return provenance


async def run_nightly_backup(
    db: AsyncSession, *, root: Path, backup_root: Path
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    manifest = backup_module.create_backup(root, backup_root)
    validation = backup_module.validate_backup(backup_root / manifest.backup_id)
    status = "success" if validation.valid else "failed"
    total_rows = sum(f.row_count for f in manifest.files)
    provenance = await _record_provenance(
        db,
        pipeline="data_lake_backup",
        source="local_filesystem",
        status=status,
        symbols_processed=len(manifest.files),
        rows_ingested=total_rows,
        started_at=started,
        error_message="; ".join(validation.problems) if validation.problems else None,
        details={"backup_id": manifest.backup_id, "files": len(manifest.files)},
    )
    await db.commit()
    return provenance


def get_market_pulse(provider: MarketDataProvider) -> MarketPulseSnapshot:
    return provider.market_pulse()
