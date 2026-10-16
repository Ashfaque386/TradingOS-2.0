"""Operator controls for paper-trading subscriptions: list, deactivate, activate, close a position.

A subscription used to be unstoppable from the app (stopping one meant editing the database, which
also left any open position unmanaged -- no stop-loss watching it -- while the dashboards kept
counting it). These tests drive the real routes, roles included, through to the ledger and the
audit trail.
"""

import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.audit.service import write_audit_entry  # noqa: F401  (the controls write through it)
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data.price_provider import get_signal_price_provider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.main import app
from src.models.audit_log import AuditLog
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription

SYMBOL = "DEMOSTOCK"


@pytest.fixture(autouse=True)
def _providers(redis_client):
    """The signal price provider is synthetic for this file only (the cycle needs the known
    default-seeded series); redis is the per-test connection, as in the other paper tests."""
    app.dependency_overrides[get_signal_price_provider] = lambda: FakeDailyPriceProvider()
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_signal_price_provider, None)
    app.dependency_overrides.pop(get_redis, None)


@pytest_asyncio.fixture(autouse=True)
async def _no_ticks_left_by_an_earlier_test(redis_client):
    """The redis stream is shared across tests: a fresh tick published by one would let the next
    close "find a live price" it never published."""
    await redis_client.delete(tick_stream_key(SYMBOL))
    yield
    await redis_client.delete(tick_stream_key(SYMBOL))


async def _headers(client, make_user, email: str, role: Role = Role.SYSTEM_ADMINISTRATOR) -> dict:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _enrol(client, headers, *, symbol: str = SYMBOL) -> str:
    created = await client.post(
        "/api/v1/strategies",
        json={"name": f"Ctl-{uuid.uuid4().hex[:6]}", "objective": "o"},
        headers=headers,
    )
    version_id = created.json()["versions"][0]["id"]
    resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={"strategy_version_id": version_id, "symbol": symbol},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _open_position(client, headers, subscription_id: str) -> dict:
    signals = await client.post(
        "/api/v1/paper-trading/daily-signal-run", json={"as_of": "2026-09-09"}, headers=headers
    )
    reference = next(s for s in signals.json() if s["subscription_id"] == subscription_id)[
        "reference_price"
    ]
    fill = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference},
        headers=headers,
    )
    assert fill.json()["side"] == "buy"
    position = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=headers
    )
    return position.json()


async def _fresh_tick(
    redis_client, price: float, *, age_seconds: float = 0.0, symbol: str = SYMBOL
):
    await redis_client.delete(tick_stream_key(symbol))
    await publish_tick(
        redis_client,
        Tick(
            symbol=symbol,
            price=price,
            timestamp_ms=int((time.time() - age_seconds) * 1000),
            source="real",
        ),
    )


async def _audit_actions(db_session_factory) -> list[str]:
    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(AuditLog.action).order_by(AuditLog.sequence))).scalars().all()
        )
    return list(rows)


# ---- list ----


