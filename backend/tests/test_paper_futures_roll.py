"""Rolling a futures subscription into the next month (phase 4).

A few days before expiry the position is closed on the old contract -- through the risk gate, at a
live tick, on real depth, charged -- and only then is the subscription pointed at the next one, with
that contract's own lot size. A roll that cannot close now is postponed, never forced: the
subscription is not moved onto a new contract while it still holds the old one.
"""

import time
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.brokers.base import FuturesContract, futures_symbol
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
from src.orchestration.paper_futures import (
    ROLL_ACTOR,
    roll_subscription,
    run_futures_roll,
)
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

OCT = FuturesContract(
    futures_symbol("NIFTY", date(2026, 10, 27)), "NIFTY", date(2026, 10, 27), 65, 10.0
)
NOV = FuturesContract(
    futures_symbol("NIFTY", date(2026, 11, 23)), "NIFTY", date(2026, 11, 23), 75, 10.0
)
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [tick_stream_key(c.symbol) for c in (OCT, NOV)] + [
        depth_key(c.symbol) for c in (OCT, NOV)
    ]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _book(price=24_000.0, quantity=1_000):
    return DepthSnapshot(
        bids=(OrderBookLevel(price - 5, quantity),),
        asks=(OrderBookLevel(price + 5, quantity),),
        timestamp_ms=int(time.time() * 1000),
    )


async def _live(redis, contract, price=24_000.0, quantity=1_000):
    """A fresh tick and book for `contract`: what a running feed leaves behind."""
    await publish_tick(redis, Tick(contract.symbol, price, int(time.time() * 1000), "real"))
    await publish_depth(redis, contract.symbol, _book(price, quantity), ttl_seconds=60)


async def _subscription(db_session_factory, *, contract=OCT, position=0, **overrides):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"ROLL-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=1_000_000.0,
            contract=contract,
            **overrides,
        )
        if position:
            db.add(
                PaperPosition(
                    subscription_id=subscription.id,
                    symbol=contract.symbol,
                    quantity=position,
                    avg_cost=24_000.0,
                )
            )
            await db.commit()
        return subscription


async def _reload(db_session_factory, subscription):
    async with db_session_factory() as db:
        stored = await db.get(PaperTradingSubscription, subscription.id)
        position = (
            await db.execute(
                select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
            )
        ).scalar_one_or_none()
        fills = list(
            (
                await db.execute(
                    select(PaperFill).where(PaperFill.subscription_id == subscription.id)
                )
            )
            .scalars()
            .all()
        )
    return stored, position, fills


async def _roll(db_session_factory, redis, subscription, contract=NOV):
    async with db_session_factory() as db:
        stored = await db.get(PaperTradingSubscription, subscription.id)
        return await roll_subscription(
            db,
            redis,
            stored,
            contract,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            regulatory_provider=_REGULATORY,
        )


# ---- rolling one subscription ----


async def test_a_flat_subscription_moves_to_the_next_contract_and_its_lot_size(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)

    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, fills = await _reload(db_session_factory, subscription)
    assert outcome.status == "rolled" and (outcome.old_contract, outcome.new_contract) == (
        OCT.symbol,
        NOV.symbol,
    )
    assert (stored.contract_symbol, stored.contract_expiry, stored.lot_size) == (
        NOV.symbol,
        NOV.expiry,
        75,  # the new contract's lot size, not the old one's 65
    )
    assert stored.symbol == "NIFTY"  # signals still come from the underlying
    assert position.symbol == NOV.symbol and fills == []


async def test_a_roll_is_audited_with_what_moved(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    await _roll(db_session_factory, redis, subscription)

    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "paper_trading.futures_rolled")
            )
        ).scalar_one()
    assert entry.actor == ROLL_ACTOR and entry.entity_id == str(subscription.id)
    assert entry.details == {
        "underlying": "NIFTY",
        "from": OCT.symbol,
        "to": NOV.symbol,
        "expiry": "2026-11-23",
        "lot_size": 75,
    }


