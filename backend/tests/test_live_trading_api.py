"""Live Trading API acceptance tests (Build Spec §12): the full HTTP
flow -- strategy promotion, live-eligibility sign-off, enrollment, intent
generation, and approve/reject -- with RBAC enforced throughout and a
mocked broker adapter injected via dependency override (no real
credentials or network egress needed).
"""

import httpx
from httpx import AsyncClient

from src.api.routes.live_trading import get_live_broker_adapter
from src.brokers.base import BrokerCredentials
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.core.roles import Role
from src.main import app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_broker_handler(price: float = 106.5, order_id: str = "LIVE_API_OID"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/quote":
            symbol = request.url.params["i"]
            return httpx.Response(200, json={"data": {symbol: {"last_price": price, "depth": {}}}})
        return httpx.Response(200, json={"data": {"order_id": order_id}})

    return handler


def _override_adapter(price: float = 106.5, order_id: str = "LIVE_API_OID"):
    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(_mock_broker_handler(price, order_id)),
    )
    app.dependency_overrides[get_live_broker_adapter] = lambda: adapter
    return adapter


def _clear_adapter_override():
    app.dependency_overrides.pop(get_live_broker_adapter, None)


async def _promote_to_live_eligible(client: AsyncClient, admin_token: str, risk_token: str) -> str:
    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "LiveApiDemo", "objective": "obj"},
        headers=_auth(admin_token),
    )
    assert create_resp.status_code == 201
    strategy_id = create_resp.json()["id"]

    request_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/request-promotion", headers=_auth(admin_token)
    )
    approval_id = request_resp.json()["id"]
    await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"approve": True},
        headers=_auth(risk_token),
    )
    promote_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/promote", headers=_auth(admin_token)
    )
    assert promote_resp.status_code == 200
    assert promote_resp.json()["status"] == "PaperTrading"

    live_request_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/request-live-eligibility", headers=_auth(risk_token)
    )
    assert live_request_resp.status_code == 201
    live_approval_id = live_request_resp.json()["id"]
    await client.post(
        f"/api/v1/approvals/{live_approval_id}/decide",
        json={"approve": True},
        headers=_auth(risk_token),
    )

    readiness_body = {
        "num_trades": 50,
        "calendar_days_running": 30,
        "clean_shadow_mode_streak_days": 15,
        "live_win_rate": 0.55,
        "backtest_win_rate": 0.5,
    }
    approve_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/approve-live-eligibility",
        json=readiness_body,
        headers=_auth(risk_token),
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "LiveEligible"

    return strategy_id


