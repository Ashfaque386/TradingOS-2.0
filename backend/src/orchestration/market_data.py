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

import inspect
import uuid
from collections.abc import Awaitable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pandas as pd
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.data import backup as backup_module
from src.data import bhavcopy as bhavcopy_module
from src.data import lake
from src.data.nse_calendar import is_nse_trading_day, previous_nse_trading_day
from src.data.providers import AnyMarketDataProvider, FakeMarketDataProvider, MarketPulseSnapshot
from src.models.corporate_action import CorporateAction
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.instrument import Instrument
from src.models.market_data_provenance import MarketDataProvenance

logger = structlog.get_logger(__name__)

PROVIDER_SOURCE_NAME = "fake_market_data_provider"

_T = TypeVar("_T")

# Fewer real daily bars than this and a symbol cannot drive a moving-average signal.
MIN_HISTORY_ROWS = 60
BACKFILL_DAYS = 400  # about a year of trading days
REPAIR_WINDOW_DAYS = 7  # re-fetched every morning: a broker's final daily bar can differ from the
# one composed at the close (the official close is a 30-minute average; the last trade is not)


async def _resolve(value: _T | Awaitable[_T]) -> _T:
    """A provider call's result, whether the provider is synchronous (the generated-data one) or
    does real I/O and returns an awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


def _source_name(provider: AnyMarketDataProvider) -> str:
    return str(getattr(provider, "source_name", PROVIDER_SOURCE_NAME))


def _tagged(bars: pd.DataFrame) -> pd.DataFrame:
    """Bars as the lake should store them: with a `source`. A provider that did not say where its
    bars came from is the generated-data one, so they are stored as `synthetic` -- never as real."""
    if lake.SOURCE_COLUMN in bars.columns:
        return bars
    return bars.assign(**{lake.SOURCE_COLUMN: lake.SYNTHETIC_SOURCE})


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
    details: dict[str, Any] | None = None,
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
    provider: AnyMarketDataProvider,
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
            source=_source_name(provider),
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
            bars = await _resolve(provider.daily_bars(symbol, as_of, as_of))
            if bars.empty:
                errors.append(f"{symbol}: provider returned no bar for {as_of.isoformat()}")
                continue
            lake.write_daily_bars(root, symbol, _tagged(bars))
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
        source=_source_name(provider),
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
    provider: AnyMarketDataProvider,
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
            bars = await _resolve(provider.intraday_minute_bars(symbol, day))
            if bars.empty:
                errors.append(f"{symbol}: provider returned no intraday bars for {day.isoformat()}")
                continue
            lake.write_intraday_bars(root, symbol, _tagged(bars))
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
        source=_source_name(provider),
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
    db: AsyncSession, *, provider: AnyMarketDataProvider, symbols: list[str], since: date
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    rows_ingested = 0
    symbols_processed = 0
    errors: list[str] = []
    for symbol in symbols:
        try:
            actions = await _resolve(provider.corporate_actions(symbol, since))
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
        source=_source_name(provider),
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
    db: AsyncSession, *, provider: AnyMarketDataProvider, symbols: list[str]
) -> MarketDataProvenance:
    started = datetime.now(UTC)
    records = await _resolve(provider.instrument_master(symbols))
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
        source=_source_name(provider),
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
    fallback_provider: AnyMarketDataProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
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
            bars = await _resolve(provider.daily_bars(symbol, day, day))
            if bars.empty:
                errors.append(f"{symbol}: synthetic fallback produced no bar")
                continue
            lake.write_daily_bars(root, symbol, _tagged(bars))
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


def get_market_pulse(provider: AnyMarketDataProvider) -> MarketPulseSnapshot:
    """The synchronous (generated-data) providers' pulse; `resolve_market_pulse` handles both."""
    pulse = provider.market_pulse()
    if inspect.isawaitable(pulse):
        raise TypeError("this provider's market_pulse is async: use resolve_market_pulse")
    return pulse


async def resolve_market_pulse(provider: AnyMarketDataProvider) -> MarketPulseSnapshot:
    return await _resolve(provider.market_pulse())


# ---- history: backfill and the morning gap repair ----


