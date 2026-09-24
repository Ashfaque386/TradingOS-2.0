import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agents.scheduler import start_heartbeat_loop
from src.api.router import api_router
from src.api.routes.health import router as health_router
from src.brokers.factory import build_configured_adapter
from src.brokers.tick_source import build_tick_source
from src.core.config import get_settings
from src.core.db import AsyncSessionLocal
from src.core.redis_client import get_redis
from src.data.price_provider import DataLakePriceProvider
from src.data.providers import FakeMarketDataProvider
from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.gateway.apply import apply_config_from_file
from src.gateway.watcher import ConfigWatcher
from src.models.agent_config_version import ConfigVersionStatus
from src.observability.audit_middleware import AuditLoggingMiddleware
from src.observability.correlation import CorrelationIdMiddleware
from src.observability.logging import configure_logging
from src.observability.metrics import trading_holiday_gauge_updater
from src.observability.rate_limit_middleware import RateLimitMiddleware
from src.observability.scheduler_registry import register_scheduler, unregister_all
from src.orchestration.audit_scheduler import start_audit_scheduler
from src.orchestration.investor_reporting_scheduler import start_investor_reporting_scheduler
from src.orchestration.live_trading_scheduler import start_live_trading_scheduler
from src.orchestration.market_data_scheduler import start_market_data_scheduler
from src.orchestration.notification_scheduler import start_notification_scheduler
from src.orchestration.paper_trading_scheduler import start_paper_trading_scheduler
from src.orchestration.post_trade_review_scheduler import start_post_trade_review_scheduler
from src.orchestration.recovery import reap_incomplete_runs
from src.orchestration.screener_scheduler import start_screener_scheduler
from src.orchestration.task_engine import drive_run_to_quiescence, start_stall_sweep_loop

configure_logging()

