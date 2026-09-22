"""Broker-fill reconciliation tests (Phase 16 audit follow-up B):
`Trade.status` used to be permanently stuck at `pending_confirmation`
because nothing ever polled the broker for a real outcome --
`reconcile_pending_trades` is the fix. Uses a minimal fake adapter
implementing just the `BrokerAdapter` surface this function actually
calls (`broker_name`, `get_order_book()`), rather than HTTP-mocking a
real adapter class, since the reconciliation logic itself never touches
anything else on the Protocol.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.brokers.base import BrokerOrder, OrderSide
from src.models.audit_log import AuditLog
from src.models.live_order_intent import LiveOrderIntent
from src.models.order import Order
from src.models.trade import Trade
from src.orchestration.live_trading import reconcile_pending_trades
from src.orchestration.strategies import create_strategy


class _FakeAdapter:
    def __init__(self, broker_name: str, orders: list[BrokerOrder]) -> None:
        self.broker_name = broker_name
        self._orders = orders
        self.calls = 0

    async def get_order_book(self) -> list[BrokerOrder]:
        self.calls += 1
        return self._orders


class _FailingAdapter:
    broker_name = "zerodha"

    async def get_order_book(self) -> list[BrokerOrder]:
        raise ConnectionError("broker unreachable")


async def _make_pending_trade(
    db_session_factory, *, broker_name: str = "zerodha", broker_order_id: str | None = "OID1"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="ReconcileDemo", objective="obj")
        intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol="RELIANCE",
            side="buy",
            quantity=10,
            intent_type="entry",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            status="approved",
        )
        db.add(intent)
        await db.flush()

        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=strategy.id,
            symbol="RELIANCE",
            side="buy",
            quantity=10,
            broker_name=broker_name,
            broker_order_id=broker_order_id,
            status="submitted",
        )
        db.add(order)
        await db.flush()

        trade = Trade(
            order_id=order.id,
            symbol="RELIANCE",
            side="buy",
            quantity=10,
            price=2500.0,
            status="pending_confirmation",
        )
        db.add(trade)
        await db.commit()
        await db.refresh(trade)
        return trade.id, order.id


async def test_reconcile_marks_a_complete_broker_order_as_filled(db_session_factory):
    trade_id, _order_id = await _make_pending_trade(db_session_factory)
    adapter = _FakeAdapter(
        "zerodha",
        [
            BrokerOrder(
                broker_order_id="OID1",
                symbol="RELIANCE",
                side=OrderSide.BUY,
                quantity=10,
                status="COMPLETE",
                average_price=2503.5,
            )
        ],
    )

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 1
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "filled"
        assert trade.fill_price == 2503.5
        assert trade.confirmed_at is not None

        audit_rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "trade.reconciled")))
            .scalars()
            .all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].details["outcome"] == "filled"
    assert audit_rows[0].details["broker_order_id"] == "OID1"


async def test_reconcile_marks_a_rejected_broker_order_as_rejected_without_a_fill_price(
    db_session_factory,
):
    trade_id, _ = await _make_pending_trade(db_session_factory, broker_order_id="OID2")
    adapter = _FakeAdapter(
        "zerodha",
        [
            BrokerOrder(
                broker_order_id="OID2",
                symbol="RELIANCE",
                side=OrderSide.BUY,
                quantity=10,
                status="REJECTED",
                average_price=None,
            )
        ],
    )

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 1
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "rejected"
        assert trade.fill_price is None
        assert trade.confirmed_at is not None


async def test_reconcile_leaves_a_still_open_broker_order_pending(db_session_factory):
    trade_id, _ = await _make_pending_trade(db_session_factory, broker_order_id="OID3")
    adapter = _FakeAdapter(
        "zerodha",
        [
            BrokerOrder(
                broker_order_id="OID3",
                symbol="RELIANCE",
                side=OrderSide.BUY,
                quantity=10,
                status="OPEN",
                average_price=None,
            )
        ],
    )

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 0
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "pending_confirmation"
        assert trade.confirmed_at is None


async def test_reconcile_leaves_a_trade_untouched_when_its_order_is_missing_from_the_book(
    db_session_factory,
):
    trade_id, _ = await _make_pending_trade(db_session_factory, broker_order_id="OID-NOT-RETURNED")
    adapter = _FakeAdapter("zerodha", [])

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 0
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "pending_confirmation"


async def test_reconcile_ignores_trades_routed_through_a_different_broker(db_session_factory):
    trade_id, _ = await _make_pending_trade(
        db_session_factory, broker_name="upstox", broker_order_id="OID4"
    )
    # Only zerodha is "configured" right now -- this codebase only ever
    # has one broker adapter active at a time.
    adapter = _FakeAdapter(
        "zerodha",
        [
            BrokerOrder(
                broker_order_id="OID4",
                symbol="RELIANCE",
                side=OrderSide.BUY,
                quantity=10,
                status="COMPLETE",
                average_price=2500.0,
            )
        ],
    )

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 0
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "pending_confirmation"
    # The upstox-routed trade must never even trigger a zerodha order-book
    # fetch when there's nothing of zerodha's pending.
    assert adapter.calls == 0


async def test_reconcile_is_a_noop_with_nothing_pending(db_session_factory):
    adapter = _FakeAdapter("zerodha", [])
    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)
    assert reconciled == 0
    assert adapter.calls == 0


async def test_reconcile_survives_a_broker_order_book_fetch_failure(db_session_factory):
    trade_id, _ = await _make_pending_trade(db_session_factory, broker_order_id="OID5")
    adapter = _FailingAdapter()

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 0
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "pending_confirmation"


async def test_reconcile_skips_a_trade_whose_order_has_no_broker_order_id(db_session_factory):
    trade_id, _ = await _make_pending_trade(db_session_factory, broker_order_id=None)
    adapter = _FakeAdapter("zerodha", [])

    async with db_session_factory() as db:
        reconciled = await reconcile_pending_trades(db, adapter)

    assert reconciled == 0
    async with db_session_factory() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "pending_confirmation"
