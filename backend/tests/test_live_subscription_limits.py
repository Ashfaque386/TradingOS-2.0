"""Tightening a live subscription's limits, or switching it off: which way each limit may move, what
protects an open position, who may do it, and what is audited (and what is not).
"""

import uuid
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.orchestration.live_subscription_limits import (
    LimitChange,
    LimitChangeRefused,
    plan_deactivation,
    plan_limit_changes,
    switch_off,
)
from src.orchestration.strategies import create_strategy

PATH = "/api/v1/live-trading/subscriptions"


def _sub(**over) -> LiveTradingSubscription:
    values = dict(
        strategy_id=uuid.uuid4(),
        symbol="RELIANCE",
        broker_name="zerodha",
        is_active=True,
        max_intents_per_window=5,
        rate_limit_window_minutes=60,
        max_notional_per_intent=50_000.0,
        intent_expiry_seconds=90,
    )
    values.update(over)
    return LiveTradingSubscription(**values)


def _plan(requested, *, held_quantity=0, held_cost=0.0, **over):
    return plan_limit_changes(
        _sub(**over), requested, held_quantity=held_quantity, held_cost=held_cost
    )


# ---- the rules, on their own ----


def test_a_stricter_value_for_each_limit_is_a_change_with_its_before_and_after():
    changes = _plan(
        {
            "max_intents_per_window": 3,
            "rate_limit_window_minutes": 120,
            "max_notional_per_intent": 20_000.0,
            "intent_expiry_seconds": 30,
        }
    )

    assert {c.field: (c.before, c.after) for c in changes} == {
        "max_intents_per_window": (5, 3),
        "rate_limit_window_minutes": (60, 120),
        "max_notional_per_intent": (50_000.0, 20_000.0),
        "intent_expiry_seconds": (90, 30),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "max_intents_per_window",
            9,
            "orders per window is 5 and can only be lowered here, not set to 9.",
        ),
        (
            "max_notional_per_intent",
            60_000.0,
            "largest order in rupees is 50000 and can only be lowered here, not set to 60000.",
        ),
        (
            "intent_expiry_seconds",
            120,
            "seconds an order lives is 90 and can only be lowered here, not set to 120.",
        ),
        (  # a shorter window counts fewer orders
            "rate_limit_window_minutes",
            30,
            "window in minutes is 60 and can only be raised here, not set to 30.",
        ),
    ],
)
def test_a_looser_value_is_refused_and_says_to_enrol_again(field, value, message):
    with pytest.raises(LimitChangeRefused) as refused:
        _plan({field: value})

    assert str(refused.value) == (
        f"{message} Making a limit looser changes what the strategy is trusted with: "
        "enrol it again."
    )


def test_a_value_that_is_already_the_value_is_not_a_change_and_nothing_at_all_is_refused():
    changes = _plan({"max_intents_per_window": 5, "intent_expiry_seconds": 30})
    assert [c.field for c in changes] == ["intent_expiry_seconds"]

    with pytest.raises(LimitChangeRefused) as refused:
        _plan({"max_intents_per_window": 5, "rate_limit_window_minutes": 60})

    assert str(refused.value) == "nothing to change: every value is already what it is"


def test_an_order_cap_below_what_is_held_is_refused_because_it_would_block_the_exit():
    with pytest.raises(LimitChangeRefused) as refused:
        _plan({"max_notional_per_intent": 15_000.0}, held_quantity=10, held_cost=20_000.0)

    assert str(refused.value) == (
        "this strategy holds 10 shares of RELIANCE (about ₹20,000 at cost), and an order cap of "
        "₹15,000 would block the order that closes them. Lower it after the position is closed."
    )
    assert _plan({"max_notional_per_intent": 25_000.0}, held_quantity=10, held_cost=20_000.0)
    assert _plan({"max_notional_per_intent": 1_000.0}, held_quantity=0, held_cost=0.0)


def test_one_share_held_is_enough_to_protect_and_a_cap_equal_to_the_cost_is_allowed():
    with pytest.raises(LimitChangeRefused, match="holds 1 shares"):
        _plan({"max_notional_per_intent": 50.0}, held_quantity=1, held_cost=100.0)

    assert _plan({"max_notional_per_intent": 20_000.0}, held_quantity=10, held_cost=20_000.0)


