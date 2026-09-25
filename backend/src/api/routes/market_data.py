"""Market Data & Data Lake API (Build Spec §14): read-only visibility into
the ingestion pipelines' output (Market Pulse, freshness, instrument
master, provenance) plus manual triggers for every pipeline the scheduler
(`src.orchestration.market_data_scheduler`) already runs on its own --
the same "manual demo trigger exercises the exact function the scheduler
calls automatically, never a required step" posture as every prior
phase's demo-trigger endpoints (Phase 7's `/daily-signal-run`, Phase 9's
`/generate-intent`).
"""

from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from pathlib import Path

import pandas as pd
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    DatalakeStatusResponse,
    FreshnessRecordResponse,
    IndicatorSeriesResponse,
    InstrumentResponse,
    LiveOptionChainEntryResponse,
    LiveOptionChainResponse,
    MarketDataProvenanceResponse,
    MarketHoursResponse,
    MarketPulseResponse,
    PipelineStatusEntry,
    RunBhavcopyFallbackRequest,
    RunCorporateActionsIngestionRequest,
    RunDailyIngestionRequest,
    RunInstrumentMasterSyncRequest,
    RunIntradayIngestionRequest,
)
from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.data.lake import read_daily_bars
from src.data.providers import FakeMarketDataProvider
from src.engine.indicators import (
    bollinger_bands,
    exponential_moving_average,
    macd,
    relative_strength_index,
    simple_moving_average,
)
from src.engine.options_pricing import implied_volatility, year_fraction
from src.engine.paper_trading.market_hours import is_market_open_ist
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.instrument import Instrument
from src.models.market_data_provenance import MarketDataProvenance
from src.models.user import User
from src.orchestration import market_data as market_data_orch

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/market-data", tags=["market-data"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

# Must match MarketDataProvenance's own `ck_market_data_provenance_pipeline`
# CheckConstraint exactly -- the fixed, known set of pipelines this app
# runs, so the status view always reports all of them (a pipeline that has
# never run once shows as never-run, not silently missing from the list).
_KNOWN_PIPELINES = (
    "incremental_daily",
    "corporate_actions",
    "instrument_master",
    "intraday_minute",
    "bhavcopy_fallback",
    "catalog_refresh",
    "data_lake_backup",
)

register_policy("GET", "/api/v1/market-data/market-hours", roles=list(Role))
register_policy("GET", "/api/v1/market-data/pulse", roles=list(Role))
register_policy("GET", "/api/v1/market-data/freshness/{symbol}", roles=list(Role))
register_policy("GET", "/api/v1/market-data/indicators/{symbol}", roles=list(Role))
register_policy("GET", "/api/v1/market-data/instruments", roles=list(Role))
register_policy("GET", "/api/v1/market-data/option-chain/{underlying}", roles=list(Role))
register_policy("GET", "/api/v1/market-data/option-expiries/{underlying}", roles=list(Role))
register_policy("GET", "/api/v1/market-data/provenance", roles=list(Role))
register_policy("GET", "/api/v1/market-data/datalake/status", roles=list(Role))
register_policy("POST", "/api/v1/market-data/ingest/daily", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/ingest/intraday", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/ingest/corporate-actions", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/ingest/instrument-master", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/bhavcopy-fallback", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/catalog-refresh", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/market-data/backup", roles=_OPERATOR_ROLES)

# Module-level honest-stub provider -- the same instance
# src.orchestration.market_data_scheduler uses via src.main's lifespan.
_PROVIDER = FakeMarketDataProvider()


def get_market_data_broker_adapter() -> BrokerAdapter | None:
    """A thin `Depends`-wrapped seam around `build_configured_adapter`
    (Phase 8), the same pattern `src.api.routes.live_trading
    .get_live_broker_adapter` already established -- a read-only option
    chain query is safe against the production-pointed adapter (no order
    placement involved), and tests override this the same way that
    dependency is overridden, injecting an `httpx.MockTransport`-backed
    adapter without needing real broker credentials or network egress."""
    return build_configured_adapter(sandbox=False)


