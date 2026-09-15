"""Agent roster + pipeline API routes (Build Spec §7.1-§7.2)."""

from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_list_agents_returns_all_24_with_capabilities(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 24
    by_id = {a["agent_id"]: a for a in body}
    assert by_id["audit-agent"]["can_disable"] is False
    assert len(by_id["ceo-agent"]["capabilities"]) > 0


async def test_run_pipeline_endpoint_requires_operator_role(client, make_user):
    await make_user("auditor2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor2@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/agents/pipeline/run",
        json={"objective": "anything"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_run_pipeline_endpoint_reaches_deployment(client, make_user):
    await make_user("ops3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops3@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/agents/pipeline/run",
        json={"objective": "Build a momentum strategy for NIFTY"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["node_log"][0] == "ceo_kickoff"
    assert body["node_log"][-1] == "deployment"
    assert body["deployment_result"] == {"deployed": True, "target": "paper"}
