"""Console edits of the gateway config are made on the version the person read.

A write that does not say which version it was based on, or one based on a version that has moved
on, is refused and writes nothing; two writers on the same version cannot both succeed; an edit
changes only what it names and is audited under the person, not "cli"; and the file watcher does not
apply a second time what the API already applied.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from src.api.routes import gateway as gateway_routes
from src.api.routes.agents import _config_path
from src.core.config import get_settings
from src.core.roles import Role
from src.gateway.apply import ApplyResult, apply_config_from_file
from src.gateway.state import GatewayState, get_state
from src.gateway.watcher import ConfigWatcher
from src.main import app
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus
from src.models.audit_log import AuditLog


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A per-test copy of the config both route modules write to, so the repo's own is untouched."""
    path = tmp_path / "gateway.json"
    path.write_text(Path(get_settings().agent_gateway_config_path).read_text("utf-8"), "utf-8")
    app.dependency_overrides[_config_path] = lambda: path
    monkeypatch.setattr(gateway_routes, "_config_path", lambda: path)
    yield path
    app.dependency_overrides.pop(_config_path, None)


async def _admin(
    client: AsyncClient, make_user, email: str = "gw-admin@example.com"
) -> dict[str, str]:
    await make_user(email, "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _apply(db_session_factory, path: Path) -> int:
    async with db_session_factory() as db:
        result = await apply_config_from_file(db, path, source="test-seed")
    assert result.status == ConfigVersionStatus.ACTIVE
    return result.version_id


def _match(version: int) -> dict[str, str]:
    return {"If-Match": f'"{version}"'}


async def _version_count(db_session_factory) -> int:
    async with db_session_factory() as db:
        return (await db.execute(select(func.count(AgentConfigVersion.id)))).scalar_one()


# ---------------------------------------------------------------- the version a write is based on


async def test_a_change_that_does_not_name_its_version_is_refused_and_writes_nothing(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    text, versions = config_path.read_text("utf-8"), await _version_count(db_session_factory)
    url = "/api/v1/agents/market-analyst"

    missing = await client.patch(url, json={"enabled": False}, headers=headers)
    garbled = await client.patch(
        url, json={"enabled": False}, headers=headers | {"If-Match": "abc"}
    )
    star = await client.patch(url, json={"enabled": False}, headers=headers | {"If-Match": "*"})

    assert missing.status_code == 428 and "If-Match" in missing.json()["detail"]
    assert garbled.status_code == 400 and "version number" in garbled.json()["detail"]
    assert star.status_code == 400
    assert config_path.read_text("utf-8") == text
    assert await _version_count(db_session_factory) == versions
    assert get_state().get_version_id() == version


@pytest.mark.parametrize("header", ['"{v}"', "{v}", 'W/"{v}"', ' "{v}" '])
async def test_the_version_may_be_quoted_bare_or_weak(
    client, make_user, db_session_factory, config_path, header
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)

    resp = await client.patch(
        "/api/v1/agents/market-analyst",
        json={"enabled": False},
        headers=headers | {"If-Match": header.format(v=version)},
    )

    assert resp.status_code == 200, resp.text


async def test_a_role_that_may_not_edit_is_told_so_not_that_its_header_is_missing(
    client, make_user, db_session_factory, config_path
):
    await make_user("gw-pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "gw-pm@example.com", "password": "supersecret1"}
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    await _apply(db_session_factory, config_path)

    for method, url, body in (
        ("PATCH", "/api/v1/agents/market-analyst", {"enabled": False}),
        ("PUT", "/api/v1/agents/market-analyst/identity", {"name": "X"}),
        ("PUT", "/api/v1/gateway/llm-provider-order", {"order": ["openai"]}),
        ("PUT", "/api/v1/gateway/config", {"raw_text": "{}"}),
    ):
        refused = await client.request(method, url, json=body, headers=headers)
        assert refused.status_code == 403, url


async def test_an_edit_on_a_version_that_has_moved_on_is_a_409_and_writes_nothing(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    v1 = await _apply(db_session_factory, config_path)
    first = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(v1)
    )
    assert first.status_code == 200
    v2 = first.json()["version_id"]
    text, versions = config_path.read_text("utf-8"), await _version_count(db_session_factory)

    stale = await client.patch(
        "/api/v1/agents/news-agent", json={"enabled": False}, headers=headers | _match(v1)
    )

    assert stale.status_code == 409
    assert stale.json()["detail"] == (
        f"The configuration changed since you read it (you edited version {v1}, "
        f"version {v2} is running now). Reload and make your change again."
    )
    assert config_path.read_text("utf-8") == text
    assert await _version_count(db_session_factory) == versions


async def test_two_editors_on_the_same_version_cannot_both_succeed(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)

    answers = await asyncio.gather(
        client.patch(
            "/api/v1/agents/market-analyst",
            json={"enabled": False},
            headers=headers | _match(version),
        ),
        client.patch(
            "/api/v1/agents/news-agent", json={"enabled": False}, headers=headers | _match(version)
        ),
    )

    assert sorted(a.status_code for a in answers) == [200, 409]
    listing = (await client.get("/api/v1/agents", headers=headers)).json()
    switched_off = [a["agent_id"] for a in listing if not a["enabled"]]
    assert len(switched_off) == 1  # the loser's change is not in the file


# ---------------------------------------------------------------- what an edit does


async def test_an_edit_changes_what_it_names_and_leaves_the_rest_of_the_file_alone(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    before = (await client.get("/api/v1/gateway/config", headers=headers)).json()["parsed"]

    resp = await client.patch(
        "/api/v1/agents/market-analyst",
        json={"enabled": False, "heartbeat_enabled": True, "heartbeat_interval_minutes": 15},
        headers=headers | _match(version),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active" and body["version_id"] > version
    assert body["changed"] == ["enabled", "heartbeat_enabled", "heartbeat_interval_minutes"]
    after = (await client.get("/api/v1/gateway/config", headers=headers)).json()["parsed"]
    assert after["agents"]["entries"]["market-analyst"]["enabled"] is False
    assert after["agents"]["entries"]["market-analyst"]["heartbeatIntervalMinutes"] == 15
    # everything else is as it was: the other agents' entries, the bindings, the policy, infra
    for key in ("infra", "bindings", "agentToAgentPolicy"):
        assert after[key] == before[key]
    for agent_id, entry in before["agents"]["entries"].items():
        assert after["agents"]["entries"][agent_id] == entry
    assert after["agents"]["defaults"] == before["agents"]["defaults"]
    # and the file is written to be read, not as one line
    assert config_path.read_text("utf-8").count("\n") > 20


async def test_the_agent_list_says_which_config_version_its_settings_are_from(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)

    listing = (await client.get("/api/v1/agents", headers=headers)).json()
    assert {a["config_version_id"] for a in listing} == {version}

    resp = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(version)
    )
    listing = (await client.get("/api/v1/agents", headers=headers)).json()
    assert {a["config_version_id"] for a in listing} == {resp.json()["version_id"]}


async def test_the_audit_trail_names_the_person_who_edited_not_cli(
    client, make_user, db_session_factory, config_path
):
    email = "audited-admin@example.com"
    headers = await _admin(client, make_user, email)
    version = await _apply(db_session_factory, config_path)

    patched = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(version)
    )
    identity = await client.put(
        "/api/v1/agents/ceo-agent/identity",
        json={"name": "CEO Prime"},
        headers=headers | _match(patched.json()["version_id"]),
    )

    assert patched.status_code == 200 and identity.status_code == 200
    async with db_session_factory() as db:
        applied = {
            row.entity_id: row.actor
            for row in (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "agent_gateway.config_applied")
                )
            ).scalars()
        }
        sources = {
            row.id: row.source for row in (await db.execute(select(AgentConfigVersion))).scalars()
        }
    assert applied[str(patched.json()["version_id"])] == email
    assert applied[str(identity.json()["version_id"])] == email
    assert sources[patched.json()["version_id"]] == f"api:{email}"


async def test_a_long_address_is_cut_to_fit_the_version_label_but_the_audit_names_it(
    client, make_user, db_session_factory, config_path
):
    email = "a-rather-long-address-for-an-admin@example.com"
    headers = await _admin(client, make_user, email)
    version = await _apply(db_session_factory, config_path)

    resp = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(version)
    )

    assert resp.status_code == 200, resp.text
    async with db_session_factory() as db:
        row = await db.get(AgentConfigVersion, resp.json()["version_id"])
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent_gateway.config_applied",
                    AuditLog.entity_id == str(resp.json()["version_id"]),
                )
            )
        ).scalar_one()
    assert row is not None and row.source == f"api:{email}"[:32]
    assert audit.actor == email


