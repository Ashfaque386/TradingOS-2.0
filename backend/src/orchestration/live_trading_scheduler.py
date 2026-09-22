"""Autonomous scheduling for the LiveExecutionPipeline (Build Spec §12):
APScheduler drives intent generation without any manual trigger, mirroring
Phase 7's paper-trading scheduler shape exactly -- a daily IST cron for
Layer 1, a short interval job for Layer 2's tick-driven intent generation.
The one addition Phase 7 didn't need: a dedicated expiry-sweep job, since
paper trading never has anything sitting in a human-review queue that
could go stale.

The expiry sweep is deliberately NOT gated by `is_market_open_ist()`,
unlike the other two jobs: an intent generated right before market close
must still expire correctly after close if nobody acts on it, and Build
Spec §12.2 requires this to be "correct even if no one is looking at the
UI" -- gating it by market hours would silently stop resolving stale
intents the moment the market closes, which is exactly the failure mode
this sweep exists to prevent.

The intent-generation job reads from the *same* Redis tick stream Phase
7/8's tick-publish job already produces per symbol
(`src.engine.paper_trading.tick_feed`) -- a symbol's live quote is the
same value regardless of paper or live mode, so there is no separate
live-specific tick-publishing job here; this scheduler only drains.
"""

import pandas as pd
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.brokers.base import BrokerAdapter
from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.paper_trading.tick_feed import read_new_ticks
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.live_trading_subscription import LiveTradingSubscription
from src.observability.correlation import with_job_correlation_id
from src.orchestration.live_trading import (
    expire_stale_intents,
    generate_live_order_intent,
    reconcile_pending_trades,
    run_live_daily_signal_generation,
)

logger = structlog.get_logger(__name__)

DAILY_SIGNAL_HOUR_IST = 8  # before the 09:15 IST market open
DAILY_SIGNAL_MINUTE_IST = 0
INTENT_GENERATION_INTERVAL_SECONDS = 5
EXPIRY_SWEEP_INTERVAL_SECONDS = 10
RECONCILIATION_INTERVAL_SECONDS = 15

DAILY_SIGNAL_JOB_ID = "live_trading_daily_signal"
INTENT_GENERATION_JOB_ID = "live_trading_intent_generation"
EXPIRY_SWEEP_JOB_ID = "live_trading_expiry_sweep"
RECONCILIATION_JOB_ID = "live_trading_reconciliation"


async def run_live_daily_signal_job(
    session_factory: async_sessionmaker[AsyncSession], price_provider: PriceDataProvider
) -> None:
    as_of = pd.Timestamp.now(tz=IST).normalize().tz_localize(None)
    async with session_factory() as db:
        updated = await run_live_daily_signal_generation(
            db, price_provider=price_provider, as_of=as_of
        )
    if updated:
        logger.info("live_trading.daily_signals_generated", count=len(updated))


async def run_intent_generation_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    *,
    adapter: BrokerAdapter | None,
    regulatory_provider: RegulatoryDataProvider,
) -> None:
    if not is_market_open_ist():
        return

    async with session_factory() as db:
        result = await db.execute(
            select(LiveTradingSubscription).where(LiveTradingSubscription.is_active.is_(True))
        )
        subscriptions = result.scalars().all()

    for subscription in subscriptions:
        ticks, new_cursor = await read_new_ticks(
            redis, subscription.symbol, last_id=subscription.tick_cursor
        )
        for tick in ticks:
            async with session_factory() as db:
                try:
                    await generate_live_order_intent(
                        db,
                        subscription_id=subscription.id,
                        tick_price=tick.price,
                        adapter=adapter,
                        regulatory_provider=regulatory_provider,
                    )
                except KillSwitchTrippedError:
                    # Build Spec §12.2: a tripped switch must stop
                    # generation entirely -- logged once per tick this
                    # happens on, never raised further (an uncaught
                    # exception here would kill this job's drain loop
                    # for every other subscription too).
                    logger.warning(
                        "live_trading.generation_blocked_by_kill_switch",
                        subscription_id=str(subscription.id),
                    )
                except Exception:  # noqa: BLE001 - one bad tick must never kill the drain job
                    logger.exception(
                        "live_trading.intent_generation_failed",
                        subscription_id=str(subscription.id),
                    )

        if new_cursor != subscription.tick_cursor:
            async with session_factory() as db:
                sub_row = await db.get(LiveTradingSubscription, subscription.id)
                if sub_row is not None:
                    sub_row.tick_cursor = new_cursor
                    await db.commit()


async def run_expiry_sweep_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        expired_count = await expire_stale_intents(db)
    if expired_count:
        logger.info("live_trading.intents_expired", count=expired_count)


async def run_reconciliation_job(
    session_factory: async_sessionmaker[AsyncSession], *, adapter: BrokerAdapter | None
) -> None:
    if adapter is None:
        return
    async with session_factory() as db:
        try:
            reconciled_count = await reconcile_pending_trades(db, adapter)
        except Exception:  # noqa: BLE001 - one bad reconciliation pass must not kill the job's next tick
            logger.exception("live_trading.reconciliation_failed")
            return
    if reconciled_count:
        logger.info("live_trading.trades_reconciled", count=reconciled_count)


def start_live_trading_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    redis: Redis,
    price_provider: PriceDataProvider,
    regulatory_provider: RegulatoryDataProvider,
    adapter: BrokerAdapter | None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    scheduler.add_job(
        with_job_correlation_id(DAILY_SIGNAL_JOB_ID, run_live_daily_signal_job),
        CronTrigger(hour=DAILY_SIGNAL_HOUR_IST, minute=DAILY_SIGNAL_MINUTE_IST, timezone=IST),
        args=[session_factory, price_provider],
        id=DAILY_SIGNAL_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(INTENT_GENERATION_JOB_ID, run_intent_generation_job),
        "interval",
        seconds=INTENT_GENERATION_INTERVAL_SECONDS,
        args=[session_factory, redis],
        kwargs={"adapter": adapter, "regulatory_provider": regulatory_provider},
        id=INTENT_GENERATION_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(EXPIRY_SWEEP_JOB_ID, run_expiry_sweep_job),
        "interval",
        seconds=EXPIRY_SWEEP_INTERVAL_SECONDS,
        args=[session_factory],
        id=EXPIRY_SWEEP_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(RECONCILIATION_JOB_ID, run_reconciliation_job),
        "interval",
        seconds=RECONCILIATION_INTERVAL_SECONDS,
        args=[session_factory],
        kwargs={"adapter": adapter},
        id=RECONCILIATION_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