async def test_the_cursor_starts_at_the_new_streams_newest_tick_not_the_beginning(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await publish_tick(redis, Tick(NOV.symbol, 24_100.0, int(time.time() * 1000), "real"))
    newest = (await redis.xrevrange(tick_stream_key(NOV.symbol), count=1))[0][0]

    await _roll(db_session_factory, redis, subscription)

    stored, _, _ = await _reload(db_session_factory, subscription)
    assert (
        stored.tick_cursor == newest
    )  # nothing published earlier is this subscription's to act on


async def test_an_empty_new_stream_starts_the_cursor_at_the_beginning(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    await _roll(db_session_factory, redis, subscription)

    stored, _, _ = await _reload(db_session_factory, subscription)
    assert stored.tick_cursor == "0"


async def test_an_open_position_is_closed_on_the_old_contract_before_the_move(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, position=-65)  # short one lot
    await _live(redis, OCT)

    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, fills = await _reload(db_session_factory, subscription)
    assert outcome.status == "rolled"
    assert [(f.symbol, f.side, f.filled_quantity) for f in fills] == [(OCT.symbol, "buy", 65)]
    assert fills[0].costs_total > 0  # closed like any order: charged
    assert position.quantity == 0 and position.symbol == NOV.symbol
    assert stored.contract_symbol == NOV.symbol


async def test_with_no_fresh_tick_the_roll_waits_and_changes_nothing(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, position=65)

    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, fills = await _reload(db_session_factory, subscription)
    assert outcome.status == "postponed"
    assert stored.contract_symbol == OCT.symbol and stored.lot_size == 65  # still the old contract
    assert position.quantity == 65 and fills == []  # and still holding it


async def test_with_no_book_for_the_old_contract_the_roll_waits(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, position=65)
    await publish_tick(
        redis, Tick(OCT.symbol, 24_000.0, int(time.time() * 1000), "real")
    )  # no depth

    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, _ = await _reload(db_session_factory, subscription)
    assert outcome.status == "postponed" and "order book" in outcome.detail
    assert stored.contract_symbol == OCT.symbol and position.quantity == 65


async def test_a_book_too_thin_to_close_it_all_leaves_the_rest_held_and_the_roll_waiting(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, position=65)
    await _live(redis, OCT, quantity=40)  # 40 of the 65 can be sold

    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, fills = await _reload(db_session_factory, subscription)
    assert outcome.status == "postponed" and "25 left" in outcome.detail
    assert position.quantity == 25 and [f.filled_quantity for f in fills] == [40]
    assert stored.contract_symbol == OCT.symbol  # never moved while still holding the old one


async def test_a_postponed_roll_succeeds_on_a_later_run(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, position=65)
    assert (await _roll(db_session_factory, redis, subscription)).status == "postponed"

    await _live(redis, OCT)
    outcome = await _roll(db_session_factory, redis, subscription)

    stored, position, _ = await _reload(db_session_factory, subscription)
    assert (
        outcome.status == "rolled"
        and stored.contract_symbol == NOV.symbol
        and position.quantity == 0
    )


# ---- the job that decides which subscriptions to roll ----


class _Broker:
    broker_name = "zerodha"

    def __init__(self, contracts=(OCT, NOV), error=None):
        self._contracts = list(contracts)
        self._error = error
        self.asked: list[str] = []

    async def get_futures_contracts(self, underlying):
        self.asked.append(underlying)
        if self._error:
            raise self._error
        return self._contracts


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)


async def _run(db_session_factory, redis, broker, today, roll_days=3):
    async with db_session_factory() as db:
        return await run_futures_roll(
            db,
            redis,
            adapters=[broker],
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            regulatory_provider=_REGULATORY,
            roll_days=roll_days,
            now=_at(today),
        )


async def test_a_contract_not_yet_due_is_left_alone_and_the_broker_not_asked(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    broker = _Broker()

    outcomes = await _run(db_session_factory, redis, broker, today=date(2026, 10, 23))  # 4 days

    stored, _, _ = await _reload(db_session_factory, subscription)
    assert outcomes == [] and broker.asked == []
    assert stored.contract_symbol == OCT.symbol


async def test_a_contract_three_days_from_expiry_is_rolled(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    outcomes = await _run(db_session_factory, redis, _Broker(), today=date(2026, 10, 24))

    stored, _, _ = await _reload(db_session_factory, subscription)
    assert [o.status for o in outcomes] == ["rolled"]
    assert stored.contract_symbol == NOV.symbol


async def test_the_roll_window_is_the_setting(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    assert (
        await _run(db_session_factory, redis, _Broker(), today=date(2026, 10, 20), roll_days=3)
        == []
    )
    outcomes = await _run(
        db_session_factory, redis, _Broker(), today=date(2026, 10, 20), roll_days=7
    )

    assert [o.status for o in outcomes] == ["rolled"]
    assert (await _reload(db_session_factory, subscription))[0].contract_symbol == NOV.symbol


async def test_a_contract_that_expired_unrolled_is_rolled_when_a_run_gets_to_it(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)

    outcomes = await _run(db_session_factory, redis, _Broker(), today=date(2026, 10, 29))

    assert [o.status for o in outcomes] == ["rolled"]
    assert (await _reload(db_session_factory, subscription))[0].contract_symbol == NOV.symbol


async def test_with_no_contract_to_roll_into_the_subscription_stays_put_and_says_so(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)

    outcomes = await _run(
        db_session_factory, redis, _Broker(contracts=[OCT]), today=date(2026, 10, 25)
    )

    assert [o.status for o in outcomes] == ["no_contract"]
    assert "no futures contract for NIFTY" in outcomes[0].detail
    assert (await _reload(db_session_factory, subscription))[0].contract_symbol == OCT.symbol


async def test_a_broker_that_is_down_is_a_no_contract_outcome_not_a_crash(
    db_session_factory, redis
):
    await _subscription(db_session_factory)

    outcomes = await _run(
        db_session_factory, redis, _Broker(error=ConnectionError("reset")), today=date(2026, 10, 25)
    )

    assert [o.status for o in outcomes] == ["no_contract"] and "ConnectionError" in outcomes[
        0
    ].detail


async def test_inactive_and_equity_subscriptions_are_not_rolled(db_session_factory, redis):
    inactive = await _subscription(db_session_factory)
    async with db_session_factory() as db:
        (await db.get(PaperTradingSubscription, inactive.id)).is_active = False
        strategy = await create_strategy(db, name="EQ-ROLL", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        await enroll_in_paper_trading(db, strategy_version_id=version.id, symbol="NIFTY")  # equity
        await db.commit()
    broker = _Broker()

    outcomes = await _run(db_session_factory, redis, broker, today=date(2026, 10, 25))

    assert outcomes == [] and broker.asked == []


async def test_one_subscription_that_cannot_roll_does_not_stop_the_others(
    db_session_factory, redis
):
    held = await _subscription(db_session_factory, position=65)  # no tick: postponed
    flat = await _subscription(db_session_factory)

    outcomes = await _run(db_session_factory, redis, _Broker(), today=date(2026, 10, 25))

    assert sorted(o.status for o in outcomes) == ["postponed", "rolled"]
    assert (await _reload(db_session_factory, held))[0].contract_symbol == OCT.symbol
    assert (await _reload(db_session_factory, flat))[0].contract_symbol == NOV.symbol


async def test_a_subscription_already_on_the_right_contract_is_not_rolled_or_closed(
    db_session_factory, redis
):
    """A stored expiry that is out of date (it says October) on a subscription that is in fact on
    the November contract: the job must see it needs no move, not close a good position."""
    subscription = await _subscription(db_session_factory, contract=NOV, position=65)
    async with db_session_factory() as db:
        (await db.get(PaperTradingSubscription, subscription.id)).contract_expiry = date(
            2026, 10, 26
        )
        await db.commit()
    await _live(redis, NOV)

    outcomes = await _run(db_session_factory, redis, _Broker(), today=date(2026, 10, 25))

    stored, position, fills = await _reload(db_session_factory, subscription)
    assert outcomes == [] and fills == []
    assert stored.contract_symbol == NOV.symbol and position.quantity == 65
