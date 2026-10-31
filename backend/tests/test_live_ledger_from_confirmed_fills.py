"""The live ledger (Phase 3): a position moves only when the broker confirms a fill, at the broker's
price, with an estimate of what the fill cost; a rejected or cancelled order moves nothing; one
position per strategy and symbol; and no second decision is made while an order is unconfirmed.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.brokers.base import BrokerOrder, OrderSide
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.trade import Trade
from src.orchestration.equity import measure_live_equity
from src.orchestration.live_trading import _estimated_costs, reconcile_pending_trades
from src.orchestration.strategies import create_strategy


class _Book:
    broker_name = "zerodha"

    def __init__(self, orders: list[BrokerOrder]) -> None:
        self._orders = orders

    async def get_order_book(self) -> list[BrokerOrder]:
        return self._orders


def _complete(order_id, symbol, side, quantity, average_price):
    return BrokerOrder(
        broker_order_id=order_id,
        symbol=symbol,
        side=OrderSide(side),
        quantity=quantity,
        status="COMPLETE",
        average_price=average_price,
    )


async def _strategy(db_session_factory):
    async with db_session_factory() as db:
        return (await create_strategy(db, name=f"Ledger{uuid.uuid4().hex[:6]}", objective="o")).id


async def _send(db_session_factory, strategy_id, *, symbol, side, quantity, price, order_id):
    """What a submitted order leaves behind: an order and a trade awaiting confirmation."""
    async with db_session_factory() as db:
        intent = LiveOrderIntent(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            intent_type="entry",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            status="submitted",
        )
        db.add(intent)
        await db.flush()
        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            broker_name="zerodha",
            broker_order_id=order_id,
            status="submitted",
        )
        db.add(order)
        await db.flush()
        trade = Trade(
            order_id=order.id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            status="pending_confirmation",
        )
        db.add(trade)
        await db.commit()
        return trade.id


async def _positions(db_session_factory, strategy_id):
    async with db_session_factory() as db:
        rows = await db.execute(
            select(LivePosition)
            .where(LivePosition.strategy_id == strategy_id)
            .order_by(LivePosition.symbol)
        )
        return list(rows.scalars().all())


async def test_a_sent_order_moves_no_position_until_the_broker_confirms_it(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=3500.0,
        order_id="L1",
    )

    assert await _positions(db_session_factory, strategy_id) == []


async def test_a_confirmed_fill_moves_the_position_at_the_brokers_price_and_records_costs(
    db_session_factory,
):
    strategy_id = await _strategy(db_session_factory)
    trade_id = await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=3500.0,
        order_id="L1",
    )
    book = _Book([_complete("L1", "TCS", "buy", 10, 3512.5)])

    async with db_session_factory() as db:
        assert await reconcile_pending_trades(db, book) == 1

    (position,) = await _positions(db_session_factory, strategy_id)
    assert position.symbol == "TCS"
    assert position.quantity == 10
    assert position.avg_cost == 3512.5  # the broker's price, not the 3500 it was priced against
    assert position.realized_pnl == 0.0
    expected_costs = _estimated_costs("buy", 10, 3512.5)
    assert expected_costs > 0
    assert position.costs_total == expected_costs
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
    assert trade.costs_total == expected_costs
    assert trade.fill_price == 3512.5


async def test_a_completed_order_without_a_price_uses_the_price_it_was_placed_against(
    db_session_factory,
):
    strategy_id = await _strategy(db_session_factory)
    trade_id = await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=4,
        price=3500.0,
        order_id="L1",
    )
    book = _Book([_complete("L1", "TCS", "buy", 4, None)])

    async with db_session_factory() as db:
        await reconcile_pending_trades(db, book)

    (position,) = await _positions(db_session_factory, strategy_id)
    assert (position.quantity, position.avg_cost) == (4, 3500.0)
    async with db_session_factory() as db:
        assert (await db.get(Trade, trade_id)).fill_price is None  # still says the broker gave none


async def test_a_rejected_or_cancelled_order_moves_nothing(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=3500.0,
        order_id="R1",
    )
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=3500.0,
        order_id="R2",
    )
    book = _Book(
        [
            BrokerOrder("R1", "TCS", OrderSide.BUY, 10, "REJECTED", None),
            BrokerOrder("R2", "TCS", OrderSide.BUY, 10, "CANCELLED", None),
        ]
    )

    async with db_session_factory() as db:
        assert await reconcile_pending_trades(db, book) == 2

    assert await _positions(db_session_factory, strategy_id) == []


async def test_a_second_pass_does_not_apply_the_same_fill_again(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=3500.0,
        order_id="L1",
    )
    book = _Book([_complete("L1", "TCS", "buy", 10, 3500.0)])

    async with db_session_factory() as db:
        await reconcile_pending_trades(db, book)
    async with db_session_factory() as db:
        assert await reconcile_pending_trades(db, book) == 0

    (position,) = await _positions(db_session_factory, strategy_id)
    assert position.quantity == 10


async def test_a_buy_then_a_sell_realizes_the_difference_and_the_costs_of_both(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=10,
        price=100.0,
        order_id="B1",
    )
    async with db_session_factory() as db:
        await reconcile_pending_trades(db, _Book([_complete("B1", "TCS", "buy", 10, 100.0)]))
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="sell",
        quantity=10,
        price=110.0,
        order_id="S1",
    )
    async with db_session_factory() as db:
        await reconcile_pending_trades(db, _Book([_complete("S1", "TCS", "sell", 10, 112.0)]))

    (position,) = await _positions(db_session_factory, strategy_id)
    assert position.quantity == 0
    assert position.realized_pnl == 120.0  # (112 - 100) x 10, gross
    assert position.costs_total == _estimated_costs("buy", 10, 100.0) + _estimated_costs(
        "sell", 10, 112.0
    )


async def test_one_strategy_can_hold_two_symbols_in_two_rows(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    await _send(
        db_session_factory,
        strategy_id,
        symbol="TCS",
        side="buy",
        quantity=5,
        price=3500.0,
        order_id="A",
    )
    await _send(
        db_session_factory,
        strategy_id,
        symbol="INFY",
        side="buy",
        quantity=7,
        price=1500.0,
        order_id="B",
    )
    book = _Book(
        [_complete("A", "TCS", "buy", 5, 3500.0), _complete("B", "INFY", "buy", 7, 1500.0)]
    )

    async with db_session_factory() as db:
        await reconcile_pending_trades(db, book)

    positions = await _positions(db_session_factory, strategy_id)
    assert [(p.symbol, p.quantity) for p in positions] == [("INFY", 7), ("TCS", 5)]


# ---- live equity from that ledger


async def _quote_from(prices):
    async def quote(symbol):
        return prices.get(symbol)

    return quote


async def test_live_equity_is_unknown_without_a_starting_capital(db_session_factory):
    async with db_session_factory() as db:
        sample = await measure_live_equity(db, starting_capital=None, quote=await _quote_from({}))

    assert sample.mode == "live"
    assert sample.equity is None
    assert "LIVE_STARTING_CAPITAL is not set" in sample.note
    async with db_session_factory() as db:
        zero = await measure_live_equity(db, starting_capital=0.0, quote=await _quote_from({}))
    assert zero.equity is None


async def test_live_equity_is_capital_plus_realized_net_of_costs_plus_open_positions_at_the_quote(
    db_session_factory,
):
    strategy_id = await _strategy(db_session_factory)
    async with db_session_factory() as db:
        db.add(
            LivePosition(
                strategy_id=strategy_id,
                symbol="TCS",
                quantity=10,
                avg_cost=100.0,
                realized_pnl=500.0,
                costs_total=50.0,
            )
        )
        db.add(
            LivePosition(
                strategy_id=strategy_id,
                symbol="INFY",
                quantity=-4,
                avg_cost=200.0,
                realized_pnl=-100.0,
                costs_total=10.0,
            )
        )
        await db.commit()
        sample = await measure_live_equity(
            db, starting_capital=1_000_000.0, quote=await _quote_from({"TCS": 110.0, "INFY": 190.0})
        )

    assert sample.realized_net == 340.0  # (500 - 50) + (-100 - 10)
    assert sample.unrealized == 140.0  # +10 x 10 long, +10 x 4 short
    assert sample.equity == 1_000_480.0
    assert sample.capital_base == 1_000_000.0
    assert (sample.open_positions, sample.unpriced_positions) == (2, 0)
    assert sample.price_source == "real"
    assert sample.note is None


async def test_a_live_position_the_broker_cannot_price_makes_the_equity_unknown(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    async with db_session_factory() as db:
        db.add(LivePosition(strategy_id=strategy_id, symbol="TCS", quantity=10, avg_cost=100.0))
        await db.commit()
        sample = await measure_live_equity(
            db, starting_capital=1_000_000.0, quote=await _quote_from({})
        )

    assert sample.equity is None
    assert sample.unpriced_positions == 1
    assert "1 open live position(s) could not be priced" in sample.note


async def test_a_flat_position_needs_no_price(db_session_factory):
    strategy_id = await _strategy(db_session_factory)
    async with db_session_factory() as db:
        db.add(
            LivePosition(
                strategy_id=strategy_id, symbol="TCS", quantity=0, avg_cost=0.0, realized_pnl=25.0
            )
        )
        await db.commit()
        sample = await measure_live_equity(
            db, starting_capital=1_000.0, quote=await _quote_from({})
        )

    assert sample.equity == 1_025.0
    assert sample.open_positions == 0
