"""Agent roster + pipeline API routes (Build Spec §7.1-§7.2), plus identity
editing and prompt-version history (a later pass): thin HTTP fronts for
src.gateway.service.set_identity and src.orchestration.prompt_versions,
both fully unit-tested since Phase 3 but previously CLI-only.
"""

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from src.api.routes.agents import _config_path
from src.core.config import get_settings
from src.core.roles import Role
from src.main import app
from src.models.agent_identity import AgentIdentity


@pytest.fixture(autouse=True)
def _isolated_gateway_config(tmp_path):
    """`PUT /{agent_id}/identity` writes through
    `src.gateway.service.set_identity`, which atomically rewrites the Agent
    Gateway config file on disk -- override `_config_path` to a per-test
    copy so these tests never touch the repo's own tracked
    config/tradingos.config.json (mirrors test_market_data_api.py's
    get_data_lake_root override)."""
    real_path = Path(get_settings().agent_gateway_config_path)
    isolated_path = tmp_path / "gateway.json"
    isolated_path.write_text(real_path.read_text(encoding="utf-8"), encoding="utf-8")
    app.dependency_overrides[_config_path] = lambda: isolated_path
    yield
    app.dependency_overrides.pop(_config_path, None)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_identity(db_session_factory, agent_id: str) -> None:
    """create_prompt_version's FK (prompt_versions.agent_id ->
    agent_identities.agent_id) needs a real row -- in real deployments
    this always exists (src.gateway.apply syncs it on every config
    apply), but this suite's fresh-schema-per-test DB starts empty."""
    async with db_session_factory() as db:
        db.add(AgentIdentity(agent_id=agent_id, name="Seed"))
        await db.commit()


async def test_list_agents_returns_all_30_with_capabilities(client, make_user):
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "auditor@example.com", "supersecret1")

    resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 30
    by_id = {a["agent_id"]: a for a in body}
    assert by_id["audit-agent"]["can_disable"] is False
    # Phase 19 (docs/phase19-audit.md §3.2): honest data-source gap flag --
    # False only for the 3 agents with no real backing data anywhere.
    assert by_id["fundamentals-agent"]["has_data_source"] is False
    assert by_id["valuation-agent"]["has_data_source"] is False
    assert by_id["macro-agent"]["has_data_source"] is False
    assert by_id["screener-agent"]["has_data_source"] is True
    assert by_id["ceo-agent"]["has_data_source"] is True
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


