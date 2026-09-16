"""Risk & Safety Layer API tests (Build Spec §8): the kill switch trips and
blocks new order intents until an explicit human reset; the Go-Live gate
requires all four conditions; dual-control risk-limit changes reject both
self-confirmation and an insufficiently-privileged confirmer -- all
through the actual HTTP routes, RBAC included.
"""

from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _order_intent_body(**overrides) -> dict:
    body = {
        "mode": "live",
        "symbol": "RELIANCE",
        "side": "buy",
        "quantity": 10,
        "proposed_price": 2000,
        "reference_price": 2000,
        "proposed_position_value": 20000,
        "portfolio_value": 1000000,
    }
    body.update(overrides)
    return body


async def test_kill_switch_trips_and_blocks_order_intents_until_reset(client, make_user):
    await make_user("ops@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops@example.com", "supersecret1")

    intent_resp = await client.post(
        "/api/v1/risk/order-intents", json=_order_intent_body(), headers=_auth(token)
    )
    assert intent_resp.status_code == 201

    check_resp = await client.post(
        "/api/v1/kill-switch/live/check",
        json={"current_equity": 80000, "peak_equity": 100000},
        headers=_auth(token),
    )
    assert check_resp.status_code == 200
    assert check_resp.json()["tripped"] is True

    blocked_resp = await client.post(
        "/api/v1/risk/order-intents", json=_order_intent_body(), headers=_auth(token)
    )
    assert blocked_resp.status_code == 423

    # A full "recovery" observation must never self-clear it.
    recovery_resp = await client.post(
        "/api/v1/kill-switch/live/check",
        json={"current_equity": 200000, "peak_equity": 200000},
        headers=_auth(token),
    )
    assert recovery_resp.json()["tripped"] is True

    still_blocked = await client.post(
        "/api/v1/risk/order-intents", json=_order_intent_body(), headers=_auth(token)
    )
    assert still_blocked.status_code == 423

    reset_resp = await client.post("/api/v1/kill-switch/live/reset", json={}, headers=_auth(token))
    assert reset_resp.status_code == 200
    assert reset_resp.json()["tripped"] is False

    recovered_resp = await client.post(
        "/api/v1/risk/order-intents", json=_order_intent_body(), headers=_auth(token)
    )
    assert recovered_resp.status_code == 201


async def test_reset_kill_switch_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.post("/api/v1/kill-switch/live/reset", json={}, headers=_auth(token))
    assert resp.status_code == 403


async def test_go_live_readiness_requires_all_four_conditions(client, make_user):
    await make_user("ops2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops2@example.com", "supersecret1")

    good = {
        "num_trades": 30,
        "calendar_days_running": 21,
        "clean_shadow_mode_streak_days": 10,
        "live_win_rate": 0.55,
        "backtest_win_rate": 0.50,
    }
    resp = await client.post("/api/v1/risk/go-live-readiness", json=good, headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["eligible"] is True

    bad = {**good, "num_trades": 5}
    resp2 = await client.post("/api/v1/risk/go-live-readiness", json=bad, headers=_auth(token))
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["eligible"] is False
    assert body["checks"]["min_trades"] is False


async def test_dual_control_rejects_self_confirmation(client, make_user):
    await make_user("risk1@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "risk1@example.com", "supersecret1")

    stage_resp = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 10.0, "reason": "tighten"},
        headers=_auth(token),
    )
    assert stage_resp.status_code == 201
    change_id = stage_resp.json()["id"]

    self_confirm_resp = await client.post(
        f"/api/v1/risk-limits/{change_id}/confirm", headers=_auth(token)
    )
    assert self_confirm_resp.status_code == 403


async def test_dual_control_rejects_insufficiently_privileged_confirmer(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin1@example.com", "supersecret1")
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    pm_token = await _login(client, "pm1@example.com", "supersecret1")

    stage_resp = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "ws_latency_ms", "proposed_value": 50.0},
        headers=_auth(admin_token),
    )
    change_id = stage_resp.json()["id"]

    resp = await client.post(f"/api/v1/risk-limits/{change_id}/confirm", headers=_auth(pm_token))
    assert resp.status_code == 403


async def test_dual_control_full_flow_with_two_different_privileged_users(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin2@example.com", "supersecret1")
    await make_user("risk2@example.com", "supersecret1", Role.RISK_MANAGER)
    risk_token = await _login(client, "risk2@example.com", "supersecret1")

    stage_resp = await client.post(
        "/api/v1/risk-limits/stage",
        json={"limit_name": "max_drawdown_pct", "proposed_value": 12.5},
        headers=_auth(admin_token),
    )
    change_id = stage_resp.json()["id"]

    confirm_resp = await client.post(
        f"/api/v1/risk-limits/{change_id}/confirm", headers=_auth(risk_token)
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["status"] == "confirmed"

    apply_resp = await client.post(
        f"/api/v1/risk-limits/{change_id}/apply", headers=_auth(risk_token)
    )
    assert apply_resp.status_code == 200
    assert apply_resp.json()["status"] == "applied"

    list_resp = await client.get("/api/v1/risk-limits", headers=_auth(admin_token))
    assert list_resp.status_code == 200
    values = {row["name"]: row["value"] for row in list_resp.json()}
    assert values["max_drawdown_pct"] == 12.5
