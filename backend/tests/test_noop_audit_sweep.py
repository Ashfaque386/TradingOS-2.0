"""Endpoints that answered 2xx without changing anything -- and so left a row in
the permanent audit trail for something that never happened.

The audit middleware writes a row for every successful POST/PUT/PATCH/DELETE. The
earlier fix covered the orchestration run controls (pause/continue/retry); this is
the sweep over the rest of the API. Two remedies, by what each endpoint promises:

- no documented idempotent behaviour -> refuse (409, or 404 for deleting what is not
  there): nothing changes, and a refusal is never audited;
- idempotent by design (a repeat answers 200 with the current state) -> keep the 200
  but tell the middleware nothing changed (`skip_audit_entry`), so no row is written.

Every test here checks the trail itself, not only the status code.
"""

import pytest
from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.kill_switch_state import KillSwitchState
from src.models.user import User
from src.observability.audit_middleware import skip_audit_entry  # noqa: F401  (imported by routes)

KS_RESET = "POST /api/v1/kill-switch/{mode}/reset"
ADMIN = "/api/v1/admin/users/{user_id}"
ADMIN_DEACTIVATE = "POST " + ADMIN + "/deactivate"
ADMIN_REACTIVATE = "POST " + ADMIN + "/reactivate"
ADMIN_ROLE = "PATCH " + ADMIN + "/role"
PROMPT_ACTIVATE = "POST /api/v1/agents/{agent_id}/prompt-versions/{version_id}/activate"
GUIDANCE_DEACTIVATE = "POST /api/v1/operator-guidance/{guidance_id}/deactivate"
AUTONOMY = "POST /api/v1/live-trading/subscriptions/{subscription_id}/autonomy"
GUIDANCE = "/api/v1/operator-guidance"


