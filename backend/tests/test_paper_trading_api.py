"""Paper Trading Engine API tests (Build Spec §11): enroll a strategy,
run the daily signal job, feed it ticks through the manual-trigger
endpoint, and see a real position/fill appear -- all through the actual
HTTP routes, RBAC included, and with no approval endpoint anywhere in
this flow.
"""

from httpx import AsyncClient

from src.core.roles import Role


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
