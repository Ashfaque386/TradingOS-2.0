"""Outbound dispatch tests (Build Spec §18): notify() fans out only to
channels subscribed to the given alert level, best-effort per channel.
"""

from cryptography.fernet import Fernet

from src.notifications.channel_store import (
    NotificationChannelConfig,
    get_notification_channel_store,
)
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel, NotificationChannel


def _configure_store(monkeypatch, tmp_path):
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "notification_channel_store_path", str(tmp_path / "ch.enc"))
    monkeypatch.setattr(settings, "secrets_encryption_key", Fernet.generate_key().decode())
    get_notification_channel_store.cache_clear()
    return get_notification_channel_store()


async def test_notify_skips_channels_not_subscribed_to_this_alert_level(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.GO_LIVE.value]
        ),
    )

    results = await notify(AlertLevel.KILL_SWITCH, title="Kill switch tripped", body="test")

    assert results == []


async def test_notify_sends_to_a_subscribed_channel(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.KILL_SWITCH.value]
        ),
    )

    calls = []

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        calls.append((channel, text))
        from src.notifications.senders import SendResult

        return SendResult(channel, True, 200, None)

    monkeypatch.setattr("src.notifications.dispatch.send_to_channel", fake_send_to_channel)

    results = await notify(AlertLevel.KILL_SWITCH, title="Kill switch tripped", body="paper mode")

    assert len(results) == 1
    assert results[0].ok is True
    assert calls[0][0] == NotificationChannel.TELEGRAM
    assert "KILL-SWITCH" in calls[0][1]
    assert "Kill switch tripped" in calls[0][1]
    assert "paper mode" in calls[0][1]


async def test_notify_a_failing_channel_does_not_block_others(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.GO_LIVE.value]
        ),
    )
    store.set_config(
        NotificationChannel.DISCORD,
        NotificationChannelConfig(webhook_url="https://x", alert_levels=[AlertLevel.GO_LIVE.value]),
    )

    from src.notifications.senders import SendResult

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        if channel == NotificationChannel.TELEGRAM:
            raise RuntimeError("simulated crash")
        return SendResult(channel, True, 200, None)

    monkeypatch.setattr("src.notifications.dispatch.send_to_channel", fake_send_to_channel)

    results = await notify(AlertLevel.GO_LIVE, title="Go-Live", body="strategy X")

    assert len(results) == 2
    outcomes = {r.channel: r.ok for r in results}
    assert outcomes[NotificationChannel.TELEGRAM] is False
    assert outcomes[NotificationChannel.DISCORD] is True


async def test_notify_returns_empty_list_when_no_encryption_key_configured(monkeypatch):
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_encryption_key", None)
    get_notification_channel_store.cache_clear()

    results = await notify(AlertLevel.DAILY, title="Daily", body="summary")
    assert results == []
