"""Notification channel config API tests (Build Spec §18, §20): write-only
end to end, SystemAdministrator-only writes, same posture as
test_broker_credentials_api.py.

The /test and /telegram/detect-chat-id tests (Settings redesign) below
exercise real code paths: /test calls the genuine send_to_channel used by
the real alert-dispatch path (monkeypatched at the call site the same way
test_notifications_dispatch.py already does, since send_to_channel takes
its transport as a plain kwarg, not a FastAPI dependency), and
detect-chat-id calls Telegram's real getUpdates API shape against an
injected httpx.MockTransport (get_telegram_http_client *is* a FastAPI
dependency, so it's overridden the same way test_broker_oauth_api.py
overrides get_oauth_http_client).
"""

import httpx
from cryptography.fernet import Fernet
from httpx import AsyncClient

from src.api.routes.notification_channels import get_channel_store, get_telegram_http_client
from src.core.roles import Role
from src.main import app
from src.notifications.channel_store import NotificationChannelConfig, NotificationChannelStore
from src.notifications.senders import SendResult
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


def _override_telegram_http_client(handler):
    async def _fake_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    app.dependency_overrides[get_telegram_http_client] = _fake_client


def _clear_telegram_override():
    app.dependency_overrides.pop(get_telegram_http_client, None)


async def test_test_message_requires_system_administrator(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("pm2@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "pm2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/slack/test",
            json={"webhook_url": "https://hooks.slack.com/x"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_test_message_uses_saved_config_and_reports_success(
    client, make_user, tmp_path, monkeypatch
):
    """Confirms the endpoint calls the real send_to_channel dispatch used
    by the alert-dispatch path with the saved config when the request body
    omits overrides -- monkeypatched at the call site the same way
    test_notifications_dispatch.py already does, since send_to_channel's
    transport is a plain kwarg, not a FastAPI dependency.
    """
    store = _override_store(tmp_path)
    store.set_config(
        NotificationChannel.SLACK, NotificationChannelConfig(webhook_url="https://saved.example")
    )

    seen = {}

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        seen["channel"] = channel
        seen["webhook_url"] = config.webhook_url
        seen["text"] = text
        return SendResult(NotificationChannel.SLACK, True, 200, None)

    monkeypatch.setattr(
        "src.api.routes.notification_channels.send_to_channel", fake_send_to_channel
    )

    try:
        await make_user("admin6@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin6@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/slack/test", json={}, headers=_auth(token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["status_code"] == 200
        assert body["tested_at"]
        assert seen["channel"] == NotificationChannel.SLACK
        assert seen["webhook_url"] == "https://saved.example"
    finally:
        _clear_override()


async def test_test_message_reports_failure_from_dispatch(client, make_user, tmp_path, monkeypatch):
    _override_store(tmp_path)

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        return SendResult(NotificationChannel.DISCORD, False, 404, "Unknown Webhook")

    monkeypatch.setattr(
        "src.api.routes.notification_channels.send_to_channel", fake_send_to_channel
    )

    try:
        await make_user("admin7@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin7@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/discord/test",
            json={"webhook_url": "https://discord.com/api/webhooks/bad"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["status_code"] == 404
        assert body["error"] == "Unknown Webhook"
    finally:
        _clear_override()


async def test_detect_chat_id_requires_system_administrator(client, make_user, tmp_path):
    _override_store(tmp_path)
    try:
        await make_user("rm2@example.com", "supersecret1", Role.RISK_MANAGER)
        token = await _login(client, "rm2@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/telegram/detect-chat-id",
            json={"bot_token": "123:ABC"},
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_detect_chat_id_finds_most_recent_chat(client, make_user, tmp_path):
    _override_store(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123:ABC/getUpdates"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 111, "first_name": "Old"}},
                    },
                    {
                        "update_id": 2,
                        "message": {"chat": {"id": 222, "title": "Trading Alerts"}},
                    },
                ],
            },
        )

    _override_telegram_http_client(handler)
    try:
        await make_user("admin8@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin8@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/telegram/detect-chat-id",
            json={"bot_token": "123:ABC"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["chat_id"] == "222"
        assert body["chat_label"] == "Trading Alerts"
    finally:
        _clear_override()
        _clear_telegram_override()


async def test_detect_chat_id_no_messages_yet_reports_guidance(client, make_user, tmp_path):
    _override_store(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    _override_telegram_http_client(handler)
    try:
        await make_user("admin9@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin9@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/telegram/detect-chat-id",
            json={"bot_token": "123:ABC"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["chat_id"] is None
        assert "send your bot a message" in body["detail"]
    finally:
        _clear_override()
        _clear_telegram_override()


async def test_detect_chat_id_invalid_bot_token_reports_telegram_error(client, make_user, tmp_path):
    _override_store(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    _override_telegram_http_client(handler)
    try:
        await make_user("admin10@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "admin10@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/notification-channels/telegram/detect-chat-id",
            json={"bot_token": "bad-token"},
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "401" in body["detail"]
    finally:
        _clear_override()
        _clear_telegram_override()
