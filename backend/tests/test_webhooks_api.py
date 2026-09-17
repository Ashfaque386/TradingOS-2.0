"""Inbound webhook API tests (Build Spec §18's explicit acceptance
criterion: "signature verification rejects a forged/replayed webhook").
Outbound replies are monkeypatched at `src.notifications.inbound_router.
send_to_channel` (same "real code, injected transport/fake in tests"
posture as Phase 8's broker adapters) so these tests never attempt real
network I/O against Telegram/Discord/Slack.
"""

import hashlib
import hmac
import json
import time

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from src.api.routes.webhooks import get_channel_store_or_none
from src.core.redis_client import get_redis
from src.main import app
from src.models.organization_run import OrganizationRun, RunSource
from src.notifications.channel_store import NotificationChannelConfig, NotificationChannelStore
from src.notifications.types import NotificationChannel


@pytest.fixture(autouse=True)
def _isolate_redis_dependency(redis_client):
    """`src.core.redis_client.get_redis` is a process-wide `@lru_cache`d
    singleton never closed between tests -- reused across many separate
    pytest-asyncio function-scoped event loops, its underlying connection
    can end up bound to an already-closed loop from an earlier test.
    Overridden here to the per-test `redis_client` fixture (fresh
    connection, closed in its own teardown) so this file's several
    webhook requests per test, and several test functions in sequence,
    never touch that shared, unmanaged singleton.
    """
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield
    app.dependency_overrides.pop(get_redis, None)


@pytest.fixture(autouse=True)
async def _clear_replay_and_rate_limit_keys(redis_client):
    """This module's fixed literal update_id/interaction_id/ts values
    (chosen for readability) would otherwise collide with leftover
    `notify:seen:*`/`notify:ratelimit:*` keys left in the real, shared,
    un-flushed Redis instance by an earlier run of this same file --
    replay-guard/rate-limit state is the one genuinely persistent (TTL'd,
    not pub/sub) Redis state this phase introduces, so it's the one
    namespace worth clearing before each test.
    """
    keys = await redis_client.keys("notify:*")
    if keys:
        await redis_client.delete(*keys)