def get_data_lake_root() -> Path:
    return Path(get_settings().data_lake_path)


def get_data_lake_backup_root() -> Path:
    return Path(get_settings().data_lake_backup_path)


def _provenance_response(row: MarketDataProvenance) -> MarketDataProvenanceResponse:
    return MarketDataProvenanceResponse(
        id=row.id,
        pipeline=row.pipeline,
        source=row.source,
        status=row.status,
        symbols_processed=row.symbols_processed,
        rows_ingested=row.rows_ingested,
        error_message=row.error_message,
        details=row.details,
        started_at=row.started_at.isoformat(),
        completed_at=row.completed_at.isoformat(),
    )


@router.get("/market-hours")
async def market_hours_endpoint(
    _current_user: User = Depends(require_role),
) -> MarketHoursResponse:
    now = datetime.now(UTC)
    return MarketHoursResponse(is_open=is_market_open_ist(now), as_of=now.isoformat())


@router.get("/pulse")
async def market_pulse_endpoint(current_user: User = Depends(require_role)) -> MarketPulseResponse:
    pulse = market_data_orch.get_market_pulse(_PROVIDER)
    return MarketPulseResponse(
        as_of=pulse.as_of.isoformat(),
        india_vix=pulse.india_vix,
        sector_indices_change_pct=pulse.sector_indices_change_pct,
        global_indices_change_pct=pulse.global_indices_change_pct,
    )


