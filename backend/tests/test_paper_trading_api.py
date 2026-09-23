"""Paper Trading Engine API tests (Build Spec §11): enroll a strategy,
run the daily signal job, feed it ticks through the manual-trigger
endpoint, and see a real position/fill appear -- all through the actual
HTTP routes, RBAC included, and with no approval endpoint anywhere in
this flow.
"""

import pytest
from httpx import AsyncClient

from src.core.redis_client import get_redis
from src.core.roles import Role
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.main import app


@pytest.fixture(autouse=True)
def _isolate_redis_dependency(redis_client):
    """Same fix as test_webhooks_api.py/test_chat_api.py:
    src.core.redis_client.get_redis is a process-wide @lru_cache'd
    singleton never closed between tests -- reused across many separate
    pytest-asyncio function-scoped event loops, its underlying connection
    can end up bound to an already-closed loop from an earlier test.
    Overridden here to the per-test redis_client fixture (fresh
    connection, closed in its own teardown) so GET .../pnl/unrealized's
    real Redis reads never touch that shared, unmanaged singleton."""
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_redis, None)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_strategy_version(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": "PaperAPIDemo", "objective": "obj"},
        headers=_auth(token),
    )
    assert resp.status_code == 201
    return resp.json()["versions"][0]["id"]


async def test_enroll_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": "00000000-0000-0000-0000-000000000000",
            "symbol": "DEMOSTOCK",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_full_enroll_daily_signal_tick_cycle(client, make_user):
    await make_user("ops@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": version_id,
            "symbol": "DEMOSTOCK",
            "builtin_strategy": "sma_crossover",
            "sma_window": 15,
            "initial_capital": 100000,
            "stop_loss_pct": 3.0,
        },
        headers=_auth(token),
    )
    assert enroll_resp.status_code == 201
    subscription_id = enroll_resp.json()["id"]
    assert enroll_resp.json()["is_active"] is True

    # Known from FakeDailyPriceProvider's fixed default-seeded series:
    # 2026-09-09 is a genuine flat->long SMA-crossover BUY day.
    signal_resp = await client.post(
        "/api/v1/paper-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(token),
    )
    assert signal_resp.status_code == 201
    signals = [s for s in signal_resp.json() if s["subscription_id"] == subscription_id]
    assert len(signals) == 1
    assert signals[0]["signal_type"] == "BUY"
    reference_price = signals[0]["reference_price"]

    signals_list_resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/signals", headers=_auth(token)
    )
    assert signals_list_resp.status_code == 200
    assert len(signals_list_resp.json()) == 1

    tick_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference_price},
        headers=_auth(token),
    )
    assert tick_resp.status_code == 200
    fill = tick_resp.json()
    assert fill is not None
    assert fill["side"] == "buy"
    assert fill["filled_quantity"] > 0

    position_resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=_auth(token)
    )
    assert position_resp.status_code == 200
    assert position_resp.json()["quantity"] == fill["filled_quantity"]

    fills_resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/fills", headers=_auth(token)
    )
    assert fills_resp.status_code == 200
    assert len(fills_resp.json()) == 1


async def test_tick_endpoint_returns_null_when_there_is_no_actionable_signal(client, make_user):
    await make_user("ops2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops2@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={"strategy_version_id": version_id, "symbol": "DEMOSTOCK2"},
        headers=_auth(token),
    )
    subscription_id = enroll_resp.json()["id"]

    tick_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": 100.0},
        headers=_auth(token),
    )
    assert tick_resp.status_code == 200
    assert tick_resp.json() is None


async def test_position_endpoint_404s_before_any_fill(client, make_user):
    await make_user("ops3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops3@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={"strategy_version_id": version_id, "symbol": "DEMOSTOCK3"},
        headers=_auth(token),
    )
    subscription_id = enroll_resp.json()["id"]

    position_resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=_auth(token)
    )
    assert position_resp.status_code == 404


async def test_get_unknown_subscription_404s(client, make_user):
    await make_user("ops4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops4@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/paper-trading/subscriptions/00000000-0000-0000-0000-000000000000",
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_todays_pnl_endpoint_is_zero_with_no_fills(client, make_user):
    await make_user("pnl1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "pnl1@example.com", "supersecret1")

    resp = await client.get("/api/v1/paper-trading/pnl/today", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["realized_pnl"] == 0.0
    assert body["fill_count"] == 0


async def test_todays_pnl_endpoint_sums_real_realized_pnl_across_a_stop_loss_exit(
    client, make_user
):
    await make_user("pnl2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "pnl2@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": version_id,
            # Known from FakeDailyPriceProvider's fixed default-seeded
            # series (per-symbol seeded, see the full-cycle test above):
            # "DEMOSTOCK" genuinely has a flat->long SMA-crossover BUY
            # signal on 2026-09-09.
            "symbol": "DEMOSTOCK",
            "builtin_strategy": "sma_crossover",
            "sma_window": 15,
            "initial_capital": 100000,
            "stop_loss_pct": 3.0,
        },
        headers=_auth(token),
    )
    subscription_id = enroll_resp.json()["id"]

    signal_resp = await client.post(
        "/api/v1/paper-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(token),
    )
    signals = [s for s in signal_resp.json() if s["subscription_id"] == subscription_id]
    reference_price = signals[0]["reference_price"]

    # Open the position -- an opening fill always has realized_pnl == 0.0
    # (src.engine.paper_trading.position_ledger.apply_fill).
    open_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference_price},
        headers=_auth(token),
    )
    open_fill = open_resp.json()
    assert open_fill["realized_pnl"] == 0.0

    # A real HTTP baseline check before the loss: the endpoint is global
    # (portfolio-wide), so confirm it already reflects the opening fill
    # (0.0, 1 fill) before triggering the closing one.
    mid_resp = await client.get("/api/v1/paper-trading/pnl/today", headers=_auth(token))
    mid_body = mid_resp.json()
    assert mid_body["realized_pnl"] == 0.0
    assert mid_body["fill_count"] == 1

    # A tick 10% below avg cost is well past the 3% stop-loss threshold --
    # a real universal-stop-loss exit, not a fabricated closing trade.
    exit_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference_price * 0.9},
        headers=_auth(token),
    )
    exit_fill = exit_resp.json()
    assert exit_fill is not None
    assert exit_fill["side"] == "sell"
    assert exit_fill["realized_pnl"] < 0.0

    pnl_resp = await client.get("/api/v1/paper-trading/pnl/today", headers=_auth(token))
    assert pnl_resp.status_code == 200
    body = pnl_resp.json()
    assert body["fill_count"] == 2
    assert body["realized_pnl"] == exit_fill["realized_pnl"]


