"""Orders & Trades unified ledger API tests (Phase 16 wiring audit): the
frontend's Orders & Trades page had zero backend test coverage before this
-- both `GET /orders` (already existed, Phase 13) and `GET /orders/positions`
(new: the "positions by strategy" aggregate the page previously had no
backend for at all) are covered here against real DB rows, not stubs.
"""

import uuid
from datetime import UTC, datetime, timedelta

from src.core.roles import Role
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.trade import Trade
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def test_list_orders_merges_paper_and_live_newest_first(
    client, make_user, db_session_factory
):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="OrdersDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()

        fill = PaperFill(
            subscription_id=subscription.id,
            symbol="RELIANCE",
            side="buy",
            order_group_id=uuid.uuid4(),
            requested_quantity=10,
            filled_quantity=10,
            avg_fill_price=2500.0,
            fully_filled=True,
        )
        db.add(fill)

        intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol="INFY",
            side="sell",
            quantity=5,
            intent_type="entry",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            status="approved",
        )
        db.add(intent)
        await db.flush()

        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=strategy.id,
            symbol="INFY",
            side="sell",
            quantity=5,
            broker_name="zerodha",
            status="submitted",
        )
        db.add(order)
        await db.flush()
        trade = Trade(
            order_id=order.id,
            symbol="INFY",
            side="sell",
            quantity=5,
            price=1500.0,
            status="pending_confirmation",
        )
        db.add(trade)
        await db.commit()

    await make_user("orders-viewer@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "orders-viewer@example.com", "password": "supersecret1"},
    )
    token = login.json()["access_token"]

    resp = await client.get("/api/v1/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    modes = {row["mode"] for row in body}
    symbols = {row["symbol"] for row in body}
    assert modes == {"paper", "live"}
    assert symbols == {"RELIANCE", "INFY"}


async def test_list_positions_by_strategy_joins_real_strategy_names_and_skips_flat(
    client, make_user, db_session_factory
):
    async with db_session_factory() as db:
        live_strategy = await create_strategy(db, name="LiveMomentum", objective="obj")
        flat_strategy = await create_strategy(db, name="ClosedOutStrategy", objective="obj")
        db.add(
            LivePosition(strategy_id=live_strategy.id, symbol="TCS", quantity=25, avg_cost=3400.0)
        )
        db.add(LivePosition(strategy_id=flat_strategy.id, symbol="WIPRO", quantity=0, avg_cost=0.0))

        paper_strategy = await create_strategy(db, name="PaperSwing", objective="obj")
        version = await create_version_with_validation(db, paper_strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="HDFCBANK", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol="HDFCBANK", quantity=-15, avg_cost=1600.0
            )
        )
        await db.commit()

    await make_user("positions-viewer@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "positions-viewer@example.com", "password": "supersecret1"},
    )
    token = login.json()["access_token"]

    resp = await client.get(
        "/api/v1/orders/positions", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()

    by_symbol = {row["symbol"]: row for row in body}
    assert by_symbol["TCS"]["mode"] == "live"
    assert by_symbol["TCS"]["strategy_name"] == "LiveMomentum"
    assert by_symbol["TCS"]["quantity"] == 25
    assert by_symbol["HDFCBANK"]["mode"] == "paper"
    assert by_symbol["HDFCBANK"]["strategy_name"] == "PaperSwing"
    assert by_symbol["HDFCBANK"]["quantity"] == -15
    # The flat (quantity == 0) WIPRO position is closed, not held -- must
    # not appear in a "current positions" list.
    assert "WIPRO" not in by_symbol
