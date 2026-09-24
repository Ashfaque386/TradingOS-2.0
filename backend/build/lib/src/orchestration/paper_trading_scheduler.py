"""Autonomous scheduling for the Paper Trading Engine (Build Spec §11):
APScheduler drives both layers without any manual trigger. Layer 1 runs
once a day on an IST cron; Layer 2 drains each active subscription's Redis
tick stream on a short interval, only acting while
`src.engine.paper_trading.market_hours.is_market_open_ist()` is true --
outside market hours the job is a fast no-op, not skipped or cancelled, so
"runs continuously during market hours" is literally true even when
there's nothing to do at 2am.

The tick-publish job's behavior depends entirely on which `TickSource` it
is constructed with (`src.brokers.tick_source.build_tick_source`, Build
Spec §13): a `BrokerQuoteTickSource` polling real broker quotes when
credentials are configured, or the Phase 7 `MockTickSource` fallback
otherwise -- this job itself has no opinion on which. It is
market-hours-gated either way, since a mock feed advancing prices (or a
real feed polling a broker) outside real trading hours would be
dishonest simulation or wasted API calls, not extra test coverage.

No job here, or anywhere in `src.orchestration.paper_trading`, ever waits
on a human: the daily job persists signals straight away, and the drain
job acts on ticks straight away via `src.orchestration.risk_gate`'s fully
automatic checks. That is what "never requires a human click for a paper
order" means operationally.
"""

import time

import pandas as pd
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.paper_trading.order_book import OrderBookProvider
from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.paper_trading.tick_feed import TickSource, publish_ticks_once, read_new_ticks
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.latency_guard import WebSocketLatencyGuard
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.observability.correlation import with_job_correlation_id
from src.observability.metrics import ws_latency_seconds
from src.orchestration.paper_trading import process_tick, run_daily_signal_generation

logger = structlog.get_logger(__name__)

# Module-level: one guard shared across every drain-job tick, matching
# Build Spec §8's own framing of a single tick-relay health signal, not a
# per-symbol one -- Phase 6 built this class but never actually wired it
# to a real tick stream; this is that wiring, finally landing.
_latency_guard = WebSocketLatencyGuard()

DAILY_SIGNAL_HOUR_IST = 8  # before the 09:15 IST market open
DAILY_SIGNAL_MINUTE_IST = 0
TICK_DRAIN_INTERVAL_SECONDS = 5
TICK_PUBLISH_INTERVAL_SECONDS = 3

DAILY_SIGNAL_JOB_ID = "paper_trading_daily_signal"
TICK_PUBLISH_JOB_ID = "paper_trading_tick_publish"
TICK_DRAIN_JOB_ID = "paper_trading_tick_drain"


async def _active_symbols(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with session_factory() as db:
        result = await db.execute(
            select(PaperTradingSubscription.symbol)
            .where(PaperTradingSubscription.is_active.is_(True))
            .distinct()
        )
        return [row[0] for row in result.all()]


async def run_daily_signal_job(
    session_factory: async_sessionmaker[AsyncSession], price_provider: PriceDataProvider
) -> None:
    # Timezone-naive on purpose: only today's IST calendar date matters
    # here, and PriceDataProvider implementations (e.g.
    # FakeDailyPriceProvider) index by naive dates -- a tz-aware Timestamp
    # would fail to compare against that index at all.
    as_of = pd.Timestamp.now(tz=IST).normalize().tz_localize(None)
    async with session_factory() as db:
        signals = await run_daily_signal_generation(db, price_provider=price_provider, as_of=as_of)
    if signals:
        logger.info("paper_trading.daily_signals_generated", count=len(signals))


async def run_tick_publish_job(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, tick_source: TickSource
) -> None:
    if not is_market_open_ist():
        return
    symbols = await _active_symbols(session_factory)
    if symbols:
        await publish_ticks_once(redis, tick_source, symbols)


async def run_tick_drain_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
) -> None:
    if not is_market_open_ist():
        return

    async with session_factory() as db:
        result = await db.execute(
            select(PaperTradingSubscription).where(PaperTradingSubscription.is_active.is_(True))
        )
        subscriptions = result.scalars().all()

    for subscription in subscriptions:
        ticks, new_cursor = await read_new_ticks(
            redis, subscription.symbol, last_id=subscription.tick_cursor
        )
        for tick in ticks:
            latency_ms = max(0.0, time.time() * 1000 - tick.timestamp_ms)
            ws_latency_seconds.observe(latency_ms / 1000.0)
            observation = _latency_guard.observe(latency_ms)
            if observation.paused:
                logger.warning(
                    "paper_trading.tick_processing_paused_high_latency",
                    subscription_id=str(subscription.id),
                    latency_ms=latency_ms,
                    threshold_ms=observation.threshold_ms,
                )
                continue

            async with session_factory() as db:
                try:
                    await process_tick(
                        db,
                        subscription_id=subscription.id,
                        tick_price=tick.price,
                        order_book_provider=order_book_provider,
                        regulatory_provider=regulatory_provider,
                    )
                except Exception:  # noqa: BLE001 - one bad tick must never kill the drain job
                    logger.exception(
                        "paper_trading.tick_processing_failed",
                        subscription_id=str(subscription.id),
                    )

        if new_cursor != subscription.tick_cursor:
            async with session_factory() as db:
                sub_row = await db.get(PaperTradingSubscription, subscription.id)
                if sub_row is not None:
                    sub_row.tick_cursor = new_cursor
                    await db.commit()


def start_paper_trading_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    redis: Redis,
    price_provider: PriceDataProvider,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    tick_source: TickSource,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    scheduler.add_job(
        with_job_correlation_id(DAILY_SIGNAL_JOB_ID, run_daily_signal_job),
        CronTrigger(hour=DAILY_SIGNAL_HOUR_IST, minute=DAILY_SIGNAL_MINUTE_IST, timezone=IST),
        args=[session_factory, price_provider],
        id=DAILY_SIGNAL_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(TICK_PUBLISH_JOB_ID, run_tick_publish_job),
        "interval",
        seconds=TICK_PUBLISH_INTERVAL_SECONDS,
        args=[session_factory, redis, tick_source],
        id=TICK_PUBLISH_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(TICK_DRAIN_JOB_ID, run_tick_drain_job),
        "interval",
        seconds=TICK_DRAIN_INTERVAL_SECONDS,
        args=[session_factory, redis, order_book_provider, regulatory_provider],
        id=TICK_DRAIN_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
