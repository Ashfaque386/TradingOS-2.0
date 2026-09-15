"""End-to-end acceptance (Build Spec §7.3 Phase 2): POST an objective, see
a task graph get planned and executed against stub capabilities, RBAC
enforced, and rerun producing a fresh run.
"""

from httpx import AsyncClient

from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_post_objective_plans_and_executes_to_completion(client, make_user):
    await make_user("ops@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/orchestration/runs",
        json={"objective": "Research a momentum strategy for NIFTY"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "completed"
    assert body["run_type"] == "standard"
    assert len(body["tasks"]) == 9
    assert all(t["status"] == "succeeded" for t in body["tasks"])

    # GET returns the same settled state.
    get_resp = await client.get(
        f"/api/v1/orchestration/runs/{body['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "completed"


async def test_create_run_requires_operator_role(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/orchestration/runs",
        json={"objective": "anything"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_get_run_readable_by_read_only_auditor(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    admin_token = await _login(client, "admin2@example.com", "supersecret1")
    create_resp = await client.post(
        "/api/v1/orchestration/runs",
        json={"objective": "obj"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    run_id = create_resp.json()["id"]

    await make_user("auditor2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    auditor_token = await _login(client, "auditor2@example.com", "supersecret1")

    resp = await client.get(
        f"/api/v1/orchestration/runs/{run_id}", headers={"Authorization": f"Bearer {auditor_token}"}
    )
    assert resp.status_code == 200


async def test_rerun_endpoint_creates_a_fresh_run(client, make_user):
    await make_user("ops2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops2@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/orchestration/runs", json={"objective": "obj"}, headers=headers
    )
    source_id = create_resp.json()["id"]

    rerun_resp = await client.post(f"/api/v1/orchestration/runs/{source_id}/rerun", headers=headers)
    assert rerun_resp.status_code == 201
    body = rerun_resp.json()
    assert body["id"] != source_id
    assert body["source_run_id"] == source_id
    assert body["run_type"] == "rerun"
    assert body["status"] == "completed"


async def test_rerun_endpoint_404s_for_unknown_run(client, make_user):
    await make_user("ops3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "ops3@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/orchestration/runs/00000000-0000-0000-0000-000000000000/rerun",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
