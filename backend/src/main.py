import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agents.scheduler import start_heartbeat_loop
from src.api.paging import TOTAL_COUNT_HEADER
from src.api.router import api_router
from src.api.routes.health import router as health_router
from src.brokers.factory import get_live_adapter
from src.brokers.tick_source import get_tick_source
from src.core.config import get_settings
from src.core.db import AsyncSessionLocal
from src.core.redis_client import get_redis
from src.core.security import warn_if_signing_key_weak
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import build_signal_price_provider
from src.engine.paper_trading.depth import build_order_book_provider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.gateway.apply import apply_config_from_file
from src.gateway.watcher import ConfigWatcher
from src.models.agent_config_version import ConfigVersionStatus
from src.models.organization_run import RunStatus
from src.notifications.alertmanager_token import ensure_token
from src.observability.audit_middleware import AuditLoggingMiddleware
from src.observability.correlation import CorrelationIdMiddleware
from src.observability.logging import configure_logging
from src.observability.metrics import trading_holiday_gauge_updater
from src.observability.rate_limit_middleware import RateLimitMiddleware
from src.observability.scheduled_job_history import attach_run_history_listener
from src.observability.scheduler_registry import register_scheduler, unregister_all
from src.observability.security_headers import SecurityHeadersMiddleware
from src.orchestration.audit_scheduler import start_audit_scheduler
from src.orchestration.backup_scheduler import backup_gauge_updater, start_backup_scheduler
from src.orchestration.engine_alert_watcher import start_engine_alert_watcher
from src.orchestration.investor_reporting_scheduler import start_investor_reporting_scheduler
from src.orchestration.live_trading_scheduler import start_live_trading_scheduler
from src.orchestration.market_data_scheduler import start_market_data_scheduler
from src.orchestration.notification_scheduler import start_notification_scheduler
from src.orchestration.paper_trading_scheduler import start_paper_trading_scheduler
from src.orchestration.pipeline_events import (
    reap_abandoned_runs,
    start_abandoned_run_reaper_loop,
)
from src.orchestration.post_trade_review_scheduler import start_post_trade_review_scheduler
from src.orchestration.recovery import reap_incomplete_runs
from src.orchestration.risk_monitor_scheduler import start_risk_monitor_scheduler
from src.orchestration.screener_scheduler import start_screener_scheduler
from src.orchestration.shadow_mode_scheduler import start_shadow_mode_scheduler
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

    try:
        async with AsyncSessionLocal() as session:
            await warn_if_signing_key_weak(session)
    except Exception:  # noqa: BLE001 - a diagnostic must never stop the app starting
        logger.warning("security.jwt_signing_key_check_failed")

    # The bearer token Alertmanager presents to the alert webhook: created on first boot, on a
    # volume of its own (see src/notifications/alertmanager_token.py).
    ensure_token(settings.alertmanager_token_path)

    watcher = ConfigWatcher(config_path)
    watcher.start()

    redis = get_redis()
    async with AsyncSessionLocal() as session:
        reaped = await reap_incomplete_runs(session, redis)

    recovery_tasks: list[asyncio.Task[RunStatus]] = []
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

    # A LangGraph pipeline run lives inside one HTTP request, so a restart or a
    # crash leaves it with no ending: end those runs now, then keep sweeping.
    try:
        await reap_abandoned_runs(AsyncSessionLocal, redis)
    except Exception:  # noqa: BLE001 - never stop the app starting over a tidy-up
        logger.warning("pipeline_events.startup_reap_failed")
    pipeline_reaper_task = start_abandoned_run_reaper_loop(AsyncSessionLocal, redis)

    stall_sweep_task = start_stall_sweep_loop(AsyncSessionLocal, redis)
    heartbeat_task = start_heartbeat_loop(AsyncSessionLocal)

    # Market Data & Data Lake (Build Spec §14) -- started before paper/live
    # trading below since both now read from the real ingested lake
    # (data_lake_price_provider). A symbol with too little ingested history is
    # skipped ("awaiting history"), never filled in with generated bars -- unless
    # PAPER_TRADING_SYNTHETIC is set (demo/tests).
    data_lake_root = Path(settings.data_lake_path)
    market_data_scheduler = start_market_data_scheduler(
        AsyncSessionLocal,
        provider=get_market_data_provider(),
        root=data_lake_root,
        backup_root=Path(settings.data_lake_backup_path),
    )
    data_lake_price_provider = build_signal_price_provider(
        data_lake_root, synthetic=settings.paper_trading_synthetic
    )

    # Autonomous Paper Trading Engine (Build Spec §11) -- the mock L2 depth
    # and reference-table regulatory data source remain the same Phase 10
    # stand-ins used throughout src.engine.paper_trading; daily prices as
    # of this phase come from the real data lake
    # (data_lake_price_provider), not a permanent fake. The tick source is
    # no longer a permanent mock as of Phase 8: build_tick_source() polls
    # real broker quotes when credentials are configured in the secrets
    # store; with none it is an UnavailableTickSource (the subscription waits),
    # not the mock feed, unless PAPER_TRADING_SYNTHETIC is set. Passed as
    # a factory (get_tick_source itself, not get_tick_source()) so the
    # tick-publish job re-resolves it on every firing -- a broker
    # connected or disconnected later via Settings now takes effect on
    # that job's next firing instead of only after a backend restart
    # (Phase 17 Part 4 real-world testing fix); get_tick_source only
    # actually rebuilds when the configured broker/credentials changed,
    # see src.brokers.tick_source's own docstring for why.
    paper_trading_scheduler = start_paper_trading_scheduler(
        AsyncSessionLocal,
        redis=redis,
        price_provider=data_lake_price_provider,
        order_book_provider=build_order_book_provider(redis),
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        tick_source_factory=get_tick_source,
    )

    # Autonomous LiveExecutionPipeline (Build Spec §12, Phase 18) --
    # always started, regardless of whether a broker is configured or any
    # subscription has autonomy enabled: intent GENERATION and the expiry
    # sweep are useful (and safe) with no broker at all, and every real
    # gate (master switch, Kill Switch, standing caps) is enforced inside
    # `generate_live_order_intent` itself before submission is ever
    # attempted. `get_live_adapter` (`build_configured_adapter(sandbox=False)`, rebuilt when the
    # stored credentials change) is the same
    # credential-lookup path build_tick_source() uses, always pointed at
    # production -- live trading must never share a code path with Shadow
    # Mode's dedicated sandbox-pointed adapter.
    live_trading_scheduler = start_live_trading_scheduler(
        AsyncSessionLocal,
        redis=redis,
        price_provider=data_lake_price_provider,
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        adapter_factory=get_live_adapter,
    )

    # Audit archive + chain-divergence verification (Build Spec §19).
    audit_scheduler = start_audit_scheduler(
        AsyncSessionLocal, archive_root=Path(settings.audit_archive_path)
    )
    trading_holiday_task = trading_holiday_gauge_updater()

    # Nightly Postgres backup (Phase 7): a dump and a manifest on a volume of its own, retention,
    # and an engine alert the night it fails.
    backup_scheduler = start_backup_scheduler(
        AsyncSessionLocal,
        root=Path(settings.postgres_backup_path),
        database_url=settings.database_url,
    )
    # What notices a night the job never ran (Prometheus rule BackupStale); a failed run raises its
    # own engine alert.
    backup_gauge_task = backup_gauge_updater(
        Path(settings.postgres_backup_path), Path(settings.data_lake_backup_path)
    )

    # Automatic kill switch (Phase 3): reads each mode's equity every minute while the market is
    # open and once at the close, and trips the switch when the drawdown reaches the applied limit.
    risk_monitor_scheduler = start_risk_monitor_scheduler(AsyncSessionLocal, redis=redis)

    # The Go-Live gate's clean-Shadow-Mode streak (Phase 4): one dry-run order check per trading
    # day for each strategy in paper trading, so the streak builds up without anyone running it.
    shadow_mode_scheduler = start_shadow_mode_scheduler(AsyncSessionLocal)

    # Engine alerts (Phase 5): a minute-by-minute look at what the Paper Desk would flag, so a
    # position nobody is watching or a price feed that has stopped reaches a person who is not
    # looking at the page.
    engine_alert_scheduler = start_engine_alert_watcher(
        AsyncSessionLocal, redis=redis, price_provider=data_lake_price_provider
    )

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
        ("backups", backup_scheduler),
        ("risk_monitor", risk_monitor_scheduler),
        ("shadow_mode", shadow_mode_scheduler),
        ("engine_alerts", engine_alert_scheduler),
        ("notification", notification_scheduler),
        ("screener", screener_scheduler),
        ("post_trade_review", post_trade_review_scheduler),
        ("investor_reporting", investor_reporting_scheduler),
    ):
        register_scheduler(name, scheduler)
        # Phase 22 (docs/phase20-old-vs-new-comparison.md item 20): a
        # durable record of each job's real firings, since APScheduler
        # itself keeps none once a firing completes.
        attach_run_history_listener(scheduler, name, AsyncSessionLocal)

    yield

    pipeline_reaper_task.cancel()
    stall_sweep_task.cancel()
    heartbeat_task.cancel()
    trading_holiday_task.cancel()
    backup_gauge_task.cancel()
    for task in recovery_tasks:
        task.cancel()
    market_data_scheduler.shutdown(wait=False)
    paper_trading_scheduler.shutdown(wait=False)
    live_trading_scheduler.shutdown(wait=False)
    audit_scheduler.shutdown(wait=False)
    backup_scheduler.shutdown(wait=False)
    risk_monitor_scheduler.shutdown(wait=False)
    shadow_mode_scheduler.shutdown(wait=False)
    engine_alert_scheduler.shutdown(wait=False)
    notification_scheduler.shutdown(wait=False)
    screener_scheduler.shutdown(wait=False)
    post_trade_review_scheduler.shutdown(wait=False)
    investor_reporting_scheduler.shutdown(wait=False)
    unregister_all()
    watcher.stop()


# /docs, /redoc and /openapi.json describe every route to anyone who can reach the port, so they
# exist only when API_DOCS_ENABLED is set. (`app.openapi()`, which the test suite and the wiring
# guard use, still works.)
app = FastAPI(
    title="TradingOS 2.0 API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

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
    # Readable by the console (a browser hides other headers from a cross-origin page): how long to
    # wait after a 429 (the rate limiter sets the same on its own responses), and how many rows a
    # paged list matches in all.
    expose_headers=["Retry-After", TOTAL_COUNT_HEADER],
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
# Outermost, so even a response built by a middleware inside it (the rate limiter's 429) carries
# them.
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(health_router)
app.include_router(api_router)
