"""End-to-end acceptance (Build Spec §9): submit an objective via the API,
see a generated strategy that passed static validation and ran in the
sandbox, and confirm it cannot reach PaperTrading without a human approval
-- through the actual HTTP routes, RBAC included.
"""

from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_create_strategy_produces_validated_sandboxed_strategy(client, make_user):
    await make_user("ops@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Momentum", "objective": "Build a momentum strategy for NIFTY"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "Backtesting"
    assert len(body["versions"]) == 1
    version = body["versions"][0]
    assert version["static_validation_passed"] is True
    assert version["sandbox_passed"] is True

    get_resp = await client.get(
        f"/api/v1/strategies/{body['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "Backtesting"


async def test_create_strategy_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/strategies",
        json={"name": "X", "objective": "obj"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_promotion_is_blocked_until_approved_end_to_end(client, make_user):
    await make_user("ops2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "ops2@example.com", "supersecret1")
    await make_user("risk1@example.com", "supersecret1", Role.RISK_MANAGER)
    risk_token = await _login(client, "risk1@example.com", "supersecret1")

    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Momentum", "objective": "obj"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = create_resp.json()["id"]

    # No approval requested at all yet -- promotion must be refused.
    promote_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/promote",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert promote_resp.status_code == 409

    # Requesting promotion creates a pending ApprovalRequest -- still not
    # enough on its own.
    request_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/request-promotion",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert request_resp.status_code == 201
    approval_id = request_resp.json()["id"]

    still_blocked = await client.post(
        f"/api/v1/strategies/{strategy_id}/promote",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert still_blocked.status_code == 409

    # A Risk Manager decides the request...
    decide_resp = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"Authorization": f"Bearer {risk_token}"},
    )
    assert decide_resp.status_code == 200
    assert decide_resp.json()["status"] == "approved"

    # ...and only now does promotion succeed.
    promoted = await client.post(
        f"/api/v1/strategies/{strategy_id}/promote",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "PaperTrading"


async def test_decide_approval_requires_risk_or_admin_role(client, make_user):
    await make_user("ops3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "ops3@example.com", "supersecret1")
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    pm_token = await _login(client, "pm1@example.com", "supersecret1")

    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "S", "objective": "obj"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = create_resp.json()["id"]
    request_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/request-promotion",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    approval_id = request_resp.json()["id"]

    # PortfolioManager is not in the decide-roles set.
    resp = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"approve": True},
        headers={"Authorization": f"Bearer {pm_token}"},
    )
    assert resp.status_code == 403


async def test_suggestion_flow_end_to_end(client, make_user):
    await make_user("ops4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops4@example.com", "supersecret1")

    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "S", "objective": "obj"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = create_resp.json()
    strategy_id = body["id"]
    version_id = body["versions"][0]["id"]

    submit_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/suggestions",
        json={"base_version_id": version_id, "suggestion_text": "Add a stop-loss"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert submit_resp.status_code == 201
    suggestion_id = submit_resp.json()["id"]
    assert submit_resp.json()["status"] == "pending"

    review_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/suggestions/{suggestion_id}/review",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert review_resp.status_code == 200
    assert review_resp.json()["status"] == "reviewed"
    assert review_resp.json()["ai_verdict"] is not None

    regen_resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/suggestions/{suggestion_id}/regenerate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert regen_resp.status_code == 200
    assert regen_resp.json()["status"] == "regenerated"
    assert regen_resp.json()["regenerated_version_id"] is not None
    assert regen_resp.json()["regeneration_diff"] is not None
