"""Notification channel config API tests (Build Spec §18, §20): write-only
end to end, SystemAdministrator-only writes, same posture as
test_broker_credentials_api.py.
"""

from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.notification_channels import get_channel_store
from src.core.roles import Role
from src.main import app
from src.notifications.channel_store import NotificationChannelConfig, NotificationChannelStore
from src.notifications.types import NotificationChannel


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_store(tmp_path) -> NotificationChannelStore:
    store = NotificationChannelStore(tmp_path / "channels.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_channel_store] = lambda: store
    return store


def _clear_override():
    app.dependency_overrides.pop(get_channel_store, None)


async def test_write_requires_system_administrator(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "pm@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/telegram",
            json={"bot_token": "t", "chat_id": "c"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_write_then_status_never_echoes_secrets(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin@example.com", "supersecret1")

        write_resp = await client.post(
            "/api/v1/notification-channels/telegram",
            json={
                "bot_token": "super-secret-token",
                "chat_id": "chat-1",
                "webhook_secret_token": "shh",
                "allowed_sender_ids": ["1", "2"],
                "alert_levels": ["kill-switch", "go-live"],
            },
            headers=_auth(token),
        )
        assert write_resp.status_code == 204

        list_resp = await client.get("/api/v1/notification-channels", headers=_auth(token))
        assert list_resp.status_code == 200
        body_text = list_resp.text
        assert "super-secret-token" not in body_text
        assert "shh" not in body_text

        statuses = {row["channel"]: row for row in list_resp.json()}
        assert statuses["telegram"]["configured"] is True
        assert statuses["telegram"]["enabled"] is True
        assert statuses["telegram"]["allowed_sender_ids"] == ["1", "2"]
        assert statuses["telegram"]["alert_levels"] == ["kill-switch", "go-live"]
        assert statuses["discord"]["configured"] is False
    finally:
        _clear_override()


async def test_unknown_channel_is_404(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/not-a-real-channel",
            json={"bot_token": "t"},
            headers=_auth(token),
        )
        assert resp.status_code == 404
    finally:
        _clear_override()


async def test_delete_requires_system_administrator(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    try:
        store.set_config(NotificationChannel.SLACK, NotificationChannelConfig(webhook_url="x"))

        await make_user("rm@example.com", "supersecret1", Role.RISK_MANAGER)
        token = await _login(client, "rm@example.com", "supersecret1")

        resp = await client.delete("/api/v1/notification-channels/slack", headers=_auth(token))
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_delete_removes_configuration(client, make_user, tmp_path):
    store = _override_store(tmp_path)
    try:
        store.set_config(NotificationChannel.SLACK, NotificationChannelConfig(webhook_url="x"))

        await make_user("admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin3@example.com", "supersecret1")

        resp = await client.delete("/api/v1/notification-channels/slack", headers=_auth(token))
        assert resp.status_code == 204
        assert store.get_config(NotificationChannel.SLACK) is None
    finally:
        _clear_override()