async def run_symbol_history_backfill(
    db: AsyncSession,
    *,
    provider: AnyMarketDataProvider,
    root: Path,
    symbol: str,
    as_of: date,
    days: int = BACKFILL_DAYS,
    replace_unreal: bool = True,
    pipeline: str = "history_backfill",
) -> MarketDataProvenance:
    """Fetch `days` of daily history ending `as_of` for one symbol and store it, tagged with its
    source. With `replace_unreal`, the symbol's generated or untagged rows are deleted first: real
    history must not sit in the same series as them. Never raises: a provider failure is a
    `failed` provenance row carrying the reason."""
    started = datetime.now(UTC)
    removed = 0
    rows = 0
    details: dict[str, Any] = {"symbol": symbol, "as_of": as_of.isoformat(), "days": days}
    error: str | None = None
    try:
        bars = await _resolve(provider.daily_bars(symbol, as_of - timedelta(days=days), as_of))
        if bars.empty:
            raise ValueError(f"{symbol}: the provider returned no daily bars")
        if replace_unreal:
            removed = lake.purge_unreal_daily_rows(root, symbol)
        lake.write_daily_bars(root, symbol, _tagged(bars))
        rows = len(bars)
        last_day = bars.index.max().date()
        if lake.has_data_for(root, symbol, last_day, subdir=lake.DAILY_SUBDIR):
            await _upsert_freshness_record(
                db, symbol=symbol, data_type="daily_ohlcv", data_date=last_day, row_count=rows
            )
        details.update(
            rows=rows,
            first_date=bars.index.min().date().isoformat(),
            last_date=last_day.isoformat(),
            source=str(bars[lake.SOURCE_COLUMN].iloc[0]) if lake.SOURCE_COLUMN in bars else None,
            unreal_rows_removed=removed,
        )
    except Exception as exc:  # noqa: BLE001 - a failed symbol is a recorded outcome, not a crash
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("market_data.history_backfill_failed", symbol=symbol, error=error)
    provenance = await _record_provenance(
        db,
        pipeline=pipeline,
        source=_source_name(provider),
        status="failed" if error else "success",
        symbols_processed=0 if error else 1,
        rows_ingested=rows,
        started_at=started,
        error_message=error[:1000] if error else None,
        details=details,
    )
    await db.commit()
    return provenance


def _real_history_state(root: Path, symbol: str, as_of: date) -> tuple[int, date | None]:
    """(real daily rows stored, the newest real day) for `symbol`."""
    bars = lake.read_daily_bars(root, symbol, date(2000, 1, 1), as_of, real_only=True)
    if bars.empty:
        return 0, None
    return len(bars), bars.index.max().date()


async def run_history_gap_repair(
    db: AsyncSession,
    *,
    provider: AnyMarketDataProvider,
    root: Path,
    symbols: list[str],
    as_of: date,
) -> list[MarketDataProvenance]:
    """Before the morning's signals: make sure every symbol has enough real history and that the
    previous trading day is in it. A symbol with too little (a new subscription, a long outage) gets
    a full backfill; any other gets the last `REPAIR_WINDOW_DAYS` re-fetched, which both fills a
    missed day and replaces a bar composed at the close with the broker's final one. Returns one
    provenance row per symbol; a `failed` row is the caller's cue to alert."""
    results: list[MarketDataProvenance] = []
    previous = previous_nse_trading_day(as_of)
    for symbol in symbols:
        count, newest = _real_history_state(root, symbol, as_of)
        if count < MIN_HISTORY_ROWS:
            results.append(
                await run_symbol_history_backfill(
                    db, provider=provider, root=root, symbol=symbol, as_of=as_of,
                    pipeline="history_gap_repair",
                )
            )  # fmt: skip
        else:
            results.append(
                await run_symbol_history_backfill(
                    db,
                    provider=provider,
                    root=root,
                    symbol=symbol,
                    as_of=as_of,
                    days=max(REPAIR_WINDOW_DAYS, (as_of - newest).days + 2)
                    if newest
                    else BACKFILL_DAYS,
                    replace_unreal=False,
                    pipeline="history_gap_repair",
                )
            )
        if newest is not None and newest < previous:
            logger.info("market_data.history_gap_found", symbol=symbol, newest=newest.isoformat())
    return results