async def test_the_list_shows_state_position_and_deactivation_details(
    client, make_user, redis_client
):
    headers = await _headers(client, make_user, "list-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    await _fresh_tick(redis_client, position["avg_cost"] * 1.02)

    rows = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == sub_id and row["symbol"] == SYMBOL
    assert row["quantity"] == position["quantity"] > 0
    assert row["avg_cost"] == pytest.approx(position["avg_cost"])
    assert row["managed"] is True
    assert row["last_tick"]["source"] == "real" and row["last_tick"]["age_seconds"] < 30
    assert row["unrealized_pnl"] == pytest.approx(
        (row["last_tick"]["price"] - row["avg_cost"]) * row["quantity"]
    )
    assert row["deactivated_at"] is None and row["deactivation_reason"] is None
    # No broker is connected in the test environment and synthetic mode is off.
    assert row["state"] == "halted_no_data" and "no broker" in row["state_reason"]


async def test_the_list_filters_by_activity_symbol_and_state(client, make_user):
    headers = await _headers(client, make_user, "filter-admin@example.com")
    first = await _enrol(client, headers, symbol="AAA")
    await _enrol(client, headers, symbol="BBB")
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{first}/deactivate",
        json={"reason": "filter test"},
        headers=headers,
    )

    def symbols(rows):
        return sorted(r["symbol"] for r in rows)

    base = "/api/v1/paper-trading/subscriptions"
    assert symbols((await client.get(base, headers=headers)).json()) == ["AAA", "BBB"]
    assert symbols((await client.get(base + "?active=false", headers=headers)).json()) == ["AAA"]
    assert symbols((await client.get(base + "?active=true", headers=headers)).json()) == ["BBB"]
    assert symbols((await client.get(base + "?symbol=bbb", headers=headers)).json()) == ["BBB"]
    assert symbols((await client.get(base + "?state=inactive", headers=headers)).json()) == ["AAA"]
    assert (await client.get(base + "?state=active", headers=headers)).json() == []  # BBB is halted


async def test_an_unmanaged_position_is_reported_as_such(client, make_user, db_session_factory):
    headers = await _headers(client, make_user, "unm-admin@example.com")
    sub_id = await _enrol(client, headers)
    await _open_position(client, headers, sub_id)
    async with db_session_factory() as db:
        sub = await db.get(PaperTradingSubscription, uuid.UUID(sub_id))
        sub.is_active = False
        await db.commit()

    row = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()[0]

    assert row["state"] == "inactive" and row["managed"] is False and row["quantity"] > 0


async def test_every_role_can_read_the_list_but_only_operators_can_change_things(client, make_user):
    admin = await _headers(client, make_user, "roles-admin@example.com")
    sub_id = await _enrol(client, admin)
    auditor = await _headers(client, make_user, "roles-auditor@example.com", Role.READ_ONLY_AUDITOR)
    base = f"/api/v1/paper-trading/subscriptions/{sub_id}"

    assert (
        await client.get("/api/v1/paper-trading/subscriptions", headers=auditor)
    ).status_code == 200
    assert (
        await client.post(base + "/deactivate", json={"reason": "no way"}, headers=auditor)
    ).status_code == 403
    assert (await client.post(base + "/activate", headers=auditor)).status_code == 403
    assert (
        await client.post(base + "/close-position", json={"reason": "no way"}, headers=auditor)
    ).status_code == 403


# ---- deactivate ----


async def test_deactivating_a_flat_subscription_records_who_when_and_why(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user, "deact-admin@example.com")
    sub_id = await _enrol(client, headers)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "demo symbol no broker can quote"},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription"]["is_active"] is False
    assert body["closed"] is None and body["left_unmanaged_quantity"] == 0
    async with db_session_factory() as db:
        sub = await db.get(PaperTradingSubscription, uuid.UUID(sub_id))
        assert sub.is_active is False
        assert sub.deactivated_by is not None and sub.deactivated_at is not None
        assert sub.deactivation_reason == "demo symbol no broker can quote"
    assert "paper_trading.subscription_deactivated" in await _audit_actions(db_session_factory)


async def test_deactivating_twice_is_refused_not_answered_200(client, make_user):
    headers = await _headers(client, make_user, "twice-admin@example.com")
    sub_id = await _enrol(client, headers)
    url = f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate"
    assert (
        await client.post(url, json={"reason": "first time"}, headers=headers)
    ).status_code == 200

    again = await client.post(url, json={"reason": "second time"}, headers=headers)

    assert again.status_code == 409 and "already inactive" in again.json()["detail"]


async def test_deactivating_with_an_open_position_demands_a_decision(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user, "open-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "stop it"},
        headers=headers,
    )

    assert resp.status_code == 409
    assert (
        "unmanaged" in resp.json()["detail"] and str(position["quantity"]) in resp.json()["detail"]
    )
    async with db_session_factory() as db:  # nothing changed
        assert (await db.get(PaperTradingSubscription, uuid.UUID(sub_id))).is_active is True


