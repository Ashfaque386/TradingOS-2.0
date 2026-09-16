"""Autonomous scheduling for the Paper Trading Engine (Build Spec §11):
APScheduler drives both layers without any manual trigger. Layer 1 runs
once a day on an IST cron; Layer 2 drains each active subscription's Redis
tick stream on a short interval, only acting while
`src.engine.paper_trading.market_hours.is_market_open_ist()` is true --
outside market hours the job is a fast no-op, not skipped or cancelled, so
"runs continuously during market hours" is literally true even when
there's nothing to do at 2am.

A mock tick-publish job stands in for Phase 8's real broker-quote poller
(`src.engine.paper_trading.tick_feed.MockTickSource`) -- also market-
hours-gated, since a mock feed advancing prices outside real trading
hours would be dishonest simulation, not extra test coverage.

No job here, or anywhere in `src.orchestration.paper_trading`, ever waits
on a human: the daily job persists signals straight away, and the drain
job acts on ticks straight away via `src.orchestration.risk_gate`'s fully
automatic checks. That is what "never requires a human click for a paper
order" means operationally.
"""

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
from src.engine.paper_trading.tick_feed import TickSource, publish_mock_ticks_once, read_new_ticks
from src.engine.risk.compliance import RegulatoryDataProvider
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_trading import process_tick, run_daily_signal_generation

logger = structlog.get_logger(__name__)

DAILY_SIGNAL_HOUR_IST = 8  # before the 09:15 IST market open
DAILY_SIGNAL_MINUTE_IST = 0
TICK_DRAIN_INTERVAL_SECONDS = 5
MOCK_TICK_PUBLISH_INTERVAL_SECONDS = 3

DAILY_SIGNAL_JOB_ID = "paper_trading_daily_signal"
MOCK_TICK_PUBLISH_JOB_ID = "paper_trading_mock_tick_publish"
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


async def run_mock_tick_publish_job(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, tick_source: TickSource
) -> None:
    if not is_market_open_ist():
        return
    symbols = await _active_symbols(session_factory)
    if symbols:
        await publish_mock_ticks_once(redis, tick_source, symbols)


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
        run_daily_signal_job,
        CronTrigger(hour=DAILY_SIGNAL_HOUR_IST, minute=DAILY_SIGNAL_MINUTE_IST, timezone=IST),
        args=[session_factory, price_provider],
        id=DAILY_SIGNAL_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_mock_tick_publish_job,
        "interval",
        seconds=MOCK_TICK_PUBLISH_INTERVAL_SECONDS,
        args=[session_factory, redis, tick_source],
        id=MOCK_TICK_PUBLISH_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_tick_drain_job,
        "interval",
        seconds=TICK_DRAIN_INTERVAL_SECONDS,
        args=[session_factory, redis, order_book_provider, regulatory_provider],
        id=TICK_DRAIN_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