async def _login(client, make_user, role: Role, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _rows(db_session_factory, action: str, entity_id: str | None = None) -> list[AuditLog]:
    async with db_session_factory() as db:
        query = select(AuditLog).where(AuditLog.action == action)
        if entity_id is not None:
            query = query.where(AuditLog.entity_id == entity_id)
        return list((await db.execute(query)).scalars().all())


# ---- kill switch: resetting a switch that is not tripped ------------------------


async def test_resetting_a_kill_switch_that_is_not_tripped_is_refused_and_not_audited(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.RISK_MANAGER, "risk-noop-ks@example.com")

    resp = await client.post("/api/v1/kill-switch/live/reset", json={}, headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "The live kill switch is not tripped; there is nothing to reset"
    assert await _rows(db_session_factory, KS_RESET) == []


async def test_a_second_reset_cannot_overwrite_who_really_reset_the_switch(
    client, make_user, db_session_factory
):
    """The record of the last genuine reset (who, when) is what an auditor reads
    after an incident; a reset of an untripped switch used to replace it."""
    headers = await _login(client, make_user, Role.RISK_MANAGER, "risk-noop-ks2@example.com")
    other = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-noop-ks2@example.com")
    await client.post(
        "/api/v1/kill-switch/live/check",
        json={"current_equity": 80000, "peak_equity": 100000},
        headers=headers,
    )
    assert (
        await client.post("/api/v1/kill-switch/live/reset", json={}, headers=headers)
    ).status_code == 200
    async with db_session_factory() as db:
        genuine = (await db.execute(select(KillSwitchState))).scalar_one()
        who, when = genuine.reset_by, genuine.reset_at
    assert who is not None and when is not None

    again = await client.post("/api/v1/kill-switch/live/reset", json={}, headers=other)

    assert again.status_code == 409
    async with db_session_factory() as db:
        after = (await db.execute(select(KillSwitchState))).scalar_one()
    assert (after.reset_by, after.reset_at) == (who, when)
    assert len(await _rows(db_session_factory, KS_RESET)) == 1


# ---- admin: deactivate / reactivate / change role that change nothing -----------


async def test_admin_changes_that_change_nothing_are_refused_and_not_audited(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-noop-au@example.com")
    target = await make_user("target-noop-au@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    base = f"/api/v1/admin/users/{target.id}"

    # Already active, already has that role: nothing to do.
    already_active = await client.post(f"{base}/reactivate", headers=admin)
    same_role = await client.patch(
        f"{base}/role", json={"role": Role.PORTFOLIO_MANAGER}, headers=admin
    )
    assert already_active.status_code == 409
    assert already_active.json()["detail"] == "This user is already active"
    assert same_role.status_code == 409
    assert "already has the role" in same_role.json()["detail"]

    # A real deactivation, then a second one.
    assert (await client.post(f"{base}/deactivate", headers=admin)).status_code == 200
    again = await client.post(f"{base}/deactivate", headers=admin)
    assert again.status_code == 409
    assert again.json()["detail"] == "This user is already deactivated"

    # And the state is exactly what the one real change left.
    async with db_session_factory() as db:
        user = await db.get(User, target.id)
    assert user.is_active is False and user.role == Role.PORTFOLIO_MANAGER

    key = str(target.id)
    assert len(await _rows(db_session_factory, ADMIN_DEACTIVATE, key)) == 1
    assert await _rows(db_session_factory, ADMIN_REACTIVATE, key) == []
    assert await _rows(db_session_factory, ADMIN_ROLE, key) == []


async def test_a_real_role_change_and_a_real_reactivation_are_still_audited(
    client, make_user, db_session_factory
):
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-noop-au2@example.com")
    target = await make_user("target-noop-au2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
    base = f"/api/v1/admin/users/{target.id}"

    assert (
        await client.patch(f"{base}/role", json={"role": Role.RISK_MANAGER}, headers=admin)
    ).status_code == 200
    assert (await client.post(f"{base}/deactivate", headers=admin)).status_code == 200
    assert (await client.post(f"{base}/reactivate", headers=admin)).status_code == 200

    key = str(target.id)
    assert len(await _rows(db_session_factory, ADMIN_ROLE, key)) == 1
    assert len(await _rows(db_session_factory, ADMIN_REACTIVATE, key)) == 1


# ---- prompt versions: activating the version that is already active -------------


async def test_reactivating_the_active_prompt_version_is_refused_and_changes_nothing(
    client, make_user, db_session_factory
):
    from tests.test_agents_api import _seed_identity

    await _seed_identity(db_session_factory, "ceo-agent")
    admin = await _login(client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-noop-pv@example.com")
    created = [
        (
            await client.post(
                "/api/v1/agents/ceo-agent/prompt-versions", json={"content": text}, headers=admin
            )
        ).json()
        for text in ("first prompt", "second prompt")
    ]
    first, second = created[0]["id"], created[1]["id"]
    url = "/api/v1/agents/ceo-agent/prompt-versions/{}/activate"
    assert (await client.post(url.format(first), headers=admin)).status_code == 200
    activated = await client.post(url.format(second), headers=admin)
    assert activated.status_code == 200
    diff_before, stamp_before = (
        activated.json()["diff_from_previous"],
        activated.json()["activated_at"],
    )
    assert diff_before  # the diff against the version it replaced

    again = await client.post(url.format(second), headers=admin)

    assert again.status_code == 409 and "already the active version" in again.json()["detail"]
    listing = (await client.get("/api/v1/agents/ceo-agent/prompt-versions", headers=admin)).json()
    row = next(v for v in listing if v["id"] == second)
    # Not re-stamped, and the diff that explains the change is still there.
    assert row["diff_from_previous"] == diff_before and row["activated_at"] == stamp_before
    assert len(await _rows(db_session_factory, "prompt_version.activated", second)) == 1
    assert (
        len(
            await _rows(
                db_session_factory,
                PROMPT_ACTIVATE,
                "ceo-agent",
            )
        )
        == 2
    )  # first + second, not the refused third


# ---- deleting credentials that are not stored -----------------------------------


async def test_deleting_credentials_that_are_not_stored_is_a_404_and_not_audited(
    client, make_user, db_session_factory, tmp_path, monkeypatch
):
    from tests.test_broker_credentials_api import _clear_override as clear_broker
    from tests.test_broker_credentials_api import _override_store as broker_store
    from tests.test_llm_providers_api import _clear_overrides as clear_llm
    from tests.test_llm_providers_api import _override_store as llm_store
    from tests.test_notification_channels_api import _clear_override as clear_channel
    from tests.test_notification_channels_api import _override_store as channel_store

    (tmp_path / "b").mkdir()
    (tmp_path / "l").mkdir()
    (tmp_path / "n").mkdir()
    broker_store(tmp_path / "b")
    llm_store(tmp_path / "l", monkeypatch)
    channel_store(tmp_path / "n")
    try:
        admin = await _login(
            client, make_user, Role.SYSTEM_ADMINISTRATOR, "admin-noop-del@example.com"
        )

        broker = await client.delete("/api/v1/broker-credentials/upstox", headers=admin)
        llm = await client.delete("/api/v1/settings/llm-providers/openai", headers=admin)
        channel = await client.delete("/api/v1/notification-channels/slack", headers=admin)

        assert broker.status_code == llm.status_code == channel.status_code == 404
        assert "no upstox credentials are stored" in broker.json()["detail"]
        assert "no openai credentials are stored" in llm.json()["detail"]
        assert "slack channel is not configured" in channel.json()["detail"]
        for action in (
            "DELETE /api/v1/broker-credentials/{broker}",
            "DELETE /api/v1/settings/llm-providers/{provider}",
            "DELETE /api/v1/notification-channels/{channel}",
            "llm_provider_credentials.deleted",
            "notification_channels.deleted",
        ):
            assert await _rows(db_session_factory, action) == [], action
    finally:
        clear_broker()
        clear_llm()
        clear_channel()


# ---- idempotent by design: the 200 stays, the false audit row does not ----------


async def test_deactivating_guidance_twice_keeps_the_200_but_audits_only_the_change(
    client, make_user, db_session_factory
):
    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-noop-gd@example.com")
    created = await client.post(
        "/api/v1/operator-guidance", json={"message": "be careful"}, headers=headers
    )
    guidance_id = created.json()["id"]
    url = f"/api/v1/operator-guidance/{guidance_id}/deactivate"

    first = await client.post(url, headers=headers)
    second = await client.post(url, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["is_active"] is False and second.json()["is_active"] is False
    action = GUIDANCE_DEACTIVATE
    assert (
        len(await _rows(db_session_factory, action, guidance_id)) == 1
    )  # the change, not the repeat
    assert len(await _rows(db_session_factory, "operator_guidance.deactivated", guidance_id)) == 1


async def test_the_audit_skip_never_hides_a_failure_or_an_unrelated_request(
    client, make_user, db_session_factory
):
    """The skip is per request: it must not leak into the next one."""
    headers = await _login(client, make_user, Role.PORTFOLIO_MANAGER, "pm-noop-gd2@example.com")
    first = (
        await client.post("/api/v1/operator-guidance", json={"message": "one"}, headers=headers)
    ).json()
    await client.post(f"/api/v1/operator-guidance/{first['id']}/deactivate", headers=headers)
    await client.post(
        f"/api/v1/operator-guidance/{first['id']}/deactivate", headers=headers
    )  # skipped

    second = (
        await client.post("/api/v1/operator-guidance", json={"message": "two"}, headers=headers)
    ).json()
    await client.post(f"/api/v1/operator-guidance/{second['id']}/deactivate", headers=headers)

    action = GUIDANCE_DEACTIVATE
    assert len(await _rows(db_session_factory, action, first["id"])) == 1
    assert (
        len(await _rows(db_session_factory, action, second["id"])) == 1
    )  # a real one right after a skip


@pytest.mark.parametrize("enabled", [True, False])
async def test_setting_autonomy_to_the_state_it_already_has_is_not_audited(
    client, make_user, db_session_factory, enabled
):
    from tests.test_live_trading_api import _auth, _promote_to_live_eligible
    from tests.test_live_trading_api import _login as login_token

    await make_user(
        f"admin-noop-au-{enabled}@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR
    )
    admin_token = await login_token(client, f"admin-noop-au-{enabled}@example.com", "supersecret1")
    await make_user(f"risk-noop-au-{enabled}@example.com", "supersecret1", Role.RISK_MANAGER)
    risk_token = await login_token(client, f"risk-noop-au-{enabled}@example.com", "supersecret1")
    strategy_id = await _promote_to_live_eligible(client, admin_token, risk_token)
    subscription = (
        await client.post(
            "/api/v1/live-trading/subscriptions",
            json={"strategy_id": strategy_id, "symbol": "DEMOSTOCK", "broker_name": "zerodha"},
            headers=_auth(admin_token),
        )
    ).json()
    url = f"/api/v1/live-trading/subscriptions/{subscription['id']}/autonomy"
    action = AUTONOMY

    # Autonomy starts disabled: when testing "disable", the first call is itself a repeat.
    first = await client.post(url, json={"enabled": enabled}, headers=_auth(risk_token))
    second = await client.post(url, json={"enabled": enabled}, headers=_auth(risk_token))

    assert first.status_code == second.status_code == 200
    expected_rows = 1 if enabled else 0
    assert len(await _rows(db_session_factory, action, subscription["id"])) == expected_rows