async def test_leave_unmanaged_deactivates_and_says_how_much_is_left_open(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user, "leave-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "accepting the risk", "on_open_position": "leave_unmanaged"},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["left_unmanaged_quantity"] == position["quantity"]
    assert resp.json()["closed"] is None
    async with db_session_factory() as db:
        pos = (await db.execute(select(PaperPosition))).scalar_one()
        assert pos.quantity == position["quantity"]  # untouched


async def test_close_then_deactivate_closes_at_a_fresh_tick_and_leaves_no_position(
    client, make_user, redis_client, db_session_factory
):
    headers = await _headers(client, make_user, "closedeact-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    await _fresh_tick(redis_client, position["avg_cost"])

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "done with it", "on_open_position": "close"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subscription"]["is_active"] is False
    assert body["closed"]["closed"] is True and body["closed"]["position"]["quantity"] == 0
    assert body["closed"]["fill"]["side"] == "sell"
    assert body["left_unmanaged_quantity"] == 0
    actions = await _audit_actions(db_session_factory)
    assert "paper_trading.position_closed" in actions
    assert "paper_trading.subscription_deactivated" in actions


async def test_a_failed_close_leaves_the_subscription_active(client, make_user, db_session_factory):
    """No fresh tick and no price supplied: the close fails, so the deactivation must not happen."""
    headers = await _headers(client, make_user, "failclose-admin@example.com")
    sub_id = await _enrol(client, headers)
    await _open_position(client, headers, sub_id)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "try", "on_open_position": "close"},
        headers=headers,
    )

    assert resp.status_code == 422 and "supply `price`" in resp.json()["detail"]
    async with db_session_factory() as db:
        assert (await db.get(PaperTradingSubscription, uuid.UUID(sub_id))).is_active is True


async def test_an_unknown_subscription_is_404_for_every_control(client, make_user):
    headers = await _headers(client, make_user, "unknown-admin@example.com")
    missing = uuid.uuid4()
    base = f"/api/v1/paper-trading/subscriptions/{missing}"

    assert (
        await client.post(base + "/deactivate", json={"reason": "abc"}, headers=headers)
    ).status_code == 404
    assert (await client.post(base + "/activate", headers=headers)).status_code == 404
    assert (
        await client.post(base + "/close-position", json={"reason": "abc"}, headers=headers)
    ).status_code == 404


# ---- activate ----


async def test_activating_resets_the_cursor_so_stale_ticks_are_not_replayed(
    client, make_user, redis_client, db_session_factory
):
    headers = await _headers(client, make_user, "act-admin@example.com")
    sub_id = await _enrol(client, headers)
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "pause"},
        headers=headers,
    )
    await redis_client.delete(tick_stream_key(SYMBOL))
    for price in (100.0, 101.0, 102.0):  # published while it was off
        entry_id = await publish_tick(
            redis_client, Tick(symbol=SYMBOL, price=price, timestamp_ms=int(time.time() * 1000))
        )

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/activate", headers=headers
    )

    assert resp.status_code == 200 and resp.json()["is_active"] is True
    async with db_session_factory() as db:
        sub = await db.get(PaperTradingSubscription, uuid.UUID(sub_id))
        assert sub.tick_cursor == entry_id  # the newest tick, not "0"
        assert sub.deactivated_at is None and sub.deactivated_by is None
        assert sub.deactivation_reason is None
    assert "paper_trading.subscription_activated" in await _audit_actions(db_session_factory)