@pytest.mark.parametrize(
    ("agent", "body", "status", "words"),
    [
        ("market-analyst", {"enabled": True}, 409, "already so"),
        ("market-analyst", {}, 409, "Nothing was asked"),
        ("market-analyst", {"skills": ["nonsense"]}, 422, "No such skill: nonsense"),
        ("audit-agent", {"enabled": False}, 422, "Audit Agent cannot be switched off"),
        ("market-analyst", {"heartbeat_interval_minutes": 0}, 422, None),
        ("market-analyst", {"heartbeat_interval_minutes": 5000}, 422, None),
        ("no-such-agent", {"enabled": False}, 404, "not in the fixed agent roster"),
    ],
)
async def test_an_edit_that_cannot_be_made_is_refused_with_its_reason_and_writes_nothing(
    client, make_user, db_session_factory, config_path, agent, body, status, words
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    text, versions = config_path.read_text("utf-8"), await _version_count(db_session_factory)

    resp = await client.patch(
        f"/api/v1/agents/{agent}", json=body, headers=headers | _match(version)
    )

    assert resp.status_code == status, resp.text
    if words:
        assert words in resp.json()["detail"]
    assert config_path.read_text("utf-8") == text
    assert await _version_count(db_session_factory) == versions
    assert get_state().get_version_id() == version


async def test_granting_and_revoking_skills_through_the_edit(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)

    granted = await client.patch(
        "/api/v1/agents/market-analyst",
        json={"skills": ["market-data-read", "notification-send", "code-format-lint"]},
        headers=headers | _match(version),
    )
    revoked = await client.patch(
        "/api/v1/agents/market-analyst",
        json={"skills": ["code-format-lint"]},
        headers=headers | _match(granted.json()["version_id"]),
    )

    assert granted.json()["changed"] == ["skills"] and revoked.json()["changed"] == ["skills"]
    listing = {
        a["agent_id"]: a for a in (await client.get("/api/v1/agents", headers=headers)).json()
    }
    assert listing["market-analyst"]["skills"] == ["code-format-lint"]


# ---------------------------------------------------------------- the raw editor and the order


async def test_the_raw_editor_saves_on_the_version_it_read_and_is_refused_on_a_stale_one(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    v1 = await _apply(db_session_factory, config_path)
    raw = (await client.get("/api/v1/gateway/config", headers=headers)).json()["raw_text"]

    saved = await client.put(
        "/api/v1/gateway/config", json={"raw_text": raw}, headers=headers | _match(v1)
    )
    stale = await client.put(
        "/api/v1/gateway/config", json={"raw_text": raw}, headers=headers | _match(v1)
    )
    unconditional = await client.put(
        "/api/v1/gateway/config", json={"raw_text": raw}, headers=headers
    )

    assert saved.status_code == 200 and saved.json()["status"] == "active"
    assert stale.status_code == 409
    assert unconditional.status_code == 428


async def test_text_the_raw_editor_cannot_apply_is_recorded_as_rejected_and_the_file_is_kept(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    text = config_path.read_text("utf-8")

    resp = await client.put(
        "/api/v1/gateway/config",
        json={"raw_text": "{ version: 1, infra: "},
        headers=headers | _match(version),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected" and resp.json()["errors"]
    assert config_path.read_text("utf-8") == text  # a refused edit does not break the file on disk
    assert get_state().get_version_id() == version  # and the app keeps running on the good one
    async with db_session_factory() as db:
        rejected = await db.get(AgentConfigVersion, resp.json()["version_id"])
    assert rejected is not None and rejected.status == ConfigVersionStatus.REJECTED


async def test_the_llm_provider_order_is_replaced_on_the_version_read(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    current = (await client.get("/api/v1/gateway/config", headers=headers)).json()["parsed"]
    order = current["infra"]["llmProviders"]["order"]

    reordered = await client.put(
        "/api/v1/gateway/llm-provider-order",
        json={"order": list(reversed(order))},
        headers=headers | _match(version),
    )

    assert reordered.status_code == 200, reordered.text
    after = (await client.get("/api/v1/gateway/config", headers=headers)).json()["parsed"]
    assert after["infra"]["llmProviders"]["order"] == list(reversed(order))
    assert after["agents"] == current["agents"]


@pytest.mark.parametrize(
    ("order", "status", "words"),
    [
        ([], 422, None),
        (["anthropic", "mystery"], 422, "No such LLM provider: mystery"),
        (["anthropic", "anthropic"], 422, "only once"),
    ],
)
async def test_an_order_that_cannot_be_used_is_refused(
    client, make_user, db_session_factory, config_path, order, status, words
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    text = config_path.read_text("utf-8")

    resp = await client.put(
        "/api/v1/gateway/llm-provider-order",
        json={"order": order},
        headers=headers | _match(version),
    )

    assert resp.status_code == status
    if words:
        assert words in resp.json()["detail"]
    assert config_path.read_text("utf-8") == text


async def test_the_order_already_in_place_and_a_stale_order_are_refused(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    v1 = await _apply(db_session_factory, config_path)
    current = (await client.get("/api/v1/gateway/config", headers=headers)).json()["parsed"]
    order = current["infra"]["llmProviders"]["order"]

    same = await client.put(
        "/api/v1/gateway/llm-provider-order", json={"order": order}, headers=headers | _match(v1)
    )
    await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(v1)
    )
    stale = await client.put(
        "/api/v1/gateway/llm-provider-order",
        json={"order": list(reversed(order))},
        headers=headers | _match(v1),
    )

    assert same.status_code == 409 and "already in that order" in same.json()["detail"]
    assert stale.status_code == 409 and "changed since you read it" in stale.json()["detail"]


async def test_a_file_on_disk_that_is_not_valid_blocks_an_edit_on_top_of_it_with_the_reason(
    client, make_user, db_session_factory, config_path
):
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    config_path.write_text("{ not json5 at all", "utf-8")

    resp = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(version)
    )

    assert resp.status_code == 409
    assert "file on disk is not valid" in resp.json()["detail"]
    assert "Fix it in the raw editor first" in resp.json()["detail"]


async def test_a_rollback_is_attributed_to_the_person_and_to_a_rollback(
    client, make_user, db_session_factory, config_path
):
    email = "rollback-admin@example.com"
    headers = await _admin(client, make_user, email)
    v1 = await _apply(db_session_factory, config_path)
    patched = await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(v1)
    )
    assert patched.status_code == 200

    rolled = await client.post(f"/api/v1/gateway/config/versions/{v1}/rollback", headers=headers)

    assert rolled.status_code == 200, rolled.text
    async with db_session_factory() as db:
        row = await db.get(AgentConfigVersion, rolled.json()["version_id"])
        audit = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent_gateway.config_applied",
                    AuditLog.entity_id == str(rolled.json()["version_id"]),
                )
            )
        ).scalar_one()
    assert row is not None and row.source == f"rollback:{email}"[:32]
    assert audit.actor == email


# ------------------------------------------------------- the watcher and a write the API made


def test_a_write_the_api_announces_is_skipped_once_and_only_once():
    state = GatewayState()
    state.note_self_write("a config")
    digest = _sha("a config")

    assert state.consume_self_write(digest) is True
    assert state.consume_self_write(digest) is False  # the announcement is used up


def test_a_different_change_forgets_the_announcement_so_putting_the_text_back_is_applied():
    state = GatewayState()
    state.note_self_write("written by the api")

    assert state.consume_self_write(_sha("edited by a person")) is False
    assert (
        state.consume_self_write(_sha("written by the api")) is False
    )  # a person's edit, back again


def test_an_announcement_is_good_for_thirty_seconds(monkeypatch: pytest.MonkeyPatch):
    clock = [1000.0]
    monkeypatch.setattr("src.gateway.state.time.monotonic", lambda: clock[0])
    state = GatewayState()
    state.note_self_write("text")
    clock[0] += 30.5

    assert state.consume_self_write(_sha("text")) is False


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def applied_by_watcher(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The texts the watcher applied, in order; the real apply is replaced by a recorder."""
    applied: list[str] = []

    async def fake_apply(session: Any, path: Path, *, source: str) -> ApplyResult:
        applied.append(path.read_text("utf-8"))
        return ApplyResult(ConfigVersionStatus.ACTIVE, len(applied), [])

    monkeypatch.setattr("src.gateway.watcher.apply_config_from_file", fake_apply)
    monkeypatch.setattr("src.gateway.watcher.AsyncSessionLocal", lambda: _FakeSession())
    return applied


async def test_the_watcher_does_not_apply_again_what_the_api_applied(
    tmp_path: Path, applied_by_watcher: list[str]
):
    path = tmp_path / "watched.json"
    watcher = ConfigWatcher(path)

    get_state().note_self_write("written by the api")
    path.write_text("written by the api", "utf-8")
    await watcher._apply()  # noqa: SLF001  the event the write raised
    await watcher._apply()  # noqa: SLF001  and the second one editors often raise for one save

    assert applied_by_watcher == []


async def test_the_watcher_applies_a_person_s_edit_even_right_after_one_of_the_apis(
    tmp_path: Path, applied_by_watcher: list[str]
):
    path = tmp_path / "watched.json"
    watcher = ConfigWatcher(path)
    get_state().note_self_write("written by the api")
    path.write_text("written by the api", "utf-8")
    await watcher._apply()  # noqa: SLF001

    path.write_text("edited by a person", "utf-8")
    await watcher._apply()  # noqa: SLF001
    path.write_text("written by the api", "utf-8")  # the person undoes their edit
    await watcher._apply()  # noqa: SLF001

    assert applied_by_watcher == ["edited by a person", "written by the api"]


async def test_a_save_through_the_api_leaves_one_version_not_two(
    client, make_user, db_session_factory, config_path, monkeypatch
):
    """The whole path: the API writes the file, the watcher notices it, and no second version
    appears. (The watcher is run by hand on the file the API wrote.)"""
    monkeypatch.setattr("src.gateway.watcher.AsyncSessionLocal", db_session_factory)
    headers = await _admin(client, make_user)
    version = await _apply(db_session_factory, config_path)
    watcher = ConfigWatcher(config_path)
    await client.patch(
        "/api/v1/agents/market-analyst", json={"enabled": False}, headers=headers | _match(version)
    )
    versions_after_save = await _version_count(db_session_factory)

    await watcher._apply()  # noqa: SLF001  the event the API's own write raised

    assert await _version_count(db_session_factory) == versions_after_save


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