def test_the_other_limits_are_not_held_back_by_a_position():
    assert _plan({"max_intents_per_window": 1}, held_quantity=-10, held_cost=20_000.0)


def test_a_switched_off_subscription_has_nothing_to_tighten():
    with pytest.raises(LimitChangeRefused) as refused:
        _plan({"max_intents_per_window": 1}, is_active=False)

    assert str(refused.value) == (
        "the RELIANCE subscription is switched off: nothing is left to tighten. "
        "Enrol the strategy again to trade it."
    )


def test_a_planned_change_is_a_record_that_cannot_be_altered():
    change = LimitChange("max_intents_per_window", 5, 2)

    with pytest.raises(FrozenInstanceError):
        change.after = 1  # type: ignore[misc]

    assert not hasattr(change, "__dict__")  # no room for anything but its three fields


def test_switching_off_needs_an_active_subscription_that_holds_nothing():
    plan_deactivation(_sub(), held_quantity=0)  # allowed: no error

    with pytest.raises(LimitChangeRefused) as off:
        plan_deactivation(_sub(is_active=False), held_quantity=0)
    with pytest.raises(LimitChangeRefused) as held:
        plan_deactivation(_sub(), held_quantity=-7)

    assert str(off.value) == "the RELIANCE subscription is already switched off"
    assert str(held.value) == (
        "this strategy holds 7 shares of RELIANCE, and a switched-off subscription generates no "
        "exit and no stop-loss order, so they would be left unwatched. Turn autonomous trading "
        "off instead (it can be turned back on), and switch the subscription off once the "
        "position is closed."
    )


@pytest.mark.parametrize("was_on", [True, False])
def test_switching_off_ends_autonomy_and_says_whether_it_was_on(was_on):
    subscription = _sub(
        autonomous_trading_enabled=was_on, autonomy_enabled_by="someone" if was_on else None
    )

    assert switch_off(subscription) is was_on

    assert subscription.is_active is False
    assert subscription.autonomous_trading_enabled is False
    assert subscription.autonomy_enabled_by is None
    assert subscription.autonomy_enabled_at is None


# ---- the route ----


async def _login(client, make_user, role, email="actor@example.com") -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed(db_session_factory, *, held: tuple[int, float] | None = None, **over) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"Live-{uuid.uuid4().hex[:6]}", objective="obj")
        subscription = LiveTradingSubscription(
            strategy_id=strategy.id, symbol="RELIANCE", broker_name="zerodha", **over
        )
        db.add(subscription)
        if held:
            db.add(
                LivePosition(
                    strategy_id=strategy.id, symbol="RELIANCE", quantity=held[0], avg_cost=held[1]
                )
            )
        await db.commit()
        return subscription.id


async def _audit(db_session_factory, subscription_id, action) -> list[AuditLog]:
    async with db_session_factory() as db:
        rows = await db.execute(
            select(AuditLog).where(
                AuditLog.entity_id == str(subscription_id), AuditLog.action == action
            )
        )
        return list(rows.scalars().all())


async def _patch(client, headers, subscription_id, body):
    return await client.patch(f"{PATH}/{subscription_id}", json=body, headers=headers)