async def test_activating_with_an_empty_stream_starts_from_the_beginning(
    client, make_user, redis_client, db_session_factory
):
    headers = await _headers(client, make_user, "actempty-admin@example.com")
    sub_id = await _enrol(client, headers)
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "pause"},
        headers=headers,
    )
    await redis_client.delete(tick_stream_key(SYMBOL))

    await client.post(f"/api/v1/paper-trading/subscriptions/{sub_id}/activate", headers=headers)

    async with db_session_factory() as db:
        assert (await db.get(PaperTradingSubscription, uuid.UUID(sub_id))).tick_cursor == "0"


async def test_activating_an_active_subscription_is_refused(client, make_user):
    headers = await _headers(client, make_user, "actact-admin@example.com")
    sub_id = await _enrol(client, headers)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/activate", headers=headers
    )

    assert resp.status_code == 409 and "already active" in resp.json()["detail"]


async def test_activating_is_refused_when_another_active_subscription_covers_the_same_symbol(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user, "dupact-admin@example.com")
    first = await _enrol(client, headers)
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{first}/deactivate",
        json={"reason": "swap"},
        headers=headers,
    )
    async with db_session_factory() as db:  # a second, active one for the same version + symbol
        original = await db.get(PaperTradingSubscription, uuid.UUID(first))
        db.add(
            PaperTradingSubscription(
                strategy_version_id=original.strategy_version_id, symbol=original.symbol
            )
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{first}/activate", headers=headers
    )

    assert resp.status_code == 409 and "already exists" in resp.json()["detail"]


async def test_enrolling_a_duplicate_active_subscription_is_refused(client, make_user):
    headers = await _headers(client, make_user, "dupenrol-admin@example.com")
    created = await client.post(
        "/api/v1/strategies", json={"name": "DupEnrol", "objective": "o"}, headers=headers
    )
    version_id = created.json()["versions"][0]["id"]
    body = {"strategy_version_id": version_id, "symbol": SYMBOL}
    url = "/api/v1/paper-trading/subscriptions"
    assert (await client.post(url, json=body, headers=headers)).status_code == 201

    resp = await client.post(url, json=body, headers=headers)

    assert resp.status_code == 409 and "already exists" in resp.json()["detail"]
    other = await client.post(url, json={**body, "symbol": "OTHER"}, headers=headers)
    assert other.status_code == 201  # a different symbol is fine


# ---- close ----


async def test_closing_at_a_live_tick_goes_through_the_gate_and_the_ledger(
    client, make_user, redis_client, db_session_factory
):
    headers = await _headers(client, make_user, "close-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    await _fresh_tick(redis_client, position["avg_cost"] * 1.01)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "taking the profit"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["closed"] is True and body["remaining_quantity"] == 0
    assert body["administrative"] is False
    assert body["fill"]["side"] == "sell"
    assert body["fill"]["filled_quantity"] == position["quantity"]
    assert body["price_basis"].startswith("live tick")
    assert body["data_source"] == "mixed"  # a real tick walked through the synthetic book
    assert body["position"]["quantity"] == 0
    assert body["position"]["realized_pnl"] > position["realized_pnl"]  # it was a winner
    async with db_session_factory() as db:
        fills = (await db.execute(select(PaperFill).order_by(PaperFill.created_at))).scalars().all()
        assert [f.side for f in fills] == ["buy", "sell"]


async def test_closing_a_flat_subscription_is_refused(client, make_user):
    headers = await _headers(client, make_user, "flat-admin@example.com")
    sub_id = await _enrol(client, headers)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "nothing there"},
        headers=headers,
    )

    assert resp.status_code == 409 and "no open position" in resp.json()["detail"]