async def test_todays_pnl_endpoint_readable_by_every_role(client, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"pnl3-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/paper-trading/pnl/today", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied read access"


async def test_unrealized_pnl_endpoint_is_zero_with_no_open_positions(client, make_user):
    await make_user("upnl1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "upnl1@example.com", "supersecret1")

    resp = await client.get("/api/v1/paper-trading/pnl/unrealized", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["unrealized_pnl"] == 0.0
    assert body["positions_priced"] == 0
    assert body["positions_unpriced"] == 0
    # This sandbox has no broker credentials configured -- see Root Cause
    # 1 of the Settings-fix pass earlier in this project's history --
    # so the live TickSource is genuinely MockTickSource, not a fabricated
    # label.
    assert body["price_source"] == "synthetic"


async def test_unrealized_pnl_endpoint_marks_an_open_position_against_the_latest_published_tick(
    client, make_user, redis_client
):
    symbol = "DEMOSTOCK"
    await redis_client.delete(tick_stream_key(symbol))

    await make_user("upnl2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "upnl2@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": version_id,
            "symbol": symbol,
            "builtin_strategy": "sma_crossover",
            "sma_window": 15,
            "initial_capital": 100000,
            "stop_loss_pct": 3.0,
        },
        headers=_auth(token),
    )
    subscription_id = enroll_resp.json()["id"]

    signal_resp = await client.post(
        "/api/v1/paper-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(token),
    )
    signals = [s for s in signal_resp.json() if s["subscription_id"] == subscription_id]
    reference_price = signals[0]["reference_price"]

    # process_tick (the manual fill-simulation trigger) never touches
    # paper:ticks:{symbol} itself -- opening a position here proves
    # nothing about whether a mark-to-market price is available.
    open_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference_price},
        headers=_auth(token),
    )
    assert open_resp.json()["side"] == "buy"

    position_resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=_auth(token)
    )
    position = position_resp.json()
    avg_cost = position["avg_cost"]
    quantity = position["quantity"]
    assert quantity > 0

    # A real tick published straight to the Redis stream -- independent
    # of the fill-simulation tick above, same as production (the
    # intraday tick-drain job publishes here, not the manual endpoint).
    current_price = round(avg_cost * 1.05, 2)
    await publish_tick(
        redis_client, Tick(symbol=symbol, price=current_price, timestamp_ms=1_700_000_000_000)
    )

    resp = await client.get("/api/v1/paper-trading/pnl/unrealized", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions_priced"] == 1
    assert body["positions_unpriced"] == 0
    assert body["price_source"] == "synthetic"
    assert body["unrealized_pnl"] == pytest.approx((current_price - avg_cost) * quantity)

    await redis_client.delete(tick_stream_key(symbol))


async def test_unrealized_pnl_endpoint_reports_none_when_an_open_position_has_no_tick_yet(
    client, make_user, redis_client
):
    symbol = "DEMOSTOCK"
    await redis_client.delete(tick_stream_key(symbol))

    await make_user("upnl3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "upnl3@example.com", "supersecret1")
    version_id = await _create_strategy_version(client, token)

    enroll_resp = await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": version_id,
            "symbol": symbol,
            "builtin_strategy": "sma_crossover",
            "sma_window": 15,
            "initial_capital": 100000,
            "stop_loss_pct": 3.0,
        },
        headers=_auth(token),
    )
    subscription_id = enroll_resp.json()["id"]

    signal_resp = await client.post(
        "/api/v1/paper-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(token),
    )
    signals = [s for s in signal_resp.json() if s["subscription_id"] == subscription_id]
    reference_price = signals[0]["reference_price"]

    open_resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/tick",
        json={"tick_price": reference_price},
        headers=_auth(token),
    )
    assert open_resp.json()["side"] == "buy"

    # No tick has ever been published to paper:ticks:DEMOSTOCK in this
    # test -- the position is genuinely open with nothing to mark it
    # against.
    resp = await client.get("/api/v1/paper-trading/pnl/unrealized", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions_priced"] == 0
    assert body["positions_unpriced"] == 1
    assert body["unrealized_pnl"] is None


async def test_unrealized_pnl_endpoint_readable_by_every_role(client, make_user):
    for i, role in enumerate(
        [
            Role.SYSTEM_ADMINISTRATOR,
            Role.PORTFOLIO_MANAGER,
            Role.RISK_MANAGER,
            Role.READ_ONLY_AUDITOR,
        ]
    ):
        email = f"upnl4-{i}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _login(client, email, "supersecret1")
        resp = await client.get("/api/v1/paper-trading/pnl/unrealized", headers=_auth(token))
        assert resp.status_code == 200, f"role {role} was denied read access"
