"""Equity measurement (Phase 3): capital + realized (net of costs) + unrealized at the latest tick,
unknown -- never zero -- whenever any part of it is unknown.
"""

from datetime import UTC, datetime, timedelta

from src.engine.paper_trading.tick_feed import Tick, publish_tick
from src.models.equity_snapshot import EquitySnapshot
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.equity import (
    measure_equity,
    measure_paper_equity,
    peak_equity_since,
    record_snapshot,
)
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def _subscription(db, name="EquityDemo", symbol="EQTY", capital=100_000.0):
    strategy = await create_strategy(db, name=name, objective="obj")
    version = await create_version_with_validation(db, strategy, _VALID_CODE)
    subscription = PaperTradingSubscription(
        strategy_version_id=version.id,
        symbol=symbol,
        builtin_strategy="always_long",
        initial_capital=capital,
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def _tick(redis, symbol, price):
    await publish_tick(
        redis,
        Tick(symbol=symbol, price=price, timestamp_ms=int(datetime.now(UTC).timestamp() * 1000)),
    )


async def test_with_no_positions_equity_is_capital_plus_realized_net_of_costs(
    db_session_factory, redis_client
):
    async with db_session_factory() as db:
        sub = await _subscription(db)
        db.add(
            PaperPosition(
                subscription_id=sub.id,
                symbol="EQTY",
                quantity=0,
                avg_cost=0.0,
                realized_pnl=2_500.0,
                costs_total=300.0,
            )
        )
        await db.commit()
        sample = await measure_paper_equity(db, redis_client, price_source="real")

    assert sample.mode == "paper"
    assert sample.capital_base == 100_000.0
    assert sample.realized_net == 2_200.0  # gross less what the fills cost
    assert sample.unrealized == 0.0
    assert sample.equity == 102_200.0
    assert sample.open_positions == 0
    assert sample.unpriced_positions == 0
    assert sample.price_source == "real"
    assert sample.note is None


async def test_open_positions_are_marked_at_the_latest_tick_long_and_short(
    db_session_factory, redis_client
):
    symbol_a, symbol_b = "EQLONG", "EQSHORT"
    async with db_session_factory() as db:
        sub_a = await _subscription(db, name="A", symbol=symbol_a)
        sub_b = await _subscription(db, name="B", symbol=symbol_b)
        db.add(
            PaperPosition(subscription_id=sub_a.id, symbol=symbol_a, quantity=10, avg_cost=100.0)
        )
        db.add(
            PaperPosition(subscription_id=sub_b.id, symbol=symbol_b, quantity=-5, avg_cost=200.0)
        )
        await db.commit()
    await _tick(redis_client, symbol_a, 110.0)  # +10 each on 10 long = +100
    await _tick(redis_client, symbol_b, 190.0)  # a short gains when the price falls: +10 x 5 = +50

    async with db_session_factory() as db:
        sample = await measure_paper_equity(db, redis_client, price_source="synthetic")

    assert sample.capital_base == 200_000.0
    assert sample.unrealized == 150.0
    assert sample.equity == 200_150.0
    assert sample.open_positions == 2
    assert sample.price_source == "synthetic"


async def test_an_open_position_with_no_price_makes_the_equity_unknown_not_zero(
    db_session_factory, redis_client
):
    async with db_session_factory() as db:
        sub = await _subscription(db, symbol="EQUNPRICED")
        db.add(
            PaperPosition(subscription_id=sub.id, symbol="EQUNPRICED", quantity=4, avg_cost=50.0)
        )
        await db.commit()
        sample = await measure_paper_equity(db, redis_client, price_source="none")

    assert sample.equity is None
    assert sample.unrealized is None
    assert sample.unpriced_positions == 1
    assert sample.open_positions == 1
    assert sample.capital_base == 100_000.0
    assert "1 open position(s) have no price" in sample.note


async def test_no_capital_anywhere_is_unknown_not_a_zero_equity(db_session_factory, redis_client):
    async with db_session_factory() as db:
        sample = await measure_paper_equity(db, redis_client, price_source="none")

    assert sample.equity is None
    assert sample.capital_base == 0.0
    assert "no paper subscription has any capital" in sample.note


async def test_capital_counts_inactive_subscriptions_too(db_session_factory, redis_client):
    async with db_session_factory() as db:
        sub = await _subscription(db, symbol="EQOFF")
        sub.is_active = False
        await db.commit()
        sample = await measure_paper_equity(db, redis_client, price_source="real")

    assert sample.capital_base == 100_000.0


async def test_live_without_a_starting_capital_is_unknown(db_session_factory, redis_client):
    async with db_session_factory() as db:
        sample = await measure_equity(db, redis_client, "live")

    assert sample.mode == "live"
    assert sample.equity is None
    assert "LIVE_STARTING_CAPITAL is not set" in sample.note


async def test_live_with_a_starting_capital_is_measured_from_the_ledger(
    db_session_factory, redis_client, monkeypatch
):
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "live_starting_capital", 250_000.0)

    async def quote(_symbol):
        return None

    async with db_session_factory() as db:
        sample = await measure_equity(db, redis_client, "live", live_quote=quote)

    assert sample.equity == 250_000.0
    assert sample.capital_base == 250_000.0


