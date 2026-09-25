"""Agent run analytics (Phase 22, docs/phase20-old-vs-new-comparison.md
item 21): per-agent summary and a system-wide daily trend, both over real
Task rows joined by capability -- the same join
`/{agent_id}/activity` already established (Task has no agent_id column).
"""

from datetime import UTC, datetime, timedelta

from src.core.roles import Role
from src.models.organization_run import OrganizationRun, RunSource, RunStatus, RunType
from src.models.organizational_plan import OrganizationalPlan, PlanStatus
from src.models.task import Task, TaskStatus


async def _login(client, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_run_and_plan(db_session_factory) -> tuple:
    async with db_session_factory() as db:
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
        await db.commit()
        return run.id, plan.id


async def test_summary_has_one_row_per_roster_agent_including_zero_task_agents(client, make_user):
    await make_user("viewer1@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "viewer1@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/agents/analytics/summary", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    agent_ids = {r["agent_id"] for r in rows}
    assert "ceo-agent" in agent_ids
    assert len(rows) >= 24  # the fixed roster, never a silently-shorter list

    ceo_row = next(r for r in rows if r["agent_id"] == "ceo-agent")
    assert ceo_row["tasks_total"] == 0
    assert ceo_row["success_rate"] is None
    assert ceo_row["avg_duration_seconds"] is None


async def test_summary_counts_real_tasks_by_capability_and_computes_duration(
    client, make_user, db_session_factory
):
    await make_user("viewer2@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "viewer2@example.com", "supersecret1")
    run_id, plan_id = await _seed_run_and_plan(db_session_factory)

    now = datetime.now(UTC)
    async with db_session_factory() as db:
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="k1",
                capability="graph.ceo_kickoff",
                name="CEO kickoff 1",
                status=TaskStatus.SUCCEEDED,
                created_at=now - timedelta(seconds=20),
                completed_at=now - timedelta(seconds=10),
            )
        )
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="k2",
                capability="graph.ceo_kickoff",
                name="CEO kickoff 2",
                status=TaskStatus.FAILED,
                created_at=now - timedelta(seconds=8),
                completed_at=now - timedelta(seconds=4),
            )
        )
        # Not yet terminal -- must not count toward success_rate or duration.
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="k3",
                capability="graph.ceo_kickoff",
                name="CEO kickoff 3 (running)",
                status=TaskStatus.RUNNING,
            )
        )
        # A different agent's capability -- must never be attributed to ceo-agent.
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="k4",
                capability="graph.risk_assessment",
                name="Risk assessment",
                status=TaskStatus.SUCCEEDED,
            )
        )
        await db.commit()

    resp = await client.get(
        "/api/v1/agents/analytics/summary", headers={"Authorization": f"Bearer {token}"}
    )
    ceo_row = next(r for r in resp.json() if r["agent_id"] == "ceo-agent")
    assert ceo_row["tasks_total"] == 3
    assert ceo_row["tasks_succeeded"] == 1
    assert ceo_row["tasks_failed"] == 1
    assert ceo_row["success_rate"] == 0.5
    # Two terminal tasks: a real 10s duration and a real 4s duration -> avg 7s.
    assert abs(ceo_row["avg_duration_seconds"] - 7.0) < 0.5


async def test_trend_buckets_by_completion_day_and_fills_zero_days(
    client, make_user, db_session_factory
):
    await make_user("viewer3@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "viewer3@example.com", "supersecret1")
    run_id, plan_id = await _seed_run_and_plan(db_session_factory)

    now = datetime.now(UTC)
    async with db_session_factory() as db:
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="t1",
                capability="graph.ceo_kickoff",
                name="today succeed",
                status=TaskStatus.SUCCEEDED,
                completed_at=now,
            )
        )
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="t2",
                capability="graph.ceo_kickoff",
                name="today fail",
                status=TaskStatus.FAILED,
                completed_at=now,
            )
        )
        # Older than the requested window -- must not be counted.
        db.add(
            Task(
                run_id=run_id,
                plan_id=plan_id,
                plan_key="t3",
                capability="graph.ceo_kickoff",
                name="too old",
                status=TaskStatus.SUCCEEDED,
                completed_at=now - timedelta(days=30),
            )
        )
        await db.commit()

    resp = await client.get(
        "/api/v1/agents/analytics/trend",
        params={"days": 3},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    points = resp.json()
    today_key = now.date().isoformat()
    today_point = next(p for p in points if p["date"] == today_key)
    assert today_point["tasks_succeeded"] == 1
    assert today_point["tasks_failed"] == 1
    assert all(p["date"] != (now - timedelta(days=30)).date().isoformat() for p in points)


async def test_trend_rejects_an_out_of_range_days_value(client, make_user):
    await make_user("viewer4@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "viewer4@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/agents/analytics/trend",
        params={"days": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