settings = get_settings()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config_path = Path(settings.agent_gateway_config_path)

    async with AsyncSessionLocal() as session:
        result = await apply_config_from_file(session, config_path, source="startup")

    if result.status == ConfigVersionStatus.ACTIVE:
        logger.info("agent_gateway.startup_config_applied", version=result.version_id)
    else:
        # Never crash the app over a bad seed config — it starts with no
        # gateway state loaded (matches hot-reload's "keep running" rule).
        logger.warning(
            "agent_gateway.startup_config_rejected",
            version=result.version_id,
            errors=result.errors,
        )

    watcher = ConfigWatcher(config_path)
    watcher.start()

    redis = get_redis()
    async with AsyncSessionLocal() as session:
        reaped = await reap_incomplete_runs(session, redis)

    recovery_tasks: list[asyncio.Task] = []
    if reaped:
        logger.info("orchestration.runs_reaped", run_ids=[str(r) for r in reaped])
        # Reaping only makes stuck tasks reclaimable; it doesn't itself
        # resume execution. drive_run_to_quiescence() safely no-ops for a
        # reaped run that isn't RUNNING (e.g. PLANNING, whose re-entry is a
        # narrower crash window not yet handled here), so it's safe to call
        # for every reaped run unconditionally. Fired as background tasks
        # (not awaited) so app startup — and the /health endpoint — isn't
        # blocked on however long recovery takes.
        recovery_tasks = [
            asyncio.create_task(drive_run_to_quiescence(AsyncSessionLocal, redis, run_id))
            for run_id in reaped
        ]

    stall_sweep_task = start_stall_sweep_loop(AsyncSessionLocal, redis)
    heartbeat_task = start_heartbeat_loop(AsyncSessionLocal)

    # Market Data & Data Lake (Build Spec §14) -- started before paper/live
    # trading below since both now read from the real ingested lake
    # (data_lake_price_provider), falling back per-symbol to the same
    # synthetic FakeDailyPriceProvider used pre-Phase-10 for any symbol
    # not yet ingested.
    data_lake_root = Path(settings.data_lake_path)
    market_data_scheduler = start_market_data_scheduler(
        AsyncSessionLocal,
        provider=FakeMarketDataProvider(),
        root=data_lake_root,
        backup_root=Path(settings.data_lake_backup_path),
    )
    data_lake_price_provider = DataLakePriceProvider(
        root=data_lake_root, fallback=FakeDailyPriceProvider()
    )

    # Autonomous Paper Trading Engine (Build Spec §11) -- the mock L2 depth
    # and reference-table regulatory data source remain the same Phase 10
    # stand-ins used throughout src.engine.paper_trading; daily prices as
    # of this phase come from the real data lake
    # (data_lake_price_provider), not a permanent fake. The tick source is
    # no longer a permanent mock as of Phase 8: build_tick_source() polls
    # real broker quotes when credentials are configured in the secrets
    # store, and falls back to the Phase 7 mock feed otherwise (which is
    # what this sandbox, with no live broker credentials, always
    # resolves to).
    paper_trading_scheduler = start_paper_trading_scheduler(
        AsyncSessionLocal,
        redis=redis,
        price_provider=data_lake_price_provider,
        order_book_provider=MockOrderBookProvider(),
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        tick_source=build_tick_source(),
    )

    # Autonomous LiveExecutionPipeline (Build Spec §12, Phase 18) --
    # always started, regardless of whether a broker is configured or any
    # subscription has autonomy enabled: intent GENERATION and the expiry
    # sweep are useful (and safe) with no broker at all, and every real
    # gate (master switch, Kill Switch, standing caps) is enforced inside
    # `generate_live_order_intent` itself before submission is ever
    # attempted. `build_configured_adapter(sandbox=False)` is the same
    # credential-lookup path build_tick_source() uses, always pointed at
    # production -- live trading must never share a code path with Shadow
    # Mode's dedicated sandbox-pointed adapter.
    live_trading_scheduler = start_live_trading_scheduler(
        AsyncSessionLocal,
        redis=redis,
        price_provider=data_lake_price_provider,
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        adapter=build_configured_adapter(sandbox=False),
    )

    # Audit archive + chain-divergence verification (Build Spec §19).
    audit_scheduler = start_audit_scheduler(
        AsyncSessionLocal, archive_root=Path(settings.audit_archive_path)
    )
    trading_holiday_task = trading_holiday_gauge_updater()

    # Daily summary alert (Build Spec §18) -- the one alert level with no
    # single triggering event; kill-switch/sign-off/go-live fire
    # synchronously from the real code paths that produce them instead.
    notification_scheduler = start_notification_scheduler(AsyncSessionLocal)

    # Phase 19 (docs/phase19-audit.md): Screener, Post-Trade Review, and
    # Investor Reporting agents -- each a real scheduled job, not a
    # pipeline node (see src.agents.roster.PIPELINE_NODE_AGENTS, still
    # exactly 13). Screener runs before the daily signal jobs (07:30 IST)
    # so a screener-created strategy exists in time for the same day;
    # Post-Trade Review runs after NSE close (16:00 IST); Investor
    # Reporting runs weekly (Monday 07:00 IST).
    screener_scheduler = start_screener_scheduler(
        AsyncSessionLocal, price_provider=data_lake_price_provider
    )
    post_trade_review_scheduler = start_post_trade_review_scheduler(AsyncSessionLocal)
    investor_reporting_scheduler = start_investor_reporting_scheduler(AsyncSessionLocal)

    # Phase 19 (docs/phase19-audit.md Part 1.2): registers every real
    # APScheduler instance so GET /api/v1/system/scheduled-jobs can read
    # their live next_run_time -- the audit's own finding was that no
    # cadence for any of these was surfaced anywhere in the UI.
    for name, scheduler in (
        ("market_data", market_data_scheduler),
        ("paper_trading", paper_trading_scheduler),
        ("live_trading", live_trading_scheduler),
        ("audit", audit_scheduler),
        ("notification", notification_scheduler),
        ("screener", screener_scheduler),
        ("post_trade_review", post_trade_review_scheduler),
        ("investor_reporting", investor_reporting_scheduler),
    ):
        register_scheduler(name, scheduler)

    yield

    stall_sweep_task.cancel()
    heartbeat_task.cancel()
    trading_holiday_task.cancel()
    for task in recovery_tasks:
        task.cancel()
    market_data_scheduler.shutdown(wait=False)
    paper_trading_scheduler.shutdown(wait=False)
    live_trading_scheduler.shutdown(wait=False)
    audit_scheduler.shutdown(wait=False)
    notification_scheduler.shutdown(wait=False)
    screener_scheduler.shutdown(wait=False)
    post_trade_review_scheduler.shutdown(wait=False)
    investor_reporting_scheduler.shutdown(wait=False)
    unregister_all()
    watcher.stop()


app = FastAPI(title="TradingOS 2.0 API", version="0.1.0", lifespan=lifespan)

# Read by src.observability.audit_middleware.AuditLoggingMiddleware, which
# runs as pure ASGI middleware outside FastAPI's dependency-injection graph
# and so can't use Depends(get_session_factory)/dependency_overrides the
# way route handlers do; tests override this attribute directly instead
# (see tests/conftest.py's db_session_factory fixture).
app.state.db_session_factory = AsyncSessionLocal

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Middleware wraps in reverse registration order -- CorrelationIdMiddleware
# (added last) is outermost, so a correlation ID is bound before anything
# else runs; AuditLoggingMiddleware (added just before it) sees that same
# ID via write_audit_entry's automatic contextvar pickup. RateLimitMiddleware
# sits between the two so a 429 it returns still carries a correlation ID
# (needs CorrelationIdMiddleware to have already run) but is rejected
# before AuditLoggingMiddleware or any route does real work.
app.add_middleware(AuditLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CorrelationIdMiddleware)

app.include_router(health_router)
app.include_router(api_router)
