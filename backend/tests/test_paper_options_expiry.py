"""The expiry sweep: squaring options off before expiry and cleaning up after it (phase 5).

An option position is squared off `squareoff_days` before its expiry through the normal close (a
live tick, a real book, the risk gate; it can be postponed). One still open after its expiry is
closed at the last real quote seen -- as a cleanup, uncharged. A future whose contract expired
without being rolled is cleaned up the same way (the gap phase 4 documented). Not modelled:
exercise, assignment, physical delivery.
"""

import time
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.brokers.base import FuturesContract, futures_symbol, option_symbol
from src.core.config import get_settings
from src.engine.paper_trading.depth import (
    DepthSnapshot,
    RealDepthOrderBookProvider,
    depth_key,
    publish_depth,
)
from src.engine.paper_trading.order_book import OrderBookLevel
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.audit_log import AuditLog
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_options import SWEEP_ACTOR, run_expiry_sweep
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # Monday 15:30 IST
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


def _opt(day: int, strike=24500.0, kind="CE") -> str:
    return option_symbol("NIFTY", date(2026, 10, day), strike, kind)


# Expiries relative to NOW (the 19th): the 18th is past, the 19th is today, the 20th tomorrow.
PAST, TODAY, TOMORROW, IN_TWO, IN_THREE = (_opt(d) for d in (18, 19, 20, 21, 22))
SHORT_TOMORROW = _opt(20, 24400.0)  # a sold leg that expires tomorrow too
ALL = [PAST, TODAY, TOMORROW, IN_TWO, IN_THREE, SHORT_TOMORROW]
FUT = FuturesContract(
    futures_symbol("NIFTY", date(2026, 10, 18)), "NIFTY", date(2026, 10, 18), 65, 10.0
)


def _ms(offset=0.0):
    return int((NOW.timestamp() + offset) * 1000)


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [k for s in [*ALL, FUT.symbol] for k in (depth_key(s), tick_stream_key(s))]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _subscription(db_session_factory, *, option=True, contract=None, active=True):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"SW-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=1_000_000.0,
            option_strategy="vertical_spread" if option else None,
            contract=contract,
        )
        if not active:
            (await db.get(PaperTradingSubscription, subscription.id)).is_active = False
            await db.commit()
        return subscription


async def _hold(db_session_factory, subscription, symbol, quantity, avg_cost=100.0):
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id,
                symbol=symbol,
                quantity=quantity,
                avg_cost=avg_cost,
            )
        )
        await db.commit()


async def _quote(redis, symbol, price, *, age=0.0, book=True):
    if book:
        await publish_depth(
            redis,
            symbol,
            DepthSnapshot(
                bids=(OrderBookLevel(price - 1.0, 1_000),),
                asks=(OrderBookLevel(price + 1.0, 1_000),),
                timestamp_ms=int(time.time() * 1000),
            ),
            ttl_seconds=60,
        )
    await publish_tick(redis, Tick(symbol, price, _ms(-age), "real"))


async def _sweep(db_session_factory, redis, *, squareoff_days=1):
    async with db_session_factory() as db:
        return await run_expiry_sweep(
            db,
            redis,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            regulatory_provider=_REGULATORY,
            squareoff_days=squareoff_days,
            now=NOW,
        )


async def _position(db_session_factory, subscription, symbol) -> PaperPosition:
    async with db_session_factory() as db:
        return (
            await db.execute(
                select(PaperPosition).where(
                    PaperPosition.subscription_id == subscription.id,
                    PaperPosition.symbol == symbol,
                )
            )
        ).scalar_one()


async def _fills(db_session_factory, subscription) -> list[PaperFill]:
    async with db_session_factory() as db:
        return list(
            (
                await db.execute(
                    select(PaperFill)
                    .where(PaperFill.subscription_id == subscription.id)
                    .order_by(PaperFill.created_at)
                )
            )
            .scalars()
            .all()
        )


# ---- before expiry: a normal close ----


async def test_a_position_expiring_tomorrow_is_squared_off_through_the_normal_close(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TOMORROW, 65)
    await _quote(redis, TOMORROW, 80.0)

    outcomes = await _sweep(db_session_factory, redis)

    assert [(o.symbol, o.status) for o in outcomes] == [(TOMORROW, "closed")]
    assert outcomes[0].forced is False  # squared off on time: nothing to report
    [fill] = await _fills(db_session_factory, subscription)
    assert (fill.side, fill.filled_quantity, fill.avg_fill_price) == ("sell", 65, 79.0)  # the bid
    assert fill.costs_total > 0 and fill.costs_breakdown["segment"] == "option"  # a trade: charged
    assert (await _position(db_session_factory, subscription, TOMORROW)).quantity == 0


