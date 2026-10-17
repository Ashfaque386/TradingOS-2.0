"""The jobs and controls know a future is quoted, drained and closed by its contract (phase 4)."""

import asyncio
import time
import uuid
from datetime import date

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
from src.models.daily_signal import DailySignal
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration import paper_trading_scheduler as scheduler_module
from src.orchestration.paper_subscription_controls import (
    activate_subscription,
    close_position,
    deactivate_subscription,
)
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

OCT = FuturesContract(
    futures_symbol("NIFTY", date(2026, 10, 27)), "NIFTY", date(2026, 10, 27), 65, 10.0
)
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [tick_stream_key(OCT.symbol), depth_key(OCT.symbol), tick_stream_key("NIFTY")]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _future(db_session_factory, *, capital=1_000_000.0, contract=OCT):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"FS-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=capital,
            contract=contract,
        )


def _book(price=24_000.0, quantity=1_000):
    return DepthSnapshot(
        bids=(OrderBookLevel(price - 5, quantity),),
        asks=(OrderBookLevel(price + 5, quantity),),
        timestamp_ms=int(time.time() * 1000),
    )


# ---- publishing: what gets quoted ----


async def test_the_tick_job_quotes_a_futures_contract_not_its_underlying(
    db_session_factory, redis, monkeypatch
):
    await _future(db_session_factory)
    asked: list[list[str]] = []

    class _Source:
        async def next_tick(self, symbol: str) -> Tick:
            asked.append([symbol])
            return Tick(symbol=symbol, price=24_000.0, timestamp_ms=int(time.time() * 1000))

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await scheduler_module.run_tick_publish_job(db_session_factory, redis, _Source)

    quoted = {symbol for call in asked for symbol in call}
    assert OCT.symbol in quoted and "NIFTY" not in quoted
    assert await redis.xlen(tick_stream_key(OCT.symbol)) == 1


async def test_a_stock_and_its_future_are_each_quoted_once(db_session_factory, redis, monkeypatch):
    await _future(db_session_factory)
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="FS-EQ", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        await enroll_in_paper_trading(db, strategy_version_id=version.id, symbol="NIFTY")
    asked: list[str] = []

    class _Source:
        async def next_tick(self, symbol: str) -> Tick:
            asked.append(symbol)
            return Tick(symbol=symbol, price=24_000.0, timestamp_ms=int(time.time() * 1000))

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await scheduler_module.run_tick_publish_job(db_session_factory, redis, _Source)

    assert sorted(set(asked)) == sorted({OCT.symbol, "NIFTY"}) and len(asked) == len(set(asked))


# ---- draining: what gets traded ----


async def test_the_drain_job_reads_the_contracts_stream_and_trades_its_lots(
    db_session_factory, redis, monkeypatch
):
    subscription = await _future(db_session_factory)
    async with db_session_factory() as db:
        db.add(
            DailySignal(
                subscription_id=subscription.id,
                symbol="NIFTY",
                signal_type="BUY",
                reference_price=24_000.0,
            )
        )
        await db.commit()
    await publish_depth(redis, OCT.symbol, _book(), ttl_seconds=60)
    # The underlying's own stream has a tick too (an index is quotable): it must be ignored. The
    # gap keeps the two streams' newest ids different, so the cursor says which one was read.
    await publish_tick(redis, Tick("NIFTY", 24_100.0, int(time.time() * 1000), "real"))
    await asyncio.sleep(0.01)
    await publish_tick(redis, Tick(OCT.symbol, 24_000.0, int(time.time() * 1000), "real"))
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis,
        RealDepthOrderBookProvider(redis, max_age_seconds=30),
        _REGULATORY,
    )

    async with db_session_factory() as db:
        position = (
            await db.execute(
                select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
            )
        ).scalar_one()
        stored = await db.get(PaperTradingSubscription, subscription.id)
    assert (position.symbol, position.quantity) == (OCT.symbol, 65)
    newest = (await redis.xrevrange(tick_stream_key(OCT.symbol), count=1))[0][0]
    assert stored.tick_cursor == newest  # the cursor is on the contract's stream


# ---- the roll job ----


async def test_the_roll_job_does_nothing_while_the_market_is_shut(
    db_session_factory, redis, monkeypatch
):
    from datetime import datetime

    from src.engine.paper_trading.market_hours import IST

    calls: list[int] = []

    class _Broker:
        broker_name = "zerodha"

        async def get_futures_contracts(self, underlying):
            calls.append(1)
            return []

    today = datetime.now(IST).date()
    due = FuturesContract(futures_symbol("NIFTY", today), "NIFTY", today, 65, 10.0)
    await _future(db_session_factory, contract=due)  # expires today: certainly due for a roll

    async def run():
        await scheduler_module.run_futures_roll_job(
            db_session_factory,
            redis,
            RealDepthOrderBookProvider(redis),
            _REGULATORY,
            lambda: [_Broker()],
        )

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    await run()
    assert calls == []  # shut: not even the broker is asked

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    await run()
    assert calls == [1]  # open: the due subscription is looked at