async def test_a_stale_tick_is_not_a_price_and_a_supplied_price_is_labelled(
    client, make_user, redis_client
):
    headers = await _headers(client, make_user, "stale-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    await _fresh_tick(redis_client, position["avg_cost"], age_seconds=3600)  # an hour old
    url = f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position"

    stale = await client.post(url, json={"reason": "market is shut"}, headers=headers)
    assert stale.status_code == 422
    assert "old" in stale.json()["detail"] and "supply `price`" in stale.json()["detail"]

    supplied = await client.post(
        url, json={"reason": "market is shut", "price": position["avg_cost"]}, headers=headers
    )
    assert supplied.status_code == 200, supplied.text
    assert supplied.json()["price_basis"] == "supplied by the operator"
    assert supplied.json()["data_source"] == "mixed"  # supplied price through a synthetic book


async def test_no_tick_at_all_asks_for_a_price(client, make_user, redis_client):
    headers = await _headers(client, make_user, "notick-admin@example.com")
    sub_id = await _enrol(client, headers)
    await _open_position(client, headers, sub_id)
    await redis_client.delete(tick_stream_key(SYMBOL))

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "no feed"},
        headers=headers,
    )

    assert resp.status_code == 422 and "no price has ever been published" in resp.json()["detail"]


async def test_a_tripped_kill_switch_blocks_a_normal_close_but_not_an_administrative_one(
    client, make_user, redis_client
):
    """No carve-out for exits through the gate -- the project's rule. Cleanup has its own path."""
    admin = await _headers(client, make_user, "ks-admin@example.com")
    sub_id = await _enrol(client, admin)
    position = await _open_position(client, admin, sub_id)
    await _fresh_tick(redis_client, position["avg_cost"])
    tripped = await client.post(  # the real drawdown check: 50% down against the default limit
        "/api/v1/kill-switch/paper/check",
        json={"current_equity": 50_000, "peak_equity": 100_000},
        headers=admin,
    )
    assert tripped.status_code == 200 and tripped.json()["tripped"] is True
    url = f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position"

    blocked = await client.post(url, json={"reason": "blocked?"}, headers=admin)
    assert blocked.status_code == 409 and "kill switch" in blocked.json()["detail"]

    forced = await client.post(
        url, json={"reason": "cleanup", "administrative": True}, headers=admin
    )
    assert forced.status_code == 200 and forced.json()["administrative"] is True


async def test_an_administrative_close_needs_the_system_administrator_role(
    client, make_user, redis_client
):
    admin = await _headers(client, make_user, "adm-admin@example.com")
    sub_id = await _enrol(client, admin)
    position = await _open_position(client, admin, sub_id)
    manager = await _headers(client, make_user, "adm-pm@example.com", Role.PORTFOLIO_MANAGER)
    url = f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position"

    denied = await client.post(
        url,
        json={"reason": "cleanup", "administrative": True, "price": position["avg_cost"]},
        headers=manager,
    )

    assert denied.status_code == 403 and "SystemAdministrator" in denied.json()["detail"]


async def test_an_administrative_close_fills_everything_at_the_given_price_and_says_so(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user, "admclose-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    price = round(position["avg_cost"], 2)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "stale demo position", "administrative": True, "price": price},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["administrative"] is True and body["closed"] is True
    assert body["fill"]["filled_quantity"] == position["quantity"]
    assert body["fill"]["avg_fill_price"] == pytest.approx(price)
    assert body["data_source"] == "supplied"  # no order book was involved
    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "paper_trading.position_closed")
            )
        ).scalar_one()
        assert entry.details["administrative"] is True
        assert entry.details["reason"] == "stale demo position"
        assert entry.details["price_basis"] == "supplied by the operator"


async def test_a_partial_close_leaves_the_rest_open(client, make_user, redis_client, monkeypatch):
    """A thin book (2 levels x 20 = 40 shares deep) cannot absorb the whole position."""
    from src.api.routes import paper_trading as routes
    from src.engine.paper_trading.order_book import MockOrderBookProvider

    headers = await _headers(client, make_user, "partial-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    assert position["quantity"] > 40
    monkeypatch.setattr(
        routes, "_ORDER_BOOK_PROVIDER", MockOrderBookProvider(levels=2, quantity_per_level=20)
    )
    await _fresh_tick(redis_client, position["avg_cost"])

    closed = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "big one"},
        headers=headers,
    )

    left = position["quantity"] - 40
    assert closed.status_code == 200, closed.text
    assert closed.json()["closed"] is False and closed.json()["remaining_quantity"] == left
    assert closed.json()["position"]["quantity"] == left
    assert closed.json()["fill"]["fully_filled"] is False
    assert closed.json()["fill"]["filled_quantity"] == 40