@router.get("/freshness/{symbol}")
async def freshness_endpoint(
    symbol: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[FreshnessRecordResponse]:
    result = await db.execute(
        select(DatasetFreshnessRecord)
        .where(DatasetFreshnessRecord.symbol == symbol)
        .order_by(DatasetFreshnessRecord.data_date.desc())
    )
    return [
        FreshnessRecordResponse(
            symbol=row.symbol,
            data_type=row.data_type,
            data_date=row.data_date,
            row_count=row.row_count,
            ingested_at=row.ingested_at.isoformat(),
        )
        for row in result.scalars().all()
    ]


def _series_to_list(series: pd.Series) -> list[float | None]:
    return [None if pd.isna(value) else float(value) for value in series]


@router.get("/indicators/{symbol}")
async def indicators_endpoint(
    symbol: str,
    lookback_days: int = 250,
    sma_window: int = 20,
    ema_span: int = 20,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal_period: int = 9,
    bollinger_window: int = 20,
    bollinger_std: float = 2.0,
    root: Path = Depends(get_data_lake_root),
    current_user: User = Depends(require_role),
) -> IndicatorSeriesResponse:
    """Computes real technical indicators over whatever real OHLCV history
    the data lake actually has for `symbol` -- reads `src.data.lake`
    directly rather than `DataLakePriceProvider` (which silently falls
    back to synthetic data), so a symbol the pipeline hasn't ingested
    honestly comes back empty instead of a fabricated series."""
    end = datetime.now(UTC).date()
    start = end - timedelta(days=lookback_days)
    df = read_daily_bars(root, symbol, start, end)
    if df.empty:
        return IndicatorSeriesResponse(
            symbol=symbol,
            dates=[],
            close=[],
            sma=[],
            sma_window=sma_window,
            ema=[],
            ema_span=ema_span,
            rsi=[],
            rsi_period=rsi_period,
            macd=[],
            macd_signal=[],
            macd_histogram=[],
            bollinger_upper=[],
            bollinger_middle=[],
            bollinger_lower=[],
        )

    close = df["close"]
    macd_line, macd_sig, macd_hist = macd(close, macd_fast, macd_slow, macd_signal_period)
    boll_upper, boll_mid, boll_lower = bollinger_bands(close, bollinger_window, bollinger_std)
    return IndicatorSeriesResponse(
        symbol=symbol,
        dates=[d.strftime("%Y-%m-%d") for d in df.index],
        close=[float(v) for v in close],
        sma=_series_to_list(simple_moving_average(close, sma_window)),
        sma_window=sma_window,
        ema=_series_to_list(exponential_moving_average(close, ema_span)),
        ema_span=ema_span,
        rsi=_series_to_list(relative_strength_index(close, rsi_period)),
        rsi_period=rsi_period,
        macd=_series_to_list(macd_line),
        macd_signal=_series_to_list(macd_sig),
        macd_histogram=_series_to_list(macd_hist),
        bollinger_upper=_series_to_list(boll_upper),
        bollinger_middle=_series_to_list(boll_mid),
        bollinger_lower=_series_to_list(boll_lower),
    )


@router.get("/instruments")
async def instruments_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[InstrumentResponse]:
    result = await db.execute(select(Instrument).order_by(Instrument.symbol))
    return [
        InstrumentResponse(
            symbol=row.symbol,
            exchange=row.exchange,
            instrument_type=row.instrument_type,
            isin=row.isin,
            lot_size=row.lot_size,
            tick_size=row.tick_size,
            underlying_symbol=row.underlying_symbol,
            expiry_date=row.expiry_date,
            strike_price=row.strike_price,
            option_type=row.option_type,
            is_active=row.is_active,
            last_synced_at=row.last_synced_at.isoformat(),
        )
        for row in result.scalars().all()
    ]


@router.get("/option-chain/{underlying}")
async def live_option_chain_endpoint(
    underlying: str,
    expiry: str,
    _current_user: User = Depends(require_role),
    adapter: BrokerAdapter | None = Depends(get_market_data_broker_adapter),
) -> LiveOptionChainResponse:
    """Phase 16 audit follow-up D: a real live option chain (OI/IV/LTP),
    not the static instrument master alone. Reads through whichever
    broker is actually configured (Phase 8's `ResilientBrokerAdapter`,
    so a failing call counts against that broker's real circuit-breaker
    state same as any other dispatch) -- Zerodha has no option-chain
    endpoint at all and its adapter honestly raises `NotImplementedError`,
    surfaced here as a real 422 naming the broker and reason rather than
    a bare 500 or a silently empty list.

    Phase 17 real-world testing pass: also marks the ATM strike using a
    real spot-price lookup (`adapter.get_quote(underlying)`), not the
    synced instrument master -- confirmed live that the instrument-master
    sync pipeline's provider (`FakeMarketDataProvider.instrument_master`)
    has never actually generated an options row for any underlying (see
    docs/phase17-realworld-testing.md), so the broker's own live chain
    response (already the source `entries` comes from) is the only real
    strike source available and is reused for the spot lookup too."""
    if adapter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no broker is configured -- live option chain requires a real "
            "broker (Settings > Broker Config)",
        )
    try:
        entries = await adapter.get_option_chain(underlying, expiry)
    except NotImplementedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    underlying_ltp: float | None = None
    atm_strike: float | None = None
    if entries:
        try:
            spot = await adapter.get_quote(underlying)
            underlying_ltp = spot.last_price
        except Exception as exc:  # noqa: BLE001 - spot lookup is best-effort
            logger.warning(
                "market_data.option_chain_spot_lookup_failed",
                broker=adapter.broker_name,
                underlying=underlying,
                error=str(exc),
            )
        if underlying_ltp is not None:
            atm_strike = min(entries, key=lambda e: abs(e.strike - underlying_ltp)).strike

    # Best-effort Black-Scholes IV, only ever filling a gap the broker's
    # own API genuinely cannot answer (Zerodha -- Kite Connect has no
    # options-greeks field at all) -- never computed, let alone shown,
    # when the broker already reported a real call_iv/put_iv (Upstox).
    # A missing spot price or a malformed/already-elapsed expiry simply
    # leaves every computed field None, same "best-effort, never breaks
    # the real per-strike data" posture as the ATM block above.
    time_to_expiry: float | None = None
    if underlying_ltp is not None:
        try:
            time_to_expiry = year_fraction(
                date_type.fromisoformat(expiry), datetime.now(UTC).date()
            )
        except ValueError:
            time_to_expiry = None
    rate = get_settings().risk_free_rate

    def _computed_iv(
        option_type: str, strike: float, ltp: float | None, real_iv: float | None
    ) -> float | None:
        if real_iv is not None or ltp is None or underlying_ltp is None or not time_to_expiry:
            return None
        return implied_volatility(
            option_type=option_type,
            market_price=ltp,
            spot=underlying_ltp,
            strike=strike,
            time_to_expiry=time_to_expiry,
            rate=rate,
        )

    response_entries = [
        LiveOptionChainEntryResponse(
            strike=e.strike,
            call_symbol=e.call_symbol,
            put_symbol=e.put_symbol,
            call_ltp=e.call_ltp,
            put_ltp=e.put_ltp,
            call_oi=e.call_oi,
            put_oi=e.put_oi,
            call_iv=e.call_iv,
            put_iv=e.put_iv,
            call_iv_computed=_computed_iv("CE", e.strike, e.call_ltp, e.call_iv),
            put_iv_computed=_computed_iv("PE", e.strike, e.put_ltp, e.put_iv),
        )
        for e in entries
    ]

    return LiveOptionChainResponse(
        broker=adapter.broker_name,
        underlying=underlying,
        expiry=expiry,
        underlying_ltp=underlying_ltp,
        atm_strike=atm_strike,
        entries=response_entries,
    )


