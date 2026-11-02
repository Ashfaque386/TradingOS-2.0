"""The history of improvement suggestions made for a strategy: newest first, with who asked and
when, the reviewer's verdict, and what a regeneration produced."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.core.roles import Role
from src.models.strategy_suggestion import StrategySuggestion
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


async def _headers(client, make_user, role=Role.READ_ONLY_AUDITOR) -> dict[str, str]:
    await make_user("reader@example.com", "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "reader@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _strategy_with_two_suggestions(db_session_factory) -> uuid.UUID:
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="History", objective="obj")
        first = await create_version_with_validation(db, strategy, _CODE)
        second = await create_version_with_validation(db, strategy, _CODE + "# v2\n")
        db.add_all(
            [
                StrategySuggestion(
                    strategy_id=strategy.id,
                    base_version_id=first.id,
                    suggestion_text="tighten the stop",
                    requested_by="alice",
                    status="regenerated",
                    ai_verdict={"verdict": "accept", "notes": "sensible"},
                    regenerated_version_id=second.id,
                    regeneration_diff="- stop = 5\n+ stop = 3",
                    created_at=NOW - timedelta(days=2),
                    reviewed_at=NOW - timedelta(days=2, minutes=-1),
                ),
                StrategySuggestion(
                    strategy_id=strategy.id,
                    base_version_id=second.id,
                    suggestion_text="size by volatility",
                    requested_by="bob",
                    status="pending",
                    created_at=NOW - timedelta(hours=1),
                ),
            ]
        )
        await db.commit()
        return strategy.id


async def test_the_history_is_newest_first_with_who_asked_and_when(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    strategy = await _strategy_with_two_suggestions(db_session_factory)

    resp = await client.get(f"/api/v1/strategies/{strategy}/suggestions", headers=headers)

    assert resp.status_code == 200
    rows = resp.json()
    assert [r["suggestion_text"] for r in rows] == ["size by volatility", "tighten the stop"]
    assert rows[0]["requested_by"] == "bob" and rows[0]["status"] == "pending"
    assert rows[0]["reviewed_at"] is None and rows[0]["ai_verdict"] is None
    assert datetime.fromisoformat(rows[1]["created_at"]) == NOW - timedelta(days=2)


async def test_a_regenerated_suggestion_says_which_version_it_made_and_what_changed(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    strategy = await _strategy_with_two_suggestions(db_session_factory)

    older = (
        await client.get(f"/api/v1/strategies/{strategy}/suggestions", headers=headers)
    ).json()[1]

    assert older["status"] == "regenerated"
    assert older["regenerated_version_id"] is not None
    assert older["regeneration_diff"] == "- stop = 5\n+ stop = 3"
    assert older["ai_verdict"]["verdict"] == "accept"


async def test_a_strategy_with_no_suggestions_has_an_empty_history(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="Quiet", objective="obj")

    resp = await client.get(f"/api/v1/strategies/{strategy.id}/suggestions", headers=headers)

    assert resp.status_code == 200 and resp.json() == []


async def test_only_that_strategys_suggestions_are_listed(client, make_user, db_session_factory):
    headers = await _headers(client, make_user)
    await _strategy_with_two_suggestions(db_session_factory)
    async with db_session_factory() as db:
        other = await create_strategy(db, name="Other", objective="obj")

    resp = await client.get(f"/api/v1/strategies/{other.id}/suggestions", headers=headers)

    assert resp.json() == []


async def test_an_unknown_strategy_is_a_404(client, make_user):
    headers = await _headers(client, make_user)

    resp = await client.get(f"/api/v1/strategies/{uuid.uuid4()}/suggestions", headers=headers)

    assert resp.status_code == 404


@pytest.mark.parametrize("role", list(Role))
async def test_every_role_may_read_the_history(client, make_user, db_session_factory, role):
    headers = await _headers(client, make_user, role)
    strategy = await _strategy_with_two_suggestions(db_session_factory)

    resp = await client.get(f"/api/v1/strategies/{strategy}/suggestions", headers=headers)

    assert resp.status_code == 200