async def test_set_identity_updates_only_given_fields(client, make_user):
    await make_user("admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin1@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        "/api/v1/agents/ceo-agent/identity",
        json={"name": "CEO Prime", "emoji": "\U0001f451"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    listing = await client.get("/api/v1/agents", headers=headers)
    by_id = {a["agent_id"]: a for a in listing.json()}
    assert by_id["ceo-agent"]["identity_name"] == "CEO Prime"
    assert by_id["ceo-agent"]["emoji"] == "\U0001f451"
    assert by_id["ceo-agent"]["avatar"] is None

    resp2 = await client.put(
        "/api/v1/agents/ceo-agent/identity",
        json={"theme": "midnight"},
        headers=headers,
    )
    assert resp2.status_code == 200

    listing2 = await client.get("/api/v1/agents", headers=headers)
    by_id2 = {a["agent_id"]: a for a in listing2.json()}
    assert by_id2["ceo-agent"]["identity_name"] == "CEO Prime"  # untouched by the 2nd PUT
    assert by_id2["ceo-agent"]["theme"] == "midnight"


async def test_set_identity_requires_admin_role(client, make_user):
    await make_user("pm1@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm1@example.com", "supersecret1")

    resp = await client.put(
        "/api/v1/agents/ceo-agent/identity",
        json={"name": "Nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_set_identity_404s_for_unknown_agent(client, make_user):
    await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin2@example.com", "supersecret1")

    resp = await client.put(
        "/api/v1/agents/not-a-real-agent/identity",
        json={"name": "Nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_agent_activity_reports_real_heartbeats_and_capability_tasks(
    client, make_user, db_session_factory
):
    """Phase 19 (docs/phase19-audit.md Part 3.2): Agent Fleet's real
    per-agent activity panel -- heartbeat self-checks straight off
    HeartbeatLog, and orchestration tasks joined by capability (Task has
    no agent_id column) rather than any fabricated per-agent metric."""
    from src.models.heartbeat_log import HeartbeatLog, HeartbeatStatus
    from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
    from src.models.organizational_plan import OrganizationalPlan, PlanStatus
    from src.models.task import Task, TaskStatus

    await make_user("activity-viewer@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "activity-viewer@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    async with db_session_factory() as db:
        db.add(AgentIdentity(agent_id="ceo-agent", name="Seed"))
        db.add(HeartbeatLog(agent_id="ceo-agent", status=HeartbeatStatus.OK, details={"ok": True}))
        run = OrganizationRun(
            objective="obj",
            source=RunSource.UI,
            run_type=RunType.STANDARD,
            status=RunStatus.RUNNING,
        )
        db.add(run)
        await db.flush()
        plan = OrganizationalPlan(
            run_id=run.id, attempt_number=1, status=PlanStatus.ACCEPTED, objective="obj"
        )
        db.add(plan)
        await db.flush()
        # ceo-agent's real registered capability (src.agents.roster.AGENT_CAPABILITIES).
        db.add(
            Task(
                run_id=run.id,
                plan_id=plan.id,
                plan_key="k1",
                capability="graph.ceo_kickoff",
                name="CEO kickoff",
                status=TaskStatus.SUCCEEDED,
            )
        )
        # A capability this agent does NOT own -- must never show up below.
        db.add(
            Task(
                run_id=run.id,
                plan_id=plan.id,
                plan_key="k2",
                capability="graph.risk_assessment",
                name="Risk assessment",
                status=TaskStatus.SUCCEEDED,
            )
        )
        await db.commit()

    resp = await client.get("/api/v1/agents/ceo-agent/activity", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "ceo-agent"
    assert len(body["heartbeats"]) == 1
    assert body["heartbeats"][0]["status"] == "ok"
    assert [t["name"] for t in body["tasks"]] == ["CEO kickoff"]


async def test_agent_activity_404s_for_unknown_agent(client, make_user):
    await make_user("activity-viewer2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "activity-viewer2@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/agents/not-a-real-agent/activity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_list_prompt_versions_empty_then_ordered_desc(client, make_user, db_session_factory):
    await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin3@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    empty = await client.get("/api/v1/agents/ceo-agent/prompt-versions", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []

    await _seed_identity(db_session_factory, "ceo-agent")
    await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions", json={"content": "v1 content"}, headers=headers
    )
    await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions", json={"content": "v2 content"}, headers=headers
    )

    listing = await client.get("/api/v1/agents/ceo-agent/prompt-versions", headers=headers)
    assert listing.status_code == 200
    numbers = [row["version_number"] for row in listing.json()]
    assert numbers == [2, 1]


async def test_create_prompt_version_requires_admin_role(client, make_user, db_session_factory):
    await _seed_identity(db_session_factory, "ceo-agent")
    await make_user("pm2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    token = await _login(client, "pm2@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions",
        json={"content": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_create_prompt_version_404s_for_unknown_agent(client, make_user):
    await make_user("admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin4@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/agents/not-a-real-agent/prompt-versions",
        json={"content": "nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_create_prompt_version_sets_created_by(client, make_user, db_session_factory):
    await _seed_identity(db_session_factory, "ceo-agent")
    await make_user("admin5@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin5@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions",
        json={"content": "You are the CEO."},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["created_by"] == "admin5@example.com"


async def test_activate_prompt_version_supersedes_previous_and_computes_diff(
    client, make_user, db_session_factory
):
    await _seed_identity(db_session_factory, "ceo-agent")
    await make_user("admin6@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin6@example.com", "supersecret1")
    headers = {"Authorization": f"Bearer {token}"}

    v1 = await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions",
        json={"content": "line one\n"},
        headers=headers,
    )
    v1_id = v1.json()["id"]
    activate_v1 = await client.post(
        f"/api/v1/agents/ceo-agent/prompt-versions/{v1_id}/activate", headers=headers
    )
    assert activate_v1.status_code == 200
    assert activate_v1.json()["status"] == "active"
    assert activate_v1.json()["diff_from_previous"] is None

    v2 = await client.post(
        "/api/v1/agents/ceo-agent/prompt-versions",
        json={"content": "line one\nline two\n"},
        headers=headers,
    )
    v2_id = v2.json()["id"]
    activate_v2 = await client.post(
        f"/api/v1/agents/ceo-agent/prompt-versions/{v2_id}/activate", headers=headers
    )
    assert activate_v2.status_code == 200
    assert activate_v2.json()["status"] == "active"
    assert "line two" in activate_v2.json()["diff_from_previous"]

    listing = await client.get("/api/v1/agents/ceo-agent/prompt-versions", headers=headers)
    by_id = {row["id"]: row for row in listing.json()}
    assert by_id[v1_id]["status"] == "superseded"


async def test_activate_prompt_version_404s_for_unknown_version(
    client, make_user, db_session_factory
):
    await _seed_identity(db_session_factory, "ceo-agent")
    await make_user("admin7@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin7@example.com", "supersecret1")

    resp = await client.post(
        f"/api/v1/agents/ceo-agent/prompt-versions/{uuid.uuid4()}/activate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