def _override_store(tmp_path) -> NotificationChannelStore:
    store = NotificationChannelStore(tmp_path / "channels.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_channel_store_or_none] = lambda: store
    return store


def _clear_override():
    app.dependency_overrides.pop(get_channel_store_or_none, None)


def _patch_outbound(monkeypatch):
    from src.notifications.senders import SendResult

    calls = []

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        calls.append((channel, text))
        return SendResult(channel, True, 200, None)

    monkeypatch.setattr("src.notifications.inbound_router.send_to_channel", fake_send_to_channel)
    return calls


# --- Telegram -------------------------------------------------------------


async def test_telegram_forged_secret_token_is_rejected(client, tmp_path):
    store = _override_store(tmp_path)
    try:
        store.set_config(
            NotificationChannel.TELEGRAM,
            NotificationChannelConfig(webhook_secret_token="real-secret", allowed_sender_ids=["1"]),
        )
        resp = await client.post(
            "/api/v1/webhooks/telegram",
            json={"update_id": 1, "message": {"from": {"id": 1}, "text": "hello"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        )
        assert resp.status_code == 401
    finally:
        _clear_override()


async def test_telegram_replayed_update_id_is_rejected(client, tmp_path, monkeypatch):
    store = _override_store(tmp_path)
    calls = _patch_outbound(monkeypatch)
    try:
        store.set_config(
            NotificationChannel.TELEGRAM,
            NotificationChannelConfig(webhook_secret_token="real-secret", allowed_sender_ids=["1"]),
        )
        headers = {"X-Telegram-Bot-Api-Secret-Token": "real-secret"}
        body = {"update_id": 42, "message": {"from": {"id": 1}, "text": "what is my status?"}}

        first = await client.post("/api/v1/webhooks/telegram", json=body, headers=headers)
        assert first.status_code == 200
        assert len(calls) == 1

        second = await client.post("/api/v1/webhooks/telegram", json=body, headers=headers)
        assert second.status_code == 401
        assert len(calls) == 1, "a replayed update must never trigger a second reply"
    finally:
        _clear_override()


async def test_telegram_unverified_sender_is_rejected(client, tmp_path, monkeypatch):
    store = _override_store(tmp_path)
    _patch_outbound(monkeypatch)
    try:
        store.set_config(
            NotificationChannel.TELEGRAM,
            NotificationChannelConfig(webhook_secret_token="real-secret", allowed_sender_ids=["1"]),
        )
        resp = await client.post(
            "/api/v1/webhooks/telegram",
            json={"update_id": 7, "message": {"from": {"id": 999}, "text": "research NIFTY"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-secret"},
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


async def test_telegram_verified_sender_objective_spawns_an_organization_run(
    client, tmp_path, monkeypatch, db_session_factory
):
    store = _override_store(tmp_path)
    calls = _patch_outbound(monkeypatch)
    try:
        store.set_config(
            NotificationChannel.TELEGRAM,
            NotificationChannelConfig(webhook_secret_token="real-secret", allowed_sender_ids=["1"]),
        )
        resp = await client.post(
            "/api/v1/webhooks/telegram",
            json={
                "update_id": 100,
                "message": {"from": {"id": 1}, "text": "research NIFTY momentum strategies"},
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": "real-secret"},
        )
        assert resp.status_code == 200
        assert json.loads(resp.text)["accepted"] is True

        async with db_session_factory() as db:
            runs = (
                (
                    await db.execute(
                        select(OrganizationRun).where(OrganizationRun.source == RunSource.WEBHOOK)
                    )
                )
                .scalars()
                .all()
            )
        assert len(runs) == 1
        assert "research NIFTY momentum strategies" in runs[0].objective
        assert len(calls) == 1, "the reply must be sent back through the originating channel"
    finally:
        _clear_override()


async def test_telegram_rate_limit_exceeded_returns_429(client, tmp_path, monkeypatch):
    store = _override_store(tmp_path)
    _patch_outbound(monkeypatch)
    try:
        store.set_config(
            NotificationChannel.TELEGRAM,
            NotificationChannelConfig(webhook_secret_token="real-secret", allowed_sender_ids=["1"]),
        )
        headers = {"X-Telegram-Bot-Api-Secret-Token": "real-secret"}
        last_status = None
        for i in range(25):
            resp = await client.post(
                "/api/v1/webhooks/telegram",
                json={
                    "update_id": 1000 + i,
                    "message": {"from": {"id": 1}, "text": "what is my status?"},
                },
                headers=headers,
            )
            last_status = resp.status_code
            if last_status == 429:
                break
        assert last_status == 429
    finally:
        _clear_override()


# --- Discord ---------------------------------------------------------------


def _discord_store_config(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    store = NotificationChannelStore(tmp_path / "discord.enc", Fernet.generate_key().decode())
    store.set_config(
        NotificationChannel.DISCORD,
        NotificationChannelConfig(public_key=public_bytes.hex(), allowed_sender_ids=["u1"]),
    )
    app.dependency_overrides[get_channel_store_or_none] = lambda: store
    return store, private_key


async def test_discord_ping_handshake_is_echoed_back(client, tmp_path):
    store, private_key = _discord_store_config(tmp_path)
    try:
        body = b'{"type": 1}'
        timestamp = str(int(time.time()))
        signature = private_key.sign(timestamp.encode("utf-8") + body).hex()

        resp = await client.post(
            "/api/v1/webhooks/discord",
            content=body,
            headers={
                "X-Signature-Ed25519": signature,
                "X-Signature-Timestamp": timestamp,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert json.loads(resp.text) == {"type": 1}
    finally:
        _clear_override()


async def test_discord_forged_signature_is_rejected(client, tmp_path):
    store, _private_key = _discord_store_config(tmp_path)
    try:
        body = b'{"type": 1}'
        timestamp = str(int(time.time()))
        resp = await client.post(
            "/api/v1/webhooks/discord",
            content=body,
            headers={
                "X-Signature-Ed25519": "00" * 64,
                "X-Signature-Timestamp": timestamp,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401
    finally:
        _clear_override()


# --- Slack -------------------------------------------------------------


def _sign_slack(signing_secret: str, timestamp: str, body: bytes) -> str:
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    return "v0=" + hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()


async def test_slack_url_verification_handshake_echoes_the_challenge(client, tmp_path):
    store = NotificationChannelStore(tmp_path / "slack.enc", Fernet.generate_key().decode())
    store.set_config(NotificationChannel.SLACK, NotificationChannelConfig(signing_secret="shh"))
    app.dependency_overrides[get_channel_store_or_none] = lambda: store
    try:
        body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
        timestamp = str(int(time.time()))
        signature = _sign_slack("shh", timestamp, body)

        resp = await client.post(
            "/api/v1/webhooks/slack",
            content=body,
            headers={
                "X-Slack-Signature": signature,
                "X-Slack-Request-Timestamp": timestamp,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.text == "abc123"
    finally:
        _clear_override()


async def test_slack_forged_signature_is_rejected(client, tmp_path):
    store = NotificationChannelStore(tmp_path / "slack2.enc", Fernet.generate_key().decode())
    store.set_config(NotificationChannel.SLACK, NotificationChannelConfig(signing_secret="shh"))
    app.dependency_overrides[get_channel_store_or_none] = lambda: store
    try:
        body = json.dumps({"type": "event_callback", "event": {"text": "hi"}}).encode()
        timestamp = str(int(time.time()))

        resp = await client.post(
            "/api/v1/webhooks/slack",
            content=body,
            headers={
                "X-Slack-Signature": "v0=" + "0" * 64,
                "X-Slack-Request-Timestamp": timestamp,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401
    finally:
        _clear_override()
