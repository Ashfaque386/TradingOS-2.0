"""The daily Shadow Mode dry run (Phase 4): once each NSE trading day, every strategy in paper
trading gets one dry-run order check against a connected broker's sandbox, and the result is
recorded for that strategy.

This is what makes the Go-Live gate's "clean Shadow Mode streak" something that builds up by
itself: a streak counts consecutive trading days with a dry run that worked, so something has to
try each day. With no broker connected nothing is tried, no run is recorded, and the evidence says
so (a day nobody tried is not a clean day). A check that fails is recorded as a failed run and ends
the streak.

The check is the one the console's "Run dry check" button makes: a one-share market buy of the
strategy's symbol, sent to the broker's sandbox where there is one and only built, never sent, where
there is not (src.orchestration.shadow_mode). It cannot reach production.
"""

import uuid
from datetime import UTC, datetime, time

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.brokers.base import OrderRequest, OrderSide, OrderType
from src.brokers.factory import KNOWN_BROKERS, build_broker_adapter
from src.data.nse_calendar import is_nse_trading_day
from src.engine.paper_trading.market_hours import IST
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeRun
from src.models.strategy import Strategy, StrategyStatus
from src.models.strategy_version import StrategyVersion
from src.observability.correlation import with_job_correlation_id
from src.orchestration.shadow_mode import run_shadow_order_check
from src.security.secrets_store import SecretsStore, SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)

DRY_RUN_HOUR_IST = 9
DRY_RUN_MINUTE_IST = 5
DRY_RUN_JOB_ID = "shadow_mode_daily_dry_run"


def _connected_broker(store: SecretsStore) -> str | None:
    for broker in KNOWN_BROKERS:
        if store.get_credentials(broker) is not None:
            return broker
    return None


async def run_daily_dry_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    store: SecretsStore | None = None,
    now: datetime | None = None,
) -> int:
    """One dry run per paper-trading strategy that has not had one today; how many were made."""
    now = now or datetime.now(UTC)
    today = now.astimezone(IST).date()
    if not is_nse_trading_day(today):
        return 0
    if store is None:
        try:
            store = get_secrets_store()
        except SecretsStoreError:
            logger.info("shadow_mode.daily_dry_run_skipped", reason="no secrets store configured")
            return 0
    broker = _connected_broker(store)
    credentials = store.get_credentials(broker) if broker else None
    if broker is None or credentials is None:
        logger.info("shadow_mode.daily_dry_run_skipped", reason="no broker connected")
        return 0

    start_of_day = datetime.combine(today, time.min, tzinfo=IST)
    made = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Strategy.id, PaperTradingSubscription.symbol)
                .join(StrategyVersion, StrategyVersion.strategy_id == Strategy.id)
                .join(
                    PaperTradingSubscription,
                    PaperTradingSubscription.strategy_version_id == StrategyVersion.id,
                )
                .where(
                    Strategy.status == StrategyStatus.PAPER_TRADING.value,
                    PaperTradingSubscription.is_active.is_(True),
                )
                .order_by(PaperTradingSubscription.created_at)
            )
        ).all()
        done_today: set[uuid.UUID] = {
            sid
            for sid in (
                await db.execute(
                    select(ShadowModeRun.strategy_id).where(
                        ShadowModeRun.strategy_id.is_not(None),
                        ShadowModeRun.created_at >= start_of_day,
                    )
                )
            )
            .scalars()
            .all()
            if sid is not None
        }
        for strategy_id, symbol in rows:
            if strategy_id in done_today:
                continue
            done_today.add(strategy_id)
            try:
                await run_shadow_order_check(
                    db,
                    adapter=build_broker_adapter(broker, credentials, sandbox=True),
                    order=OrderRequest(
                        symbol=symbol, side=OrderSide.BUY, quantity=1, order_type=OrderType.MARKET
                    ),
                    strategy_id=strategy_id,
                )
            except Exception:  # noqa: BLE001 - one strategy's check must not stop the others
                logger.exception("shadow_mode.daily_dry_run_failed", strategy_id=str(strategy_id))
                await db.rollback()
                continue
            made += 1
    return made


def start_shadow_mode_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        with_job_correlation_id(DRY_RUN_JOB_ID, run_daily_dry_runs),
        CronTrigger(
            day_of_week="mon-fri", hour=DRY_RUN_HOUR_IST, minute=DRY_RUN_MINUTE_IST, timezone=IST
        ),
        args=[session_factory],
        id=DRY_RUN_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