async def test_the_roll_job_uses_the_configured_window_and_the_connected_brokers(
    db_session_factory, redis, monkeypatch
):
    await _future(db_session_factory)
    seen: dict = {}
    real = scheduler_module.run_futures_roll

    async def spy(db, redis_, **kwargs):
        seen.update(kwargs)
        return await real(db, redis_, **kwargs)

    class _Broker:
        broker_name = "zerodha"

        async def get_futures_contracts(self, underlying):
            return []

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(scheduler_module, "run_futures_roll", spy)
    monkeypatch.setenv("PAPER_FUTURES_ROLL_DAYS", "5")
    get_settings.cache_clear()

    await scheduler_module.run_futures_roll_job(
        db_session_factory,
        redis,
        RealDepthOrderBookProvider(redis),
        _REGULATORY,
        lambda: [_Broker()],
    )

    assert seen["roll_days"] == 5
    assert [adapter.broker_name for adapter in seen["adapters"]] == ["zerodha"]


# ---- the controls ----


async def test_a_manual_close_prices_off_the_contracts_tick_and_book(db_session_factory, redis):
    subscription = await _future(db_session_factory)
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol=OCT.symbol, quantity=65, avg_cost=24_000.0
            )
        )
        await db.commit()
    await publish_tick(redis, Tick(OCT.symbol, 24_050.0, int(time.time() * 1000), "real"))
    await publish_tick(redis, Tick("NIFTY", 99.0, int(time.time() * 1000), "real"))  # not the price
    await publish_depth(redis, OCT.symbol, _book(24_050.0), ttl_seconds=60)

    async with db_session_factory() as db:
        result = await close_position(
            db,
            redis,
            subscription.id,
            actor="tester",
            reason="test",
            price=None,
            administrative=False,
            order_book_provider=RealDepthOrderBookProvider(redis),
            regulatory_provider=_REGULATORY,
        )

    assert result.fill.symbol == OCT.symbol and result.fill.filled_quantity == 65
    assert result.fill.avg_fill_price == 24_045.0  # the contract's bid, not the underlying's tick
    assert result.position.quantity == 0


async def test_activating_resets_the_cursor_on_the_contracts_stream(db_session_factory, redis):
    subscription = await _future(db_session_factory)
    async with db_session_factory() as db:
        await deactivate_subscription(
            db,
            redis,
            subscription.id,
            actor="tester",
            reason="pause",
            on_open_position=None,
            close_price=None,
            order_book_provider=RealDepthOrderBookProvider(redis),
            regulatory_provider=_REGULATORY,
        )
    await publish_tick(redis, Tick(OCT.symbol, 24_000.0, int(time.time() * 1000), "real"))
    newest = (await redis.xrevrange(tick_stream_key(OCT.symbol), count=1))[0][0]

    async with db_session_factory() as db:
        await activate_subscription(db, redis, subscription.id, actor="tester")

    async with db_session_factory() as db:
        assert (await db.get(PaperTradingSubscription, subscription.id)).tick_cursor == newest


# ---- when a contract counts as expired ----


def _expiring(day: date, *, kind="future", active=True) -> PaperTradingSubscription:
    return PaperTradingSubscription(
        symbol="NIFTY",
        instrument_kind=kind,
        contract_symbol=OCT.symbol if kind == "future" else None,
        contract_expiry=day,  # an equity row carrying one must still not be judged by it
        is_active=active,
    )


@pytest.mark.parametrize(
    ("expiry", "expired"),
    [
        (date(2026, 10, 26), True),  # yesterday
        (date(2026, 10, 27), False),  # today: the contract still trades until the close
        (date(2026, 10, 28), False),
    ],
)
def test_a_contract_is_expired_only_after_its_expiry_day(expiry, expired):
    from datetime import UTC, datetime

    from src.orchestration.paper_subscription_controls import contract_problem

    now = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)  # 10:30 IST on the 27th

    problem = contract_problem(_expiring(expiry), now)

    assert (problem is not None) is expired
    if expired:
        assert OCT.symbol in problem and "2026-10-26" in problem


def test_an_equity_or_an_inactive_subscription_has_no_contract_problem():
    from datetime import UTC, datetime

    from src.orchestration.paper_subscription_controls import contract_problem

    now = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)

    assert contract_problem(_expiring(date(2026, 1, 1), kind="equity"), now) is None
    assert contract_problem(_expiring(date(2026, 1, 1), active=False), now) is None


def test_the_expiry_day_is_judged_in_ist_not_utc():
    from datetime import UTC, datetime

    from src.orchestration.paper_subscription_controls import contract_problem

    # 19:00 UTC on the 26th is already 00:30 IST on the 27th: the 26th's contract has expired.
    now = datetime(2026, 10, 26, 19, 0, tzinfo=UTC)

    assert contract_problem(_expiring(date(2026, 10, 26)), now) is not None
