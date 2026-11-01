"""Autonomous scheduling for the Paper Trading Engine (Build Spec §11):
APScheduler drives both layers without any manual trigger. Layer 1 runs
once a day on an IST cron; Layer 2 drains each active subscription's Redis
tick stream on a short interval, only acting while
`src.engine.paper_trading.market_hours.is_market_open_ist()` is true --
outside market hours the job is a fast no-op, not skipped or cancelled, so
"runs continuously during market hours" is literally true even when
there's nothing to do at 2am.

The tick-publish job's behavior depends entirely on which `TickSource`
`tick_source_factory` (`src.brokers.tick_source.build_tick_source`, Build
Spec §13) resolves to: a `BrokerQuoteTickSource` polling real broker
quotes when credentials are configured, or the Phase 7 `MockTickSource`
fallback otherwise -- this job itself has no opinion on which. It is
market-hours-gated either way, since a mock feed advancing prices (or a
real feed polling a broker) outside real trading hours would be
dishonest simulation or wasted API calls, not extra test coverage.

`tick_source_factory` is called fresh on every firing (Phase 17 Part 4
real-world testing fix), not resolved once at startup and reused forever
-- the earlier design captured a single `TickSource` when the scheduler
was built, so a broker connected (or disconnected) later via Settings
only took effect after a backend restart, the same class of staleness
Phase 17's LLM-router fix ("the router singleton never saw keys saved in
Settings... it now re-reads... on each call") already closed in a sibling
subsystem. `build_configured_adapter` (what `build_tick_source` calls)
is cheap to call repeatedly -- it only reads the secrets store and
constructs a lightweight adapter object; the real per-broker
`BrokerCircuitBreaker` it wraps comes from `breaker_registry`'s shared
registry, not a fresh instance per call, so re-resolving every 3 seconds
never loses circuit-breaker state.

No job here, or anywhere in `src.orchestration.paper_trading`, ever waits
on a human: the daily job persists signals straight away, and the drain
job acts on ticks straight away via `src.orchestration.risk_gate`'s fully
automatic checks. That is what "never requires a human click for a paper
order" means operationally.
"""

import time
import uuid
from collections.abc import Callable, Sequence

import pandas as pd
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.brokers.base import BrokerAdapter
from src.core.config import get_settings
from src.data.market_data_factory import connected_adapters
from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.paper_trading.order_book import OrderBookProvider
from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.paper_trading.tick_feed import (
    SymbolBackoff,
    TickSource,
    publish_ticks_once,
    read_new_ticks,
)
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.latency_guard import WebSocketLatencyGuard
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.observability.correlation import with_job_correlation_id
from src.observability.metrics import (
    paper_tick_age_seconds,
    paper_tick_to_decision_seconds,
    paper_ticks_skipped_stale_total,
)
from src.orchestration.engine_alerts import (
    AlertCandidate,
    record_event_safely,
    sync_conditions_safely,
)
from src.orchestration.paper_futures import RollOutcome, run_futures_roll
from src.orchestration.paper_options import (
    OptionContractsFn,
    SweepOutcome,
    evaluate_option_subscription,
    option_contracts_from,
    option_quote_symbols,
    run_expiry_sweep,
)
from src.orchestration.paper_trading import process_tick, run_daily_signal_generation

logger = structlog.get_logger(__name__)

# Module-level: one guard shared across every drain-job tick, matching Build Spec §8's framing of a
# single feed-health signal, not a per-symbol one. Its threshold is `PAPER_TICK_MAX_AGE_SECONDS`,
# set on every run: the guard's own 100 ms default is a WebSocket-relay bound, and applied to
# polled ticks (fetched every 3 s, drained every 5 s) it discarded nearly all of them.
_latency_guard = WebSocketLatencyGuard()

# One back-off table for the one tick-publish job: a symbol the broker cannot quote
# is not asked about every 3 seconds (see `SymbolBackoff`).
_symbol_backoff = SymbolBackoff()

DAILY_SIGNAL_HOUR_IST = 8  # before the 09:15 IST market open
DAILY_SIGNAL_MINUTE_IST = 0
TICK_DRAIN_INTERVAL_SECONDS = 5
TICK_PUBLISH_INTERVAL_SECONDS = 3

DAILY_SIGNAL_JOB_ID = "paper_trading_daily_signal"
TICK_PUBLISH_JOB_ID = "paper_trading_tick_publish"
TICK_DRAIN_JOB_ID = "paper_trading_tick_drain"
FUTURES_ROLL_JOB_ID = "paper_trading_futures_roll"
FUTURES_ROLL_INTERVAL_SECONDS = 600
EXPIRY_SWEEP_JOB_ID = "paper_trading_expiry_sweep"
EXPIRY_SWEEP_INTERVAL_SECONDS = 600


ROLL_ALERT_PREFIX = "roll:"
EXPIRY_ALERT_PREFIX = "expiry:"