async def test_deactivating_with_close_refuses_when_only_part_of_the_position_closes(
    client, make_user, redis_client, db_session_factory, monkeypatch
):
    """The close fills 40 of the position; the rest is still open, so the subscription must stay
    active (deactivating now would leave the remainder unmanaged without anyone choosing that)."""
    from src.api.routes import paper_trading as routes
    from src.engine.paper_trading.order_book import MockOrderBookProvider

    headers = await _headers(client, make_user, "partialdeact-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    monkeypatch.setattr(
        routes, "_ORDER_BOOK_PROVIDER", MockOrderBookProvider(levels=2, quantity_per_level=20)
    )
    await _fresh_tick(redis_client, position["avg_cost"])

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/deactivate",
        json={"reason": "wrap up", "on_open_position": "close"},
        headers=headers,
    )

    assert resp.status_code == 409
    assert "partly closed" in resp.json()["detail"]
    assert f"({position['quantity'] - 40} left)" in resp.json()["detail"]
    async with db_session_factory() as db:
        assert (await db.get(PaperTradingSubscription, uuid.UUID(sub_id))).is_active is True
        pos = (await db.execute(select(PaperPosition))).scalar_one()
        assert pos.quantity == position["quantity"] - 40  # the part that did close stays closed


async def test_a_book_with_no_depth_cannot_close_anything(
    client, make_user, redis_client, db_session_factory, monkeypatch
):
    from src.api.routes import paper_trading as routes
    from src.engine.paper_trading.order_book import MockOrderBookProvider

    headers = await _headers(client, make_user, "nodepth-admin@example.com")
    sub_id = await _enrol(client, headers)
    position = await _open_position(client, headers, sub_id)
    monkeypatch.setattr(routes, "_ORDER_BOOK_PROVIDER", MockOrderBookProvider(levels=0))
    await _fresh_tick(redis_client, position["avg_cost"])

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-position",
        json={"reason": "empty book"},
        headers=headers,
    )

    assert resp.status_code == 409 and "no depth" in resp.json()["detail"]
    async with db_session_factory() as db:  # nothing was recorded
        assert len((await db.execute(select(PaperFill))).scalars().all()) == 1  # just the entry
        pos = (await db.execute(select(PaperPosition))).scalar_one()
        assert pos.quantity == position["quantity"]


# ---- state derivation: what the list says is wrong ----


async def test_missing_history_outranks_a_missing_feed_in_the_state(client, make_user, tmp_path):
    """Both are true in the test environment (no broker, an empty lake): the state names the
    first thing that stops a signal -- history -- not the second."""
    from src.data.price_provider import DataLakePriceProvider

    app.dependency_overrides[get_signal_price_provider] = lambda: DataLakePriceProvider(
        root=tmp_path / "lake"
    )
    headers = await _headers(client, make_user, "hist-admin@example.com")
    await _enrol(client, headers)

    row = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()[0]

    assert row["state"] == "awaiting_history"
    assert "history has not been ingested yet" in row["state_reason"]


async def test_history_is_not_even_looked_up_for_an_inactive_subscription(client, make_user):
    asked: list[str] = []

    class _Recording:
        def daily_bars(self, symbol, *, as_of, lookback_days):
            asked.append(symbol)
            return FakeDailyPriceProvider().daily_bars(
                symbol, as_of=as_of, lookback_days=lookback_days
            )

    app.dependency_overrides[get_signal_price_provider] = lambda: _Recording()
    headers = await _headers(client, make_user, "nolookup-admin@example.com")
    inactive = await _enrol(client, headers, symbol="OFFSTOCK")
    await _enrol(client, headers, symbol="ONSTOCK")
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{inactive}/deactivate",
        json={"reason": "off"},
        headers=headers,
    )

    await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    assert asked == ["ONSTOCK"]  # a lake read per *active* subscription only


