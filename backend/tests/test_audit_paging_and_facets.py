"""The audit log paged and filtered on the server: how many rows match in all (`X-Total-Count`),
the filters the console's page sets, the values it offers in its drop-downs, and an export that
honours the same filters.

Every sign-in writes an audit entry of its own, so each test seeds rows under an entity type no
other code writes (`widget`) and filters on it, as the other audit tests do.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

import src.api.routes.audit as audit_routes
from src.audit.service import write_audit_entry
from src.core.config import get_settings
from src.core.roles import Role


async def _login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _auditor(client, make_user) -> dict[str, str]:
    await make_user("auditor@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    return await _login(client, "auditor@example.com", "supersecret1")


async def _seed(db_session_factory) -> None:
    rows = [
        ("alice", "widget.created", "widget", "w-1"),
        ("alice", "widget.renamed", "widget", "w-1"),
        ("bob", "widget.created", "widget", "w-2"),
        ("bob", "widget.deleted", "widget", "w-2"),
        ("carol", "widget.created", "widget", "50%_off"),
    ]
    async with db_session_factory() as db:
        for actor, action, entity_type, entity_id in rows:
            await write_audit_entry(
                db, actor=actor, action=action, entity_type=entity_type, entity_id=entity_id
            )
        await db.commit()


async def _list(client, headers, **params):
    return await client.get(
        "/api/v1/audit/entries", params={"entity_type": "widget", **params}, headers=headers
    )


# ---- how many match in all ----


async def test_the_total_is_every_match_whatever_the_page(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    first = await _list(client, headers, limit=2)
    last = await _list(client, headers, limit=2, offset=4)

    assert len(first.json()) == 2 and first.headers["x-total-count"] == "5"
    assert len(last.json()) == 1 and last.headers["x-total-count"] == "5"


async def test_the_pages_walk_the_log_newest_first_without_gaps(
    client, make_user, db_session_factory
):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    sequences: list[int] = []
    for offset in (0, 2, 4):
        sequences += [
            e["sequence"] for e in (await _list(client, headers, limit=2, offset=offset)).json()
        ]

    assert len(sequences) == 5
    assert sequences == sorted(sequences, reverse=True)
    assert len(set(sequences)) == 5


async def test_the_total_counts_only_what_the_filters_match(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await _list(client, headers, actor="bob", limit=1)

    assert resp.headers["x-total-count"] == "2"
    assert len(resp.json()) == 1


async def test_the_total_is_readable_by_a_page_on_another_origin(
    client, make_user, db_session_factory
):
    headers = await _auditor(client, make_user)
    origin = get_settings().cors_origins_list[0]

    resp = await client.get("/api/v1/audit/entries", headers={**headers, "Origin": origin})

    assert "x-total-count" in resp.headers["access-control-expose-headers"].lower()


# ---- the filters ----


async def test_filter_by_action(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await _list(client, headers, action="widget.created")

    assert {e["actor"] for e in resp.json()} == {"alice", "bob", "carol"}
    assert resp.headers["x-total-count"] == "3"


async def test_search_is_a_case_insensitive_contains_over_actor_action_and_entity(
    client, make_user, db_session_factory
):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    by_actor = await _list(client, headers, search="ALIC")
    by_action = await _list(client, headers, search="renamed")
    by_entity = await _list(client, headers, search="w-2")

    assert [e["action"] for e in by_actor.json()] == ["widget.renamed", "widget.created"]
    assert [e["action"] for e in by_action.json()] == ["widget.renamed"]
    assert {e["actor"] for e in by_entity.json()} == {"bob"}


async def test_a_percent_or_underscore_in_the_search_is_looked_for_not_a_wildcard(
    client, make_user, db_session_factory
):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    percent = await _list(client, headers, search="%")
    underscore = await _list(client, headers, search="_")
    nothing_like_it = await _list(client, headers, search="w_1")

    assert [e["entity_id"] for e in percent.json()] == ["50%_off"]
    assert [e["entity_id"] for e in underscore.json()] == ["50%_off"]
    assert nothing_like_it.json() == []  # as a wildcard, "w_1" would have matched "w-1"


async def test_filters_combine(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await _list(client, headers, actor="alice", action="widget.created", search="w-1")

    assert [(e["actor"], e["action"]) for e in resp.json()] == [("alice", "widget.created")]


async def test_since_and_until_bound_the_time(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)
    now = datetime.now(UTC)
    hour = timedelta(hours=1)

    recent = await _list(client, headers, since=(now - hour).isoformat())
    future = await _list(client, headers, since=(now + hour).isoformat())
    past = await _list(client, headers, until=(now - hour).isoformat())
    window = await _list(
        client, headers, since=(now - hour).isoformat(), until=(now + hour).isoformat()
    )

    assert len(recent.json()) == 5
    assert future.json() == [] and future.headers["x-total-count"] == "0"
    assert past.json() == []
    assert len(window.json()) == 5


async def test_a_time_without_a_zone_is_read_as_utc(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await _list(client, headers, since="2000-01-01T00:00:00")

    assert len(resp.json()) == 5


# ---- the values the filters offer ----


async def test_facets_list_each_value_once_sorted(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await client.get("/api/v1/audit/facets", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert {"alice", "bob", "carol"} <= set(body["actors"])
    assert body["actors"] == sorted(body["actors"])
    assert "widget" in body["entity_types"]
    assert {"widget.created", "widget.renamed", "widget.deleted"} <= set(body["actions"])
    assert len(body["actions"]) == len(set(body["actions"]))
    assert body["truncated"] is False


async def test_facets_say_when_a_list_was_cut(
    client, make_user, db_session_factory, monkeypatch: pytest.MonkeyPatch
):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)
    monkeypatch.setattr(audit_routes, "_FACET_LIMIT", 2)

    body = (await client.get("/api/v1/audit/facets", headers=headers)).json()

    assert len(body["actors"]) == 2
    assert body["truncated"] is True


@pytest.mark.parametrize("role", [Role.PORTFOLIO_MANAGER, Role.RISK_MANAGER])
async def test_facets_are_not_for_the_roles_that_cannot_read_the_log(client, make_user, role):
    await make_user("someone@example.com", "supersecret1", role)
    headers = await _login(client, "someone@example.com", "supersecret1")

    assert (await client.get("/api/v1/audit/facets", headers=headers)).status_code == 403


async def test_facets_need_a_signed_in_user(client):
    assert (await client.get("/api/v1/audit/facets")).status_code in (401, 403)


# ---- the export follows the filters ----


async def test_the_export_is_what_the_filters_show(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)

    resp = await client.get(
        "/api/v1/audit/export",
        params={"entity_type": "widget", "actor": "bob", "action": "widget.deleted"},
        headers=headers,
    )

    rows = [json.loads(line) for line in resp.text.strip().splitlines()]
    assert [(r["actor"], r["action"]) for r in rows] == [("bob", "widget.deleted")]


async def test_the_export_takes_search_and_time_too(client, make_user, db_session_factory):
    headers = await _auditor(client, make_user)
    await _seed(db_session_factory)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    searched = await client.get(
        "/api/v1/audit/export",
        params={"entity_type": "widget", "search": "carol", "format": "csv"},
        headers=headers,
    )
    later = await client.get(
        "/api/v1/audit/export",
        params={"entity_type": "widget", "since": tomorrow},
        headers=headers,
    )

    assert len(searched.text.strip().splitlines()) == 2  # header + one row
    assert later.text == ""