@pytest.mark.parametrize("role", [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER])
async def test_the_administrator_and_the_risk_manager_may_tighten_a_limit(
    client, make_user, db_session_factory, role
):
    headers = await _login(client, make_user, role)
    sub = await _seed(db_session_factory)

    resp = await _patch(
        client, headers, sub, {"max_intents_per_window": 2, "reason": "too many orders on the open"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription"]["max_intents_per_window"] == 2
    assert body["changes"] == [{"field": "max_intents_per_window", "before": 5.0, "after": 2.0}]


@pytest.mark.parametrize("role", [Role.PORTFOLIO_MANAGER, Role.READ_ONLY_AUDITOR])
async def test_the_other_roles_may_not(client, make_user, db_session_factory, role):
    headers = await _login(client, make_user, role)
    sub = await _seed(db_session_factory)

    resp = await _patch(client, headers, sub, {"max_intents_per_window": 2, "reason": "because"})

    assert resp.status_code == 403


async def test_a_tightening_is_audited_with_the_reason_and_each_value_before_and_after(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.RISK_MANAGER)
    sub = await _seed(db_session_factory)

    await _patch(
        client,
        headers,
        sub,
        {"max_notional_per_intent": 10_000, "intent_expiry_seconds": 45, "reason": "volatile day"},
    )

    (entry,) = await _audit(db_session_factory, sub, "live_trading_subscription.limits_tightened")
    assert entry.details["reason"] == "volatile day"
    assert entry.details["changes"] == {
        "max_notional_per_intent": {"before": 50_000.0, "after": 10_000.0},
        "intent_expiry_seconds": {"before": 90, "after": 45},
    }


async def test_a_change_that_changes_nothing_is_refused_and_not_audited(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory)

    resp = await _patch(client, headers, sub, {"max_intents_per_window": 5, "reason": "no change"})

    assert resp.status_code == 409
    assert "nothing to change" in resp.json()["detail"]
    assert await _audit(db_session_factory, sub, "live_trading_subscription.limits_tightened") == []


async def test_a_looser_limit_is_refused_and_leaves_the_subscription_as_it_was(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory)

    resp = await _patch(client, headers, sub, {"max_notional_per_intent": 90_000, "reason": "more"})

    assert resp.status_code == 409
    assert "can only be lowered here" in resp.json()["detail"]
    fetched = (await client.get(f"{PATH}/{sub}", headers=headers)).json()
    assert fetched["max_notional_per_intent"] == 50_000.0


async def test_an_order_cap_below_the_position_held_is_refused_with_the_reason(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory, held=(10, 2_000.0))

    resp = await _patch(
        client, headers, sub, {"max_notional_per_intent": 5_000, "reason": "tighter"}
    )

    assert resp.status_code == 409
    assert "holds 10 shares of RELIANCE" in resp.json()["detail"]


async def test_switching_a_subscription_off_clears_autonomy_and_is_audited(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.RISK_MANAGER)
    sub = await _seed(
        db_session_factory, autonomous_trading_enabled=True, autonomy_enabled_by="someone"
    )

    resp = await _patch(
        client, headers, sub, {"deactivate": True, "reason": "retiring this symbol"}
    )

    assert resp.status_code == 200
    body = resp.json()["subscription"]
    assert body["is_active"] is False
    assert body["autonomous_trading_enabled"] is False
    assert body["autonomy_enabled_by"] is None
    (entry,) = await _audit(db_session_factory, sub, "live_trading_subscription.deactivated")
    assert entry.details["reason"] == "retiring this symbol"
    assert entry.details["autonomy_was_on"] is True


async def test_it_cannot_be_switched_off_twice_or_tightened_after(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory)
    await _patch(client, headers, sub, {"deactivate": True, "reason": "done with it"})

    again = await _patch(client, headers, sub, {"deactivate": True, "reason": "again"})
    tighten = await _patch(client, headers, sub, {"max_intents_per_window": 1, "reason": "after"})

    assert again.status_code == 409 and "already switched off" in again.json()["detail"]
    assert tighten.status_code == 409 and "switched off" in tighten.json()["detail"]


async def test_a_subscription_holding_shares_is_not_switched_off_because_nothing_would_watch_them(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory, held=(-7, 100.0), autonomous_trading_enabled=True)

    resp = await _patch(client, headers, sub, {"deactivate": True, "reason": "retiring it"})

    assert resp.status_code == 409
    assert "would be left unwatched" in resp.json()["detail"]
    fetched = (await client.get(f"{PATH}/{sub}", headers=headers)).json()
    assert fetched["is_active"] is True and fetched["autonomous_trading_enabled"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "no change named"},
        {"deactivate": True, "max_intents_per_window": 1, "reason": "both kinds"},
        {"max_intents_per_window": 0, "reason": "zero"},
        {"intent_expiry_seconds": 301, "reason": "too long"},
        {"max_intents_per_window": 1, "reason": "no"},
        {"max_intents_per_window": 1},
    ],
)
async def test_a_request_that_does_not_make_sense_is_a_422(
    client, make_user, db_session_factory, body
):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)
    sub = await _seed(db_session_factory)

    assert (await _patch(client, headers, sub, body)).status_code == 422


async def test_an_unknown_subscription_is_a_404(client, make_user):
    headers = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR)

    resp = await _patch(
        client, headers, uuid.uuid4(), {"max_intents_per_window": 1, "reason": "x y z"}
    )

    assert resp.status_code == 404
