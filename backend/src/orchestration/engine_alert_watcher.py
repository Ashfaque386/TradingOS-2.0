"""The scheduled look that turns the Paper Desk's attention rules into engine alerts.

Every minute it reads the same subscription list the console does
(`list_subscription_views`), runs the rules (`src.orchestration.paper_attention`, the Python
twin of `attentionItems`), adds what the brokers have said about the stored credentials, and hands
the result to `engine_alerts.sync_watched`, which opens, announces and resolves episodes.

What it speaks for depends on the clock. A position nobody is watching is critical whatever time it
is, so those are judged around the clock. A warning -- a feed that has stopped, a position with no
price, a price that has gone stale -- only means something while the market is open, so outside it
those episodes are left exactly as they are: not opened, and not "cleared" at the close only to
reopen at the next open.

Information-level items (a subscription waiting for price history) are shown on the Paper Desk but
are not worth a message, so they are not alerts.

It runs in its own scheduler, not in the heartbeat loop (src.agents.heartbeat), whose isolation
from the trading code is pinned by a test.
"""

import time
import uuid
from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.brokers import feed_health
from src.core.config import get_settings
from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.paper_trading.price_data import PriceDataProvider
from src.models.engine_alert_event import EngineAlertEvent
from src.observability.correlation import with_job_correlation_id
from src.observability.metrics import (
    engine_alert_watch_last_success_timestamp_seconds,
    engine_alerts_open,
)
from src.orchestration.engine_alerts import (
    SEVERITY_RANK,
    AlertCandidate,
    WatchResult,
    sync_watched,
)
from src.orchestration.paper_attention import (
    AttentionItem,
    attention_items,
    subject_from_view,
)
from src.orchestration.paper_subscription_controls import list_subscription_views

logger = structlog.get_logger(__name__)

WATCH_INTERVAL_SECONDS = 60
WATCH_JOB_ID = "engine_alert_watch"

BROKER_KEY_PREFIX = "broker:"


def candidate_from_item(item: AttentionItem) -> AlertCandidate:
    return AlertCandidate(
        key=item.id,
        kind=item.kind,
        severity=item.severity,
        title=item.title,
        detail=item.detail,
        subscription_id=uuid.UUID(item.subscription_id),
    )


def broker_rejection_candidates() -> list[AlertCandidate]:
    """One critical alert per broker whose stored credentials are being refused. It lasts as long
    as the refusal does, and the registry clears it the moment a call succeeds again or the
    credentials change."""
    candidates = []
    for broker, rejection in sorted(feed_health.all_rejections().items()):
        candidates.append(
            AlertCandidate(
                key=f"{BROKER_KEY_PREFIX}{broker}:rejected",
                kind="broker_rejected",
                severity="critical",
                title=f"{broker} rejected the stored credentials",
                detail=(
                    f"HTTP {rejection.status_code} since {rejection.at.astimezone(IST):%H:%M} IST. "
                    "No prices are coming from it. Reconnect the broker in Settings > Broker."
                ),
            )
        )
    return candidates


async def refresh_gauges(db: AsyncSession) -> None:
    """The open count by severity, across every source, for Prometheus and Grafana."""
    counts: dict[str, int] = {
        severity: count
        for severity, count in (
            await db.execute(
                select(EngineAlertEvent.severity, func.count())
                .where(EngineAlertEvent.resolved_at.is_(None))
                .group_by(EngineAlertEvent.severity)
            )
        ).tuples()
    }
    for severity in SEVERITY_RANK:
        engine_alerts_open.labels(severity=severity).set(counts.get(severity, 0))


async def run_engine_alert_watch(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    price_provider: PriceDataProvider,
    *,
    now: datetime | None = None,
) -> WatchResult:
    now = now or datetime.now(UTC)
    settings = get_settings()
    market_open = is_market_open_ist(now)
    async with session_factory() as db:
        views = await list_subscription_views(db, redis, price_provider, now=now)
        items = attention_items(
            [subject_from_view(view) for view in views],
            market_open=market_open,
            stale_after_seconds=settings.paper_stale_price_seconds,
        )
        candidates = [candidate_from_item(item) for item in items if item.severity != "info"]
        candidates += broker_rejection_candidates()
        result = await sync_watched(
            db,
            candidates,
            scope=frozenset({"critical", "warning"}) if market_open else frozenset({"critical"}),
            confirm_seconds={"critical": 0.0, "warning": settings.engine_alert_confirm_seconds},
            now=now,
        )
        await refresh_gauges(db)
    engine_alert_watch_last_success_timestamp_seconds.set(time.time())
    if result.opened or result.resolved:
        logger.info(
            "engine_alerts.watch",
            opened=result.opened,
            announced=result.announced,
            resolved=result.resolved,
        )
    return result


def start_engine_alert_watcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    redis: Redis,
    price_provider: PriceDataProvider,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(WATCH_JOB_ID, run_engine_alert_watch),
        "interval",
        seconds=WATCH_INTERVAL_SECONDS,
        args=[session_factory, redis, price_provider],
        id=WATCH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
