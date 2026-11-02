"""What the Paper Desk's tabs read: a fill says when it was a manual close, and a signal says
when it was detected and when it was acted on."""

import uuid
from datetime import UTC, datetime, timedelta

from src.core.roles import Role
from src.models.daily_signal import DailySignal
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


async def _headers(client, make_user) -> dict[str, str]:
    await make_user("desk-reader@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "desk-reader@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _subscription(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="DeskFields", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add_all(
            [
                PaperFill(
                    subscription_id=subscription.id,
                    symbol="RELIANCE",
                    side="buy",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=5,
                    filled_quantity=5,
                    avg_fill_price=2500.0,
                    fully_filled=True,
                    data_source="real",
                    created_at=NOW,
                ),
                PaperFill(
                    subscription_id=subscription.id,
                    symbol="RELIANCE",
                    side="sell",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=5,
                    filled_quantity=5,
                    avg_fill_price=2510.0,
                    fully_filled=True,
                    data_source="real",
                    administrative=True,
                    created_at=NOW + timedelta(minutes=5),
                ),
            ]
        )
        db.add_all(
            [
                DailySignal(
                    subscription_id=subscription.id,
                    symbol="RELIANCE",
                    signal_type="BUY",
                    reference_price=2495.0,
                    consumed=True,
                    consumed_at=NOW + timedelta(minutes=1),
                    generated_at=NOW - timedelta(hours=2),
                ),
                DailySignal(
                    subscription_id=subscription.id,
                    symbol="RELIANCE",
                    signal_type="SELL",
                    reference_price=2520.0,
                    generated_at=NOW + timedelta(days=1),
                ),
            ]
        )
        await db.commit()
        return subscription.id


async def test_a_fill_says_whether_it_was_a_manual_close(client, make_user, db_session_factory):
    headers = await _headers(client, make_user)
    subscription = await _subscription(db_session_factory)

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription}/fills", headers=headers
    )

    assert resp.status_code == 200
    assert [(f["side"], f["administrative"]) for f in resp.json()] == [
        ("buy", False),
        ("sell", True),
    ]


async def test_a_signal_says_when_it_was_detected_and_when_it_was_acted_on(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    subscription = await _subscription(db_session_factory)

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription}/signals", headers=headers
    )

    assert resp.status_code == 200
    by_type = {s["signal_type"]: s for s in resp.json()}
    assert by_type["BUY"]["consumed"] is True
    assert datetime.fromisoformat(by_type["BUY"]["generated_at"]) == NOW - timedelta(hours=2)
    assert datetime.fromisoformat(by_type["BUY"]["consumed_at"]) == NOW + timedelta(minutes=1)
    assert by_type["SELL"]["consumed"] is False
    assert by_type["SELL"]["consumed_at"] is None
    assert datetime.fromisoformat(by_type["SELL"]["generated_at"]) == NOW + timedelta(days=1)
