"""Saving a notification channel's settings must not erase its credentials.

The Settings page toggles an alert level by sending only `{enabled, alert_levels}`. The write
endpoint used to build the whole stored config from the request, so the toggle silently emptied the
channel's `bot_token`, `chat_id`, `webhook_url` and inbound secrets while it still reported
`configured: true`. A field a request leaves out now keeps its stored value, and `PATCH` changes the
non-secret settings without any way to touch a secret.
"""

import pytest

from src.core.roles import Role
from src.notifications.channel_store import NotificationChannelConfig
from src.notifications.types import NotificationChannel
from tests.test_notification_channels_api import _auth, _clear_override, _login, _override_store

URL = "/api/v1/notification-channels"


@pytest.fixture
async def admin(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    await make_user("admin-ch@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "admin-ch@example.com", "supersecret1")
    yield store, token
    _clear_override()


def _telegram(store) -> NotificationChannelConfig:
    config = store.get_config(NotificationChannel.TELEGRAM)
    assert config is not None
    return config


async def _configure_telegram(client, token):
    resp = await client.post(
        f"{URL}/telegram",
        json={
            "bot_token": "the-bot-token",
            "chat_id": "chat-9",
            "webhook_secret_token": "inbound-secret",
            "allowed_sender_ids": ["111"],
            "alert_levels": ["kill-switch"],
        },
        headers=_auth(token),
    )
    assert resp.status_code == 204


async def test_a_post_that_leaves_secrets_out_keeps_them(client, admin):
    store, token = admin
    await _configure_telegram(client, token)

    resp = await client.post(
        f"{URL}/telegram",
        json={"enabled": True, "alert_levels": ["kill-switch", "daily"]},
        headers=_auth(token),
    )

    assert resp.status_code == 204
    config = _telegram(store)
    assert config.alert_levels == ["kill-switch", "daily"]
    assert config.bot_token == "the-bot-token"
    assert config.chat_id == "chat-9"
    assert config.webhook_secret_token == "inbound-secret"
    assert config.allowed_sender_ids == ["111"]


async def test_a_post_can_still_clear_a_field_by_sending_null_or_an_empty_list(client, admin):
    store, token = admin
    await _configure_telegram(client, token)

    resp = await client.post(
        f"{URL}/telegram",
        json={"chat_id": None, "allowed_sender_ids": []},
        headers=_auth(token),
    )

    assert resp.status_code == 204
    config = _telegram(store)
    assert config.chat_id is None
    assert config.allowed_sender_ids == []
    assert config.bot_token == "the-bot-token"  # not mentioned, so untouched


async def test_a_first_post_still_starts_from_the_deny_by_default_config(client, admin):
    store, token = admin

    resp = await client.post(
        f"{URL}/slack", json={"webhook_url": "https://hooks.example/x"}, headers=_auth(token)
    )

    assert resp.status_code == 204
    config = store.get_config(NotificationChannel.SLACK)
    assert config is not None
    assert config.webhook_url == "https://hooks.example/x"
    assert config.alert_levels == [] and config.allowed_sender_ids == []


async def test_patch_changes_settings_and_cannot_touch_a_secret(client, admin):
    store, token = admin
    await _configure_telegram(client, token)

    resp = await client.patch(
        f"{URL}/telegram",
        json={"alert_levels": ["sign-off"], "enabled": False, "bot_token": "evil"},
        headers=_auth(token),
    )

    assert resp.status_code == 204
    config = _telegram(store)
    assert config.alert_levels == ["sign-off"] and config.enabled is False
    assert config.bot_token == "the-bot-token"  # the PATCH body has no such field: ignored


async def test_patch_can_unsubscribe_a_channel_from_everything(client, admin):
    store, token = admin
    await _configure_telegram(client, token)

    resp = await client.patch(f"{URL}/telegram", json={"alert_levels": []}, headers=_auth(token))

    assert resp.status_code == 204
    assert _telegram(store).alert_levels == []


async def test_patch_of_an_unconfigured_channel_is_a_404(client, admin):
    _store, token = admin

    resp = await client.patch(f"{URL}/discord", json={"enabled": False}, headers=_auth(token))

    assert resp.status_code == 404


async def test_patch_with_nothing_to_change_is_a_422(client, admin):
    _store, token = admin
    await _configure_telegram(client, token)

    resp = await client.patch(f"{URL}/telegram", json={}, headers=_auth(token))

    assert resp.status_code == 422


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_an_unknown_alert_level_is_rejected_instead_of_stored(client, admin, method):
    store, token = admin
    await _configure_telegram(client, token)

    resp = await getattr(client, method)(
        f"{URL}/telegram",
        json={"alert_levels": ["kill-switch", "kill_switch"]},
        headers=_auth(token),
    )

    assert resp.status_code == 422
    assert "kill_switch" in resp.text
    assert _telegram(store).alert_levels == ["kill-switch"]  # untouched


async def test_patch_requires_system_administrator(client, make_user, admin):
    _store, _token = admin
    await make_user("risk-ch@example.com", "supersecret1", Role.RISK_MANAGER)
    token = await _login(client, "risk-ch@example.com", "supersecret1")

    resp = await client.patch(f"{URL}/telegram", json={"enabled": False}, headers=_auth(token))

    assert resp.status_code == 403


async def test_a_settings_change_is_audited_with_before_and_after_and_no_secret(client, admin):
    from sqlalchemy import select

    from src.models.audit_log import AuditLog

    _store, token = admin
    await _configure_telegram(client, token)
    await client.patch(f"{URL}/telegram", json={"alert_levels": ["daily"]}, headers=_auth(token))

    from src.main import app

    async with app.state.db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "notification_channels.settings_changed"
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].details == {
        "before": {"alert_levels": ["kill-switch"]},
        "after": {"alert_levels": ["daily"]},
    }
    assert "the-bot-token" not in str(rows[0].details)