def roll_alerts(outcomes: Sequence[RollOutcome]) -> list[AlertCandidate]:
    """A futures roll that could not be done is a position about to run past its contract: one
    alert while it stays undone (the job retries every ten minutes), cleared once it goes through.
    """
    alerts = []
    for outcome in outcomes:
        if outcome.status not in ("postponed", "no_contract"):
            continue
        contract = outcome.old_contract or "the futures contract"
        postponed = outcome.status == "postponed"
        alerts.append(
            AlertCandidate(
                key=f"{ROLL_ALERT_PREFIX}{outcome.subscription_id}",
                kind="roll_postponed" if postponed else "roll_no_contract",
                severity="warning",
                title=(
                    f"Roll of {contract} postponed"
                    if postponed
                    else f"No contract to roll {contract} into"
                ),
                detail=outcome.detail or "It will be tried again.",
                subscription_id=uuid.UUID(outcome.subscription_id),
            )
        )
    return alerts


def expiry_alerts(outcomes: Sequence[SweepOutcome]) -> list[AlertCandidate]:
    """An option that could not be squared off before expiry (postponed), or a position left open
    past its expiry with no price to close it at (critical: nothing can value or close it)."""
    alerts = []
    for outcome in outcomes:
        if outcome.status == "closed":
            continue
        no_price = outcome.status == "no_price"
        alerts.append(
            AlertCandidate(
                key=f"{EXPIRY_ALERT_PREFIX}{outcome.subscription_id}:{outcome.symbol}",
                kind="expiry_no_price" if no_price else "expiry_postponed",
                severity="critical" if no_price else "warning",
                title=(
                    f"{outcome.symbol} expired with no price to close it at"
                    if no_price
                    else f"Square-off of {outcome.symbol} before expiry postponed"
                ),
                detail=outcome.detail or "It will be tried again.",
                subscription_id=uuid.UUID(outcome.subscription_id),
            )
        )
    return alerts


def forced_close_alert(outcome: SweepOutcome) -> AlertCandidate:
    """A position that was closed only after its contract had expired, at the last quote seen."""
    return AlertCandidate(
        key=f"expiry_forced_close:{outcome.subscription_id}:{outcome.symbol}:{uuid.uuid4()}",
        kind="expiry_forced_close",
        severity="warning",
        title=f"{outcome.symbol} was closed after its expiry",
        detail=f"It was still open when the contract expired, so it was closed {outcome.detail}.",
        subscription_id=uuid.UUID(outcome.subscription_id),
    )


async def _active_symbols(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis
) -> list[str]:
    async with session_factory() as db:
        result = await db.execute(
            select(PaperTradingSubscription).where(PaperTradingSubscription.is_active.is_(True))
        )
        # What is quoted: the equity itself, a future's current contract, an option strategy's
        # underlying, and the option legs it holds or has just chosen.
        symbols = {sub.trading_symbol for sub in result.scalars().all()}
        symbols |= await option_quote_symbols(db, redis)
        return sorted(symbols)


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
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    tick_source_factory: Callable[[], TickSource],
) -> None:
    if not is_market_open_ist():
        return
    symbols = await _active_symbols(session_factory, redis)
    if symbols:
        settings = get_settings()
        await publish_ticks_once(
            redis,
            tick_source_factory(),
            symbols,
            backoff=_symbol_backoff,
            maxlen=settings.paper_tick_stream_maxlen,
            depth_ttl_seconds=settings.paper_depth_max_age_seconds * 2,
        )


async def run_tick_drain_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    option_contracts: OptionContractsFn | None = None,
) -> None:
    if not is_market_open_ist():
        return

    max_tick_age_ms = get_settings().paper_tick_max_age_seconds * 1000.0
    async with session_factory() as db:
        result = await db.execute(
            select(PaperTradingSubscription).where(PaperTradingSubscription.is_active.is_(True))
        )
        subscriptions = result.scalars().all()

    for subscription in subscriptions:
        ticks, new_cursor = await read_new_ticks(
            redis, subscription.trading_symbol, last_id=subscription.tick_cursor
        )
        for tick in ticks:
            latency_ms = max(0.0, time.time() * 1000 - tick.timestamp_ms)
            paper_tick_age_seconds.observe(latency_ms / 1000.0)
            _latency_guard.threshold_ms = max_tick_age_ms
            observation = _latency_guard.observe(latency_ms)
            if observation.paused:
                paper_ticks_skipped_stale_total.inc()
                logger.warning(
                    "paper_trading.tick_skipped_stale",
                    subscription_id=str(subscription.id),
                    age_ms=latency_ms,
                    max_age_ms=observation.threshold_ms,
                )
                continue

            async with session_factory() as db:
                try:
                    if subscription.is_option:
                        await evaluate_option_subscription(
                            db,
                            redis,
                            subscription.id,
                            spot=tick.price,
                            tick_source=tick.source,
                            contracts=option_contracts,
                            order_book_provider=order_book_provider,
                            regulatory_provider=regulatory_provider,
                        )
                    else:
                        await process_tick(
                            db,
                            subscription_id=subscription.id,
                            tick_price=tick.price,
                            tick_source=tick.source,
                            order_book_provider=order_book_provider,
                            regulatory_provider=regulatory_provider,
                        )
                except Exception:  # noqa: BLE001 - one bad tick must never kill the drain job
                    logger.exception(
                        "paper_trading.tick_processing_failed",
                        subscription_id=str(subscription.id),
                    )
                else:
                    paper_tick_to_decision_seconds.observe(
                        max(0.0, time.time() * 1000 - tick.timestamp_ms) / 1000.0
                    )

        if new_cursor != subscription.tick_cursor:
            async with session_factory() as db:
                sub_row = await db.get(PaperTradingSubscription, subscription.id)
                if sub_row is not None:
                    sub_row.tick_cursor = new_cursor
                    await db.commit()