async def test_a_deactivated_subscription_does_not_block_enrolling_the_same_symbol_again(
    client, make_user
):
    headers = await _headers(client, make_user, "reenrol-admin@example.com")
    created = await client.post(
        "/api/v1/strategies", json={"name": "ReEnrol", "objective": "o"}, headers=headers
    )
    version_id = created.json()["versions"][0]["id"]
    body = {"strategy_version_id": version_id, "symbol": SYMBOL}
    first = await client.post("/api/v1/paper-trading/subscriptions", json=body, headers=headers)
    await client.post(
        f"/api/v1/paper-trading/subscriptions/{first.json()['id']}/deactivate",
        json={"reason": "swap it"},
        headers=headers,
    )

    second = await client.post("/api/v1/paper-trading/subscriptions", json=body, headers=headers)

    assert second.status_code == 201 and second.json()["id"] != first.json()["id"]


# ---- why there is no price feed ----


def _isolated_store(monkeypatch, tmp_path, *, synthetic: bool = False):
    from cryptography.fernet import Fernet

    from src.core.config import get_settings
    from src.security.secrets_store import SecretsStore, get_secrets_store

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true" if synthetic else "false")
    get_settings.cache_clear()
    get_secrets_store.cache_clear()
    return SecretsStore(tmp_path / "secrets.enc", key)


@pytest.fixture
def _clean_settings():
    from src.core.config import get_settings
    from src.security.secrets_store import get_secrets_store

    yield
    get_settings.cache_clear()
    get_secrets_store.cache_clear()


def test_feed_reason_with_no_broker_and_synthetic_off(monkeypatch, tmp_path, _clean_settings):
    from src.brokers.tick_source import feed_unavailable_reason

    _isolated_store(monkeypatch, tmp_path)

    assert feed_unavailable_reason() == "no broker is connected"


def test_synthetic_mode_counts_as_a_feed(monkeypatch, tmp_path, _clean_settings):
    from src.brokers.tick_source import feed_unavailable_reason

    _isolated_store(monkeypatch, tmp_path, synthetic=True)

    assert feed_unavailable_reason() is None


def test_feed_reason_for_keys_without_a_token(monkeypatch, tmp_path, _clean_settings):
    from src.brokers.base import BrokerCredentials
    from src.brokers.tick_source import feed_unavailable_reason

    store = _isolated_store(monkeypatch, tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="k"))

    assert feed_unavailable_reason() == "zerodha has keys saved but is not connected"


def test_feed_reason_for_an_expired_token(monkeypatch, tmp_path, _clean_settings):
    from src.brokers.base import BrokerCredentials
    from src.brokers.tick_source import feed_unavailable_reason

    store = _isolated_store(monkeypatch, tmp_path)
    store.set_credentials(
        "zerodha",
        BrokerCredentials(
            api_key="k", access_token="t", token_expires_at="2020-01-01T00:00:00+00:00"
        ),
    )

    assert feed_unavailable_reason() == "the zerodha token has expired"


def test_feed_reason_for_a_rejected_token_and_none_when_healthy(
    monkeypatch, tmp_path, _clean_settings
):
    from src.brokers import feed_health
    from src.brokers.base import BrokerCredentials
    from src.brokers.tick_source import feed_unavailable_reason

    store = _isolated_store(monkeypatch, tmp_path)
    store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))

    assert feed_unavailable_reason() is None  # a valid token, nothing said against it
    feed_health.record_rejection("zerodha", status_code=403, detail="refused")
    assert feed_unavailable_reason() == "zerodha rejected the stored credentials (HTTP 403)"