@router.get("/option-expiries/{underlying}")
async def live_option_expiries_endpoint(
    underlying: str,
    _current_user: User = Depends(require_role),
    adapter: BrokerAdapter | None = Depends(get_market_data_broker_adapter),
) -> list[str]:
    if adapter is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no broker is configured -- live option expiries require a real "
            "broker (Settings > Broker Config)",
        )
    try:
        return await adapter.get_expiries(underlying)
    except NotImplementedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/provenance")
async def provenance_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[MarketDataProvenanceResponse]:
    result = await db.execute(
        select(MarketDataProvenance).order_by(MarketDataProvenance.completed_at.desc()).limit(50)
    )
    return [_provenance_response(row) for row in result.scalars().all()]


@router.get("/datalake/status")
async def datalake_status_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> DatalakeStatusResponse:
    """A composed read (Build Spec §14, docs/phase20-old-vs-new-comparison.md
    item 35): per real pipeline, its most recent run and its most recent
    *successful* run, plus lake-wide symbol coverage -- computes nothing
    new, the same "purpose-built shape over data that already exists"
    posture as Phase 22's Live Canvas. `/provenance` already exposes the
    raw run log and `/freshness/{symbol}` the per-symbol detail; this is
    the one "is the lake healthy right now" view neither answers alone --
    every pipeline in `_KNOWN_PIPELINES` is always listed, even one that
    has never run, rather than silently omitted."""
    latest_by_pipeline = {
        row.pipeline: row
        for row in (
            await db.execute(
                select(MarketDataProvenance)
                .distinct(MarketDataProvenance.pipeline)
                .order_by(MarketDataProvenance.pipeline, MarketDataProvenance.completed_at.desc())
            )
        )
        .scalars()
        .all()
    }
    latest_success_by_pipeline = {
        row.pipeline: row
        for row in (
            await db.execute(
                select(MarketDataProvenance)
                .where(MarketDataProvenance.status == "success")
                .distinct(MarketDataProvenance.pipeline)
                .order_by(MarketDataProvenance.pipeline, MarketDataProvenance.completed_at.desc())
            )
        )
        .scalars()
        .all()
    }

    pipelines: list[PipelineStatusEntry] = []
    for name in _KNOWN_PIPELINES:
        last = latest_by_pipeline.get(name)
        success = latest_success_by_pipeline.get(name)
        pipelines.append(
            PipelineStatusEntry(
                pipeline=name,
                last_run_status=last.status if last else None,
                last_run_at=last.completed_at.isoformat() if last else None,
                last_run_source=last.source if last else None,
                last_error=last.error_message if last else None,
                last_success_at=success.completed_at.isoformat() if success else None,
            )
        )

    daily_symbols, daily_max_date = (
        await db.execute(
            select(
                func.count(func.distinct(DatasetFreshnessRecord.symbol)),
                func.max(DatasetFreshnessRecord.data_date),
            ).where(DatasetFreshnessRecord.data_type == "daily_ohlcv")
        )
    ).one()
    intraday_symbols, intraday_max_date = (
        await db.execute(
            select(
                func.count(func.distinct(DatasetFreshnessRecord.symbol)),
                func.max(DatasetFreshnessRecord.data_date),
            ).where(DatasetFreshnessRecord.data_type == "intraday_ohlcv")
        )
    ).one()

    return DatalakeStatusResponse(
        as_of=datetime.now(UTC).isoformat(),
        pipelines=pipelines,
        symbols_with_daily_data=daily_symbols or 0,
        symbols_with_intraday_data=intraday_symbols or 0,
        most_recent_daily_data_date=daily_max_date,
        most_recent_intraday_data_date=intraday_max_date,
    )