async def test_enroll_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/live-trading/subscriptions",
        json={
            "strategy_id": "00000000-0000-0000-0000-000000000000",
            "symbol": "DEMOAPISTOCK",
            "broker_name": "zerodha",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_enroll_requires_live_eligible_strategy(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin1@example.com", "supersecret1")

    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "NotYetEligible", "objective": "obj"},
        headers=_auth(admin_token),
    )
    strategy_id = create_resp.json()["id"]

    resp = await client.post(
        "/api/v1/live-trading/subscriptions",
        json={"strategy_id": strategy_id, "symbol": "DEMOAPISTOCK", "broker_name": "zerodha"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 409


async def test_full_enroll_generate_approve_cycle_reaches_the_broker(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin2@example.com", "supersecret1")
    await make_user("risk2@example.com", "supersecret1", Role.RISK_MANAGER)
    risk_token = await _login(client, "risk2@example.com", "supersecret1")

    strategy_id = await _promote_to_live_eligible(client, admin_token, risk_token)

    # "DEMOSTOCK" + 2026-09-09 is the same fixed, verified BUY day used by
    # Phase 7's own live-verification pass (FakeDailyPriceProvider's
    # default-seeded series is stable across processes -- crc32-based,
    # not Python's salted hash()).
    enroll_resp = await client.post(
        "/api/v1/live-trading/subscriptions",
        json={"strategy_id": strategy_id, "symbol": "DEMOSTOCK", "broker_name": "zerodha"},
        headers=_auth(admin_token),
    )
    assert enroll_resp.status_code == 201
    subscription_id = enroll_resp.json()["id"]

    daily_resp = await client.post(
        "/api/v1/live-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(admin_token),
    )
    assert daily_resp.status_code == 200
    assert len(daily_resp.json()) == 1
    assert daily_resp.json()[0]["id"] == subscription_id

    _override_adapter(price=106.5, order_id="LIVE_API_OID")
    try:
        generate_resp = await client.post(
            f"/api/v1/live-trading/subscriptions/{subscription_id}/generate-intent",
            json={"tick_price": 106.0},
            headers=_auth(admin_token),
        )
        assert generate_resp.status_code == 200
        intent_body = generate_resp.json()
        assert intent_body is not None
        assert intent_body["status"] == "pending_approval"
        assert intent_body["intent_type"] == "entry"
        intent_id = intent_body["id"]

        approve_resp = await client.post(
            f"/api/v1/live-trading/intents/{intent_id}/approve", headers=_auth(risk_token)
        )
        assert approve_resp.status_code == 200
        approved_body = approve_resp.json()
        assert approved_body["status"] == "submitted"
        assert approved_body["resulting_order_id"] is not None

        orders_resp = await client.get(
            f"/api/v1/live-trading/strategies/{strategy_id}/orders", headers=_auth(admin_token)
        )
        assert orders_resp.status_code == 200
        orders = orders_resp.json()
        assert len(orders) == 1
        assert orders[0]["status"] == "submitted"
        assert orders[0]["broker_order_id"] == "LIVE_API_OID"

        trades_resp = await client.get(
            f"/api/v1/live-trading/orders/{orders[0]['id']}/trades", headers=_auth(admin_token)
        )
        assert trades_resp.status_code == 200
        trades = trades_resp.json()
        assert len(trades) == 1
        assert trades[0]["price"] == 106.5

        position_resp = await client.get(
            f"/api/v1/live-trading/strategies/{strategy_id}/position", headers=_auth(admin_token)
        )
        assert position_resp.status_code == 200
        assert position_resp.json()["quantity"] == intent_body["quantity"]
    finally:
        _clear_adapter_override()


async def test_reject_intent_never_reaches_the_broker_over_http(client, make_user):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin3@example.com", "supersecret1")
    await make_user("risk3@example.com", "supersecret1", Role.RISK_MANAGER)
    risk_token = await _login(client, "risk3@example.com", "supersecret1")

    strategy_id = await _promote_to_live_eligible(client, admin_token, risk_token)

    enroll_resp = await client.post(
        "/api/v1/live-trading/subscriptions",
        json={"strategy_id": strategy_id, "symbol": "DEMOSTOCK", "broker_name": "zerodha"},
        headers=_auth(admin_token),
    )
    subscription_id = enroll_resp.json()["id"]

    await client.post(
        "/api/v1/live-trading/daily-signal-run",
        json={"as_of": "2026-09-09"},
        headers=_auth(admin_token),
    )

    # A broker that raises on any call -- if approve/generate ever touched
    # it during generation, this test would fail with an error instead of
    # asserting cleanly. generate-intent itself never calls the broker
    # (only approval does), so this also doubles as proof of that.
    def _explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the broker must never be called for a rejected intent")

    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(_explode)
    )
    app.dependency_overrides[get_live_broker_adapter] = lambda: adapter
    try:
        generate_resp = await client.post(
            f"/api/v1/live-trading/subscriptions/{subscription_id}/generate-intent",
            json={"tick_price": 106.0},
            headers=_auth(admin_token),
        )
        assert generate_resp.status_code == 200
        intent_body = generate_resp.json()
        assert intent_body is not None
        assert intent_body["status"] == "pending_approval"

        reject_resp = await client.post(
            f"/api/v1/live-trading/intents/{intent_body['id']}/reject", headers=_auth(risk_token)
        )
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"

        orders_resp = await client.get(
            f"/api/v1/live-trading/strategies/{strategy_id}/orders", headers=_auth(admin_token)
        )
        assert orders_resp.json() == []
    finally:
        _clear_adapter_override()


async def test_approve_and_reject_require_live_signoff_role(client, make_user):
    await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin4@example.com", "supersecret1")
    await make_user("pm4@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    pm_token = await _login(client, "pm4@example.com", "supersecret1")

    approve_resp = await client.post(
        "/api/v1/live-trading/intents/00000000-0000-0000-0000-000000000000/approve",
        headers=_auth(pm_token),
    )
    assert approve_resp.status_code == 403

    reject_resp = await client.post(
        "/api/v1/live-trading/intents/00000000-0000-0000-0000-000000000000/reject",
        headers=_auth(pm_token),
    )
    assert reject_resp.status_code == 403

    # A SystemAdministrator is allowed through RBAC but the intent
    # doesn't exist -- 404, not 403, proves RBAC passed for this role.
    not_found_resp = await client.post(
        "/api/v1/live-trading/intents/00000000-0000-0000-0000-000000000000/approve",
        headers=_auth(admin_token),
    )
    assert not_found_resp.status_code == 404