async def test_a_position_on_its_expiry_day_is_squared_off_too(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TODAY, 65)
    await _quote(redis, TODAY, 5.0)

    assert [o.status for o in await _sweep(db_session_factory, redis)] == ["closed"]
    [fill] = await _fills(db_session_factory, subscription)
    assert fill.avg_fill_price == 4.0 and fill.costs_total > 0  # walked the book and was charged


async def test_a_position_with_more_days_to_run_is_left_alone(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, IN_TWO, 65)
    await _hold(db_session_factory, subscription, IN_THREE, 65)
    await _quote(redis, IN_TWO, 80.0)
    await _quote(redis, IN_THREE, 80.0)

    assert await _sweep(db_session_factory, redis) == []
    assert await _fills(db_session_factory, subscription) == []


async def test_the_squareoff_window_is_the_setting(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, IN_THREE, 65)
    await _quote(redis, IN_THREE, 80.0)

    assert await _sweep(db_session_factory, redis, squareoff_days=2) == []
    outcomes = await _sweep(db_session_factory, redis, squareoff_days=3)

    assert [o.status for o in outcomes] == ["closed"]


async def test_without_a_fresh_tick_the_squareoff_waits(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TOMORROW, 65)
    await _quote(redis, TOMORROW, 80.0, age=3_600)  # an hour old: the market may be shut

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "postponed" and "old" in outcome.detail
    assert (await _position(db_session_factory, subscription, TOMORROW)).quantity == 65


async def test_without_a_book_the_squareoff_waits(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TOMORROW, 65)
    await _quote(redis, TOMORROW, 80.0, book=False)

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "postponed" and "order book" in outcome.detail
    assert (await _position(db_session_factory, subscription, TOMORROW)).quantity == 65


async def test_a_book_too_thin_to_close_it_all_is_a_postponed_squareoff(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TOMORROW, 65)
    await publish_depth(
        redis,
        TOMORROW,
        DepthSnapshot(
            bids=(OrderBookLevel(79.0, 40),), asks=(), timestamp_ms=int(time.time() * 1000)
        ),
        ttl_seconds=60,
    )
    await publish_tick(redis, Tick(TOMORROW, 80.0, _ms(), "real"))

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "postponed" and "25 left" in outcome.detail
    assert (await _position(db_session_factory, subscription, TOMORROW)).quantity == 25


async def test_a_sold_leg_is_bought_back_first_and_if_it_cannot_be_its_hedge_stays(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, TOMORROW, 65, 20.0)  # the hedge
    await _hold(db_session_factory, subscription, SHORT_TOMORROW, -65, 60.0)  # the sold leg
    await _quote(redis, TOMORROW, 20.0)
    await _quote(redis, SHORT_TOMORROW, 60.0, book=False)  # the sold leg has no book

    outcomes = await _sweep(db_session_factory, redis)

    assert [(o.symbol, o.status) for o in outcomes] == [(SHORT_TOMORROW, "postponed")]
    assert await _fills(db_session_factory, subscription) == []  # the hedge was not sold
    assert (await _position(db_session_factory, subscription, TOMORROW)).quantity == 65

    await _quote(redis, SHORT_TOMORROW, 60.0)  # its book arrives: both close, short leg first
    outcomes = await _sweep(db_session_factory, redis)
    assert [(o.symbol, o.status) for o in outcomes] == [
        (SHORT_TOMORROW, "closed"),
        (TOMORROW, "closed"),
    ]
    assert [f.symbol for f in await _fills(db_session_factory, subscription)] == [
        SHORT_TOMORROW,
        TOMORROW,
    ]


# ---- after expiry: the last real quote ----


async def test_a_position_still_open_after_expiry_is_closed_at_the_last_real_quote(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, PAST, 65, 100.0)
    await publish_tick(redis, Tick(PAST, 12.5, _ms(-3 * 3600), "real"))  # the last one, 3h ago

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "closed" and "12.5" in outcome.detail
    assert outcome.forced is True  # closed only after the contract had expired
    [fill] = await _fills(db_session_factory, subscription)
    assert (fill.side, fill.filled_quantity, fill.avg_fill_price) == ("sell", 65, 12.5)
    assert fill.costs_total == 0.0 and fill.costs_breakdown is None  # a cleanup is not a trade
    assert fill.data_source == "real"  # the quote was a real one
    position = await _position(db_session_factory, subscription, PAST)
    assert position.quantity == 0
    assert position.realized_pnl == pytest.approx((12.5 - 100.0) * 65)


