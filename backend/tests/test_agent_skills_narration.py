"""The skill catalog and the per-agent narration."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.agents.narration import (
    HeartbeatFact,
    NarrationFacts,
    TaskFact,
    ago,
    narrate,
)
from src.agents.skill_catalog import SKILL_INFO, build_catalog
from src.agents.tools.registry import SKILLS
from src.api.routes.agents import _config_path
from src.core.config import get_settings
from src.core.roles import Role
from src.gateway.apply import apply_config_from_file
from src.main import app

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def _facts(**over) -> NarrationFacts:
    base = dict(
        display_name="Market Analyst",
        enabled=True,
        can_disable=True,
        heartbeat_enabled=False,
        skills=("market-data-read",),
        has_data_source=True,
        capabilities=("graph.market_analysis",),
        heartbeats=(),
        tasks=(),
        now=NOW,
    )
    base.update(over)
    return NarrationFacts(**base)


# ---------------------------------------------------------------- the catalog


def test_the_catalog_describes_exactly_the_skills_the_registry_ships():
    assert set(SKILL_INFO) == set(SKILLS)
    for info in SKILL_INFO.values():
        assert info.summary.strip() and info.effect in {"read", "action"}


def test_the_placeholders_say_they_are_placeholders_and_the_real_ones_say_what_they_do():
    working = {i.skill_id for i in SKILL_INFO.values() if i.working}
    assert working == {"code-format-lint", "notification-send"}
    for skill_id in set(SKILL_INFO) - working:
        assert "placeholder" in SKILL_INFO[skill_id].summary
    assert {i.skill_id for i in SKILL_INFO.values() if i.effect == "action"} == {
        "notification-send"
    }


def test_the_catalog_lists_who_holds_each_skill_by_skill_id():
    class Agent:
        def __init__(self, agent_id: str, skills: list[str]) -> None:
            self.agent_id, self.skills = agent_id, skills

    catalog = build_catalog(
        {
            "b-agent": Agent("b-agent", ["market-data-read", "notification-send"]),  # type: ignore[dict-item]
            "a-agent": Agent("a-agent", ["market-data-read"]),  # type: ignore[dict-item]
        }
    )

    assert [e.info.skill_id for e in catalog] == sorted(SKILL_INFO)
    held = {e.info.skill_id: e.granted_to for e in catalog}
    assert held["market-data-read"] == ("a-agent", "b-agent")
    assert held["notification-send"] == ("b-agent",)
    assert held["code-format-lint"] == ()


# ---------------------------------------------------------------- the narration's sentences


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (-5, "just now"),
        (0, "just now"),
        (59, "just now"),
        (60, "1 minute ago"),
        (119, "1 minute ago"),
        (120, "2 minutes ago"),
        (3599, "59 minutes ago"),
        (3600, "1 hour ago"),
        (86_399, "23 hours ago"),
        (86_400, "1 day ago"),
        (3 * 86_400 + 5, "3 days ago"),
    ],
)
def test_how_long_ago_in_words(seconds, words):
    assert ago(NOW, NOW - timedelta(seconds=seconds)) == words


def _texts(facts: NarrationFacts, kind: str | None = None) -> list[str]:
    return [line.text for line in narrate(facts) if kind is None or line.kind == kind]


def test_an_agent_that_is_on_with_skills_and_nothing_recorded_says_what_is_missing():
    lines = narrate(_facts())
    assert [(line.kind, line.text) for line in lines] == [
        ("state", "Market Analyst is switched on."),
        ("state", "It may use 1 skill: market-data-read."),
        ("heartbeat", "Its heartbeat check is off, so it records no self-checks."),
        ("work", "No task has claimed its capabilities yet."),
    ]


def test_a_switched_off_agent_says_it_takes_no_work_and_a_protected_one_says_it_cannot_be():
    off = _texts(_facts(enabled=False), "state")
    assert off[0] == "Market Analyst is switched off, so it takes no work."
    protected = _texts(_facts(display_name="Audit Agent", can_disable=False), "state")
    assert "It cannot be switched off: governance and safety depend on it." in protected


def test_skills_are_counted_listed_in_order_and_an_empty_grant_is_said():
    assert _texts(_facts(skills=("notification-send", "code-format-lint")), "state")[1] == (
        "It may use 2 skills: code-format-lint, notification-send."
    )
    assert _texts(_facts(skills=()), "state")[1] == "It has been granted no skills."


def test_an_agent_with_no_data_source_says_it_reports_nothing_rather_than_inventing():
    gap = _texts(_facts(has_data_source=False), "gap")
    assert gap == [
        "Its subject matter has no real data source in this system yet, so it reports nothing "
        "rather than a made-up figure."
    ]
    assert _texts(_facts(), "gap") == []


def test_the_heartbeat_is_on_but_silent_or_reported_with_its_age_and_its_alerts():
    silent = _texts(_facts(heartbeat_enabled=True), "heartbeat")
    assert silent == ["Its heartbeat check is on, but none has been recorded yet."]

    ok = _texts(
        _facts(heartbeat_enabled=True, heartbeats=(HeartbeatFact("ok", _ago(minutes=5)),)),
        "heartbeat",
    )
    assert ok == ["Its last self-check was 5 minutes ago and found nothing wrong."]

    beats = (
        HeartbeatFact("alert", _ago(minutes=1)),
        HeartbeatFact("ok", _ago(minutes=11)),
        HeartbeatFact("alert", _ago(minutes=21)),
    )
    alerting = _texts(_facts(heartbeat_enabled=True, heartbeats=beats), "heartbeat")
    assert alerting == [
        "Its last self-check was 1 minute ago and found an alert.",
        "2 of its last 3 self-checks raised an alert.",
    ]
    one = _texts(
        _facts(heartbeat_enabled=True, heartbeats=(HeartbeatFact("alert", _ago(hours=2)),)),
        "heartbeat",
    )
    assert one[1] == "1 of its last 1 self-check raised an alert."


def test_an_agent_with_no_task_capability_says_the_task_engine_records_nothing_for_it():
    assert _texts(_facts(capabilities=()), "work") == [
        "It claims no task capability, so the task engine records no work for it here."
    ]


def test_tasks_are_counted_by_outcome_and_the_newest_is_described():
    tasks = (
        TaskFact("Assess the open", "running", None, _ago(minutes=2)),
        TaskFact("Assess yesterday", "succeeded", None, _ago(hours=20)),
        TaskFact("Assess last week", "failed", "broker said no", _ago(days=5)),
        TaskFact("Assess earlier", "succeeded", None, _ago(days=6)),
    )
    assert _texts(_facts(tasks=tasks), "work") == [
        "Of its last 4 tasks, 2 succeeded, 1 failed and 1 is still waiting or running.",
        "The newest, 'Assess the open', is running (started 2 minutes ago).",
    ]


def test_tasks_that_were_stopped_are_counted_apart_and_the_newest_status_is_put_in_words():
    tasks = (
        TaskFact("Assess the open", "unsatisfiable", None, _ago(minutes=3)),
        TaskFact("Assess the close", "cancelled", None, _ago(hours=3)),
        TaskFact("Assess yesterday", "succeeded", None, _ago(days=1)),
    )
    assert _texts(_facts(tasks=tasks), "work") == [
        "Of its last 3 tasks, 1 succeeded, 0 failed and 0 are still waiting or running; "
        "2 were stopped before finishing.",
        "The newest, 'Assess the open', could not run, because a task it depends on failed "
        "(started 3 minutes ago).",
    ]
    one = (TaskFact("Assess the open", "cancelled", None, _ago(minutes=3)),)
    assert _texts(_facts(tasks=one), "work")[0].endswith("; 1 was stopped before finishing.")
    waiting = (TaskFact("Assess the open", "pending", None, _ago(minutes=3)),)
    assert _texts(_facts(tasks=waiting), "work")[1].startswith(
        "The newest, 'Assess the open', is waiting on other tasks"
    )
    odd = (TaskFact("Assess the open", "mystery", None, _ago(minutes=3)),)
    assert "has the status mystery" in _texts(_facts(tasks=odd), "work")[1]


def test_a_newest_task_that_failed_gives_its_error_and_a_single_task_is_not_plural():
    failed = (TaskFact("Assess the open", "failed", "no price feed", _ago(minutes=30)),)
    assert _texts(_facts(tasks=failed), "work") == [
        "Of its last 1 task, 0 succeeded, 1 failed and 0 are still waiting or running.",
        "The newest, 'Assess the open', failed (started 30 minutes ago).",
        "It failed with: no price feed",
    ]
    no_reason = (TaskFact("Assess the open", "failed", None, _ago(minutes=30)),)
    assert len(_texts(_facts(tasks=no_reason), "work")) == 2


# ---------------------------------------------------------------- the routes


@pytest.fixture
def isolated_config(tmp_path: Path):
    path = tmp_path / "gateway.json"
    path.write_text(Path(get_settings().agent_gateway_config_path).read_text("utf-8"), "utf-8")
    app.dependency_overrides[_config_path] = lambda: path
    yield path
    app.dependency_overrides.pop(_config_path, None)


async def _headers(client, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_every_role_can_read_the_skill_catalog_with_who_holds_each_skill(
    client, make_user, db_session_factory, isolated_config
):
    async with db_session_factory() as db:
        await apply_config_from_file(db, isolated_config, source="test-seed")
    for index, role in enumerate(Role):
        headers = await _headers(client, make_user, role, f"skills-{index}@example.com")
        resp = await client.get("/api/v1/agents/skills", headers=headers)
        assert resp.status_code == 200, role
        catalog = {e["skill_id"]: e for e in resp.json()}
        assert set(catalog) == set(SKILLS)
        # the defaults reach every agent with no entry of its own
        assert len(catalog["market-data-read"]["granted_to"]) >= 25
        assert catalog["code-format-lint"]["granted_to"] == []
        assert catalog["notification-send"]["effect"] == "action"
        assert catalog["market-data-read"]["working"] is False


async def test_the_narration_of_an_agent_is_read_from_what_was_recorded(
    client, make_user, db_session_factory, isolated_config
):
    from src.models.heartbeat_log import HeartbeatLog, HeartbeatStatus

    async with db_session_factory() as db:
        await apply_config_from_file(db, isolated_config, source="test-seed")
        db.add(HeartbeatLog(agent_id="ceo-agent", status=HeartbeatStatus.ALERT, details={}))
        await db.commit()
    headers = await _headers(client, make_user, Role.READ_ONLY_AUDITOR, "narrator@example.com")

    resp = await client.get("/api/v1/agents/ceo-agent/narration", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "ceo-agent"
    lines = {(line["kind"], line["text"]) for line in body["lines"]}
    assert ("state", "CEO is switched on.") in lines  # the name the config gives it
    assert ("heartbeat", "Its last self-check was just now and found an alert.") in lines
    assert ("work", "No task has claimed its capabilities yet.") in lines


async def test_an_agent_with_no_data_source_says_so_in_its_narration(
    client, make_user, db_session_factory, isolated_config
):
    async with db_session_factory() as db:
        await apply_config_from_file(db, isolated_config, source="test-seed")
    headers = await _headers(client, make_user, Role.READ_ONLY_AUDITOR, "narrator2@example.com")

    resp = await client.get("/api/v1/agents/macro-agent/narration", headers=headers)

    assert resp.status_code == 200
    assert "gap" in {line["kind"] for line in resp.json()["lines"]}


async def test_the_narration_404s_for_an_agent_that_is_not_in_the_roster(client, make_user):
    headers = await _headers(client, make_user, Role.READ_ONLY_AUDITOR, "narrator3@example.com")
    resp = await client.get("/api/v1/agents/no-such-agent/narration", headers=headers)
    assert resp.status_code == 404