async def run_futures_roll_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    adapters_factory: Callable[[], Sequence[BrokerAdapter]],
) -> None:
    """Rolls futures subscriptions that are within `PAPER_FUTURES_ROLL_DAYS` of expiry. Only while
    the market is open: closing the old position needs a live tick and book, and a roll that
    cannot be done now is simply tried again at the next run."""
    if not is_market_open_ist():
        return
    async with session_factory() as db:
        outcomes = await run_futures_roll(
            db,
            redis,
            adapters=adapters_factory(),
            order_book_provider=order_book_provider,
            regulatory_provider=regulatory_provider,
            roll_days=get_settings().paper_futures_roll_days,
        )
    for outcome in outcomes:
        logger.info(
            "paper_trading.futures_roll_outcome",
            subscription_id=outcome.subscription_id,
            status=outcome.status,
            old_contract=outcome.old_contract,
            new_contract=outcome.new_contract,
            detail=outcome.detail,
        )
    async with session_factory() as db:
        await sync_conditions_safely(
            db, source="event", prefix=ROLL_ALERT_PREFIX, present=roll_alerts(outcomes)
        )


async def run_expiry_sweep_job(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
) -> None:
    """Squares off option positions the day before expiry and cleans up any derivative position
    still open after its expiry (see `paper_options.run_expiry_sweep`). Market hours only: the
    normal close needs a live tick and book, and one that cannot be done now is tried at the
    next run."""
    if not is_market_open_ist():
        return
    async with session_factory() as db:
        outcomes = await run_expiry_sweep(
            db,
            redis,
            order_book_provider=order_book_provider,
            regulatory_provider=regulatory_provider,
            squareoff_days=get_settings().paper_options_squareoff_days,
        )
    for outcome in outcomes:
        logger.info(
            "paper_trading.expiry_sweep_outcome",
            subscription_id=outcome.subscription_id,
            symbol=outcome.symbol,
            status=outcome.status,
            detail=outcome.detail,
        )
    async with session_factory() as db:
        await sync_conditions_safely(
            db, source="event", prefix=EXPIRY_ALERT_PREFIX, present=expiry_alerts(outcomes)
        )
        for outcome in outcomes:
            if outcome.forced:
                await record_event_safely(db, forced_close_alert(outcome))


def start_paper_trading_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    redis: Redis,
    price_provider: PriceDataProvider,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    tick_source_factory: Callable[[], TickSource],
    futures_adapters_factory: Callable[[], Sequence[BrokerAdapter]] | None = None,
    option_contracts: OptionContractsFn | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    adapters_factory = futures_adapters_factory or connected_adapters
    contracts_source = option_contracts or option_contracts_from(adapters_factory)

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
        args=[session_factory, redis, tick_source_factory],
        id=TICK_PUBLISH_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(TICK_DRAIN_JOB_ID, run_tick_drain_job),
        "interval",
        seconds=TICK_DRAIN_INTERVAL_SECONDS,
        args=[session_factory, redis, order_book_provider, regulatory_provider, contracts_source],
        id=TICK_DRAIN_JOB_ID,
        replace_existing=True,
    )

    scheduler.add_job(
        with_job_correlation_id(FUTURES_ROLL_JOB_ID, run_futures_roll_job),
        "interval",
        seconds=FUTURES_ROLL_INTERVAL_SECONDS,
        args=[session_factory, redis, order_book_provider, regulatory_provider, adapters_factory],
        id=FUTURES_ROLL_JOB_ID,
        replace_existing=True,
    )

    scheduler.add_job(
        with_job_correlation_id(EXPIRY_SWEEP_JOB_ID, run_expiry_sweep_job),
        "interval",
        seconds=EXPIRY_SWEEP_INTERVAL_SECONDS,
        args=[session_factory, redis, order_book_provider, regulatory_provider],
        id=EXPIRY_SWEEP_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