async def test_that_close_is_audited_with_what_it_rested_on(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, PAST, 65)
    await publish_tick(redis, Tick(PAST, 12.5, _ms(-3 * 3600), "real"))

    await _sweep(db_session_factory, redis)

    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "paper_trading.position_closed")
            )
        ).scalar_one()
    assert entry.actor == SWEEP_ACTOR and entry.details["administrative"] is True
    assert entry.details["symbol"] == PAST
    assert entry.details["price_basis"].startswith("last real quote, 3.0h old")
    assert "expired on 2026-10-18" in entry.details["reason"]


async def test_an_expired_position_that_never_had_a_quote_is_reported_not_priced(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, PAST, 65)

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "no_price"
    assert (await _position(db_session_factory, subscription, PAST)).quantity == 65
    assert await _fills(db_session_factory, subscription) == []


async def test_a_short_leg_that_cannot_be_priced_after_expiry_holds_back_its_hedge(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, PAST, 65)
    await _hold(db_session_factory, subscription, _opt(18, 24400.0), -65)  # sold, never quoted
    await publish_tick(redis, Tick(PAST, 12.5, _ms(-3600), "real"))

    outcomes = await _sweep(db_session_factory, redis)

    assert [o.status for o in outcomes] == ["no_price"]  # stopped at the sold leg
    assert (await _position(db_session_factory, subscription, PAST)).quantity == 65


# ---- futures that were never rolled ----


async def test_a_future_that_expired_unrolled_is_closed_at_its_last_quote(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, option=False, contract=FUT)
    await _hold(db_session_factory, subscription, FUT.symbol, -65, 24_000.0)
    await publish_tick(redis, Tick(FUT.symbol, 23_950.0, _ms(-7200), "real"))

    [outcome] = await _sweep(db_session_factory, redis)

    assert outcome.status == "closed"
    [fill] = await _fills(db_session_factory, subscription)
    assert (fill.side, fill.avg_fill_price, fill.costs_total) == ("buy", 23_950.0, 0.0)
    assert (await _position(db_session_factory, subscription, FUT.symbol)).quantity == 0


async def test_a_future_still_before_expiry_is_not_touched_by_the_sweep(db_session_factory, redis):
    live = FuturesContract(
        futures_symbol("NIFTY", date(2026, 10, 27)), "NIFTY", date(2026, 10, 27), 65, 10.0
    )
    subscription = await _subscription(db_session_factory, option=False, contract=live)
    await _hold(db_session_factory, subscription, live.symbol, 65, 24_000.0)
    await publish_tick(redis, Tick(live.symbol, 24_100.0, _ms(), "real"))

    assert await _sweep(db_session_factory, redis) == []
    await redis.delete(depth_key(live.symbol), tick_stream_key(live.symbol))


async def test_a_future_expiring_tomorrow_is_the_rolls_job_not_the_sweeps(
    db_session_factory, redis
):
    tomorrow = FuturesContract(
        futures_symbol("NIFTY", date(2026, 10, 20)), "NIFTY", date(2026, 10, 20), 65, 10.0
    )
    subscription = await _subscription(db_session_factory, option=False, contract=tomorrow)
    await _hold(db_session_factory, subscription, tomorrow.symbol, 65, 24_000.0)

    assert await _sweep(db_session_factory, redis) == []


# ---- who is swept ----


async def test_an_inactive_subscriptions_position_is_its_operators_choice_and_is_left(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, active=False)
    await _hold(db_session_factory, subscription, PAST, 65)
    await publish_tick(redis, Tick(PAST, 12.5, _ms(-3600), "real"))

    assert await _sweep(db_session_factory, redis) == []
    assert (await _position(db_session_factory, subscription, PAST)).quantity == 65


async def test_an_equity_position_is_never_swept(db_session_factory, redis):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="EQ-SW", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        equity = await enroll_in_paper_trading(db, strategy_version_id=version.id, symbol="INFY")
        db.add(
            PaperPosition(subscription_id=equity.id, symbol="INFY", quantity=10, avg_cost=1500.0)
        )
        await db.commit()

    assert await _sweep(db_session_factory, redis) == []


async def test_a_flat_leg_is_nothing_to_sweep(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, PAST, 0)

    assert await _sweep(db_session_factory, redis) == []