@router.post("/ingest/daily")
async def run_daily_ingestion_endpoint(
    body: RunDailyIngestionRequest,
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_data_lake_root),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    as_of = body.as_of or datetime.now().date()
    result = await market_data_orch.run_incremental_daily_ingestion(
        db, provider=_PROVIDER, root=root, symbols=body.symbols, as_of=as_of
    )
    return _provenance_response(result)


@router.post("/ingest/intraday")
async def run_intraday_ingestion_endpoint(
    body: RunIntradayIngestionRequest,
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_data_lake_root),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    day = body.day or datetime.now().date()
    result = await market_data_orch.run_intraday_ingestion(
        db, provider=_PROVIDER, root=root, symbols=body.symbols, day=day
    )
    return _provenance_response(result)


@router.post("/ingest/corporate-actions")
async def run_corporate_actions_endpoint(
    body: RunCorporateActionsIngestionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    since = body.since or date_type(2020, 1, 1)
    result = await market_data_orch.run_corporate_actions_ingestion(
        db, provider=_PROVIDER, symbols=body.symbols, since=since
    )
    return _provenance_response(result)


@router.post("/ingest/instrument-master")
async def run_instrument_master_endpoint(
    body: RunInstrumentMasterSyncRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    result = await market_data_orch.run_instrument_master_sync(
        db, provider=_PROVIDER, symbols=body.symbols
    )
    return _provenance_response(result)


@router.post("/bhavcopy-fallback")
async def run_bhavcopy_fallback_endpoint(
    body: RunBhavcopyFallbackRequest,
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_data_lake_root),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    result = await market_data_orch.run_bhavcopy_fallback(
        db, root=root, symbols=body.symbols, day=body.day, segment=body.segment
    )
    return _provenance_response(result)


@router.post("/catalog-refresh")
async def run_catalog_refresh_endpoint(
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_data_lake_root),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    result = await market_data_orch.run_catalog_refresh(db, root=root)
    return _provenance_response(result)


@router.post("/backup")
async def run_backup_endpoint(
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_data_lake_root),
    backup_root: Path = Depends(get_data_lake_backup_root),
    current_user: User = Depends(require_role),
) -> MarketDataProvenanceResponse:
    result = await market_data_orch.run_nightly_backup(db, root=root, backup_root=backup_root)
    return _provenance_response(result)
