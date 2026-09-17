"""Market Data & Data Lake API (Build Spec §14): read-only visibility into
the ingestion pipelines' output (Market Pulse, freshness, instrument
master, provenance) plus manual triggers for every pipeline the scheduler
(`src.orchestration.market_data_scheduler`) already runs on its own --
the same "manual demo trigger exercises the exact function the scheduler
calls automatically, never a required step" posture as every prior
phase's demo-trigger endpoints (Phase 7's `/daily-signal-run`, Phase 9's
`/generate-intent`).
"""

from datetime import date as date_type
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    FreshnessRecordResponse,
    InstrumentResponse,
    MarketDataProvenanceResponse,
    MarketPulseResponse,
    RunBhavcopyFallbackRequest,
    RunCorporateActionsIngestionRequest,
    RunDailyIngestionRequest,
    RunInstrumentMasterSyncRequest,
    RunIntradayIngestionRequest,
)
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.data.providers import FakeMarketDataProvider
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.instrument import Instrument
from src.models.market_data_provenance import MarketDataProvenance
from src.models.user import User
from src.orchestration import market_data as market_data_orch

router = APIRouter(prefix="/market-data", tags=["market-data"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("GET", "/api/v1/market-data/pulse", roles=list(Role))
register_policy("GET", "/api/v1/market-data/freshness/{symbol}", roles=list(Role))
register_policy("GET", "/api/v1/market-data/instruments", roles=list(Role))
register_policy("GET", "/api/v1/market-data/provenance", roles=list(Role))
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


@router.get("/provenance")
async def provenance_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[MarketDataProvenanceResponse]:
    result = await db.execute(
        select(MarketDataProvenance).order_by(MarketDataProvenance.completed_at.desc()).limit(50)
    )
    return [_provenance_response(row) for row in result.scalars().all()]


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