async def test_measure_equity_dispatches_paper(db_session_factory, redis_client):
    async with db_session_factory() as db:
        await _subscription(db, symbol="EQDISP")
        await db.commit()
        sample = await measure_equity(db, redis_client, "paper")

    assert sample.mode == "paper"
    assert sample.equity == 100_000.0


async def test_the_peak_is_the_highest_measured_equity_since_the_cut_off(db_session_factory):
    base = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)

    async with db_session_factory() as db:
        for minutes, equity in ((0, 100.0), (1, 150.0), (2, None), (3, 120.0)):
            db.add(
                EquitySnapshot(
                    mode="paper",
                    taken_at=base + timedelta(minutes=minutes),
                    equity=equity,
                    open_positions=0,
                    unpriced_positions=0,
                    price_source="real",
                )
            )
        db.add(
            EquitySnapshot(
                mode="live",
                taken_at=base,
                equity=9_999.0,
                open_positions=0,
                unpriced_positions=0,
                price_source="real",
            )
        )
        await db.commit()

        assert await peak_equity_since(db, "paper", None) == 150.0
        assert await peak_equity_since(db, "paper", base + timedelta(minutes=2)) == 120.0
        assert (
            await peak_equity_since(db, "paper", base + timedelta(minutes=1)) == 150.0
        )  # inclusive
        assert await peak_equity_since(db, "paper", base + timedelta(minutes=4)) is None
        assert await peak_equity_since(db, "live", None) == 9_999.0


async def test_record_snapshot_keeps_the_reading_and_what_was_judged_from_it(db_session_factory):
    from src.orchestration.equity import EquitySample

    sample = EquitySample(
        mode="paper",
        equity=95.0,
        capital_base=100.0,
        realized_net=-3.0,
        unrealized=-2.0,
        open_positions=2,
        unpriced_positions=0,
        price_source="synthetic",
        note=None,
    )
    taken = datetime(2026, 10, 2, 6, 30, tzinfo=UTC)
    async with db_session_factory() as db:
        record_snapshot(db, sample, peak_equity=100.0, drawdown_pct=5.0, taken_at=taken)
        await db.commit()
        from sqlalchemy import select

        row = (await db.execute(select(EquitySnapshot))).scalar_one()

    assert (row.mode, row.equity, row.capital_base, row.realized_net, row.unrealized) == (
        "paper",
        95.0,
        100.0,
        -3.0,
        -2.0,
    )
    assert (row.open_positions, row.unpriced_positions, row.price_source) == (2, 0, "synthetic")
    assert (row.peak_equity, row.drawdown_pct, row.note) == (100.0, 5.0, None)
    assert row.taken_at == taken


def test_the_price_source_is_real_with_a_broker_synthetic_when_switched_on_else_none(monkeypatch):
    from src.core.config import get_settings
    from src.orchestration import equity as equity_module

    settings = get_settings()
    monkeypatch.setattr(equity_module, "is_broker_configured", lambda: True)
    assert equity_module.paper_price_source() == "real"

    monkeypatch.setattr(equity_module, "is_broker_configured", lambda: False)
    monkeypatch.setattr(settings, "paper_trading_synthetic", True)
    assert equity_module.paper_price_source() == "synthetic"

    monkeypatch.setattr(settings, "paper_trading_synthetic", False)
    assert equity_module.paper_price_source() == "none"


async def test_a_live_position_is_priced_at_the_brokers_quote_or_not_at_all():
    from types import SimpleNamespace

    from src.orchestration.equity import adapter_quote

    class _Broker:
        async def get_quote(self, symbol):
            if symbol == "BROKEN":
                raise ConnectionError("no quote")
            return SimpleNamespace(last_price=123.5)

    assert await adapter_quote(None)("TCS") is None  # no broker connected: no price
    assert await adapter_quote(_Broker())("TCS") == 123.5
    assert await adapter_quote(_Broker())("BROKEN") is None  # a failed quote is no price, not zero


async def test_the_configured_quote_follows_whichever_broker_is_connected_now(monkeypatch):
    from types import SimpleNamespace

    from src.orchestration import equity as equity_module

    class _Broker:
        async def get_quote(self, _symbol):
            return SimpleNamespace(last_price=77.0)

    monkeypatch.setattr(equity_module, "build_configured_adapter", lambda sandbox: _Broker())
    assert await equity_module.configured_live_quote()("INFY") == 77.0

    monkeypatch.setattr(equity_module, "build_configured_adapter", lambda sandbox: None)
    assert await equity_module.configured_live_quote()("INFY") is None
