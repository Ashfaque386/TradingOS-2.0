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


class RecordingLogger:
    """Stands in for the module's structlog logger: structlog caches a logger on first use, so
    `capture_logs` cannot be relied on to see one that has already been used."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, severity: str):
        def log(event: str, **fields) -> None:
            self.events.append((severity, event, fields))

        return log

    def __getattr__(self, severity: str):
        return self._record(severity)


async def test_a_failed_alert_is_logged_with_its_alert_level_and_why(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.GO_LIVE.value]
        ),
    )
    recorder = RecordingLogger()
    monkeypatch.setattr("src.notifications.dispatch.logger", recorder)

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        from src.notifications.senders import SendResult

        return SendResult(channel, False, None, "ConnectTimeout")

    monkeypatch.setattr("src.notifications.dispatch.send_to_channel", fake_send_to_channel)

    await notify(AlertLevel.GO_LIVE, title="Go-Live", body="strategy X")

    [(severity, event, fields)] = recorder.events
    assert (severity, event) == ("warning", "notifications.alert_failed")
    # `level` is structlog's own key for the log's severity and would replace the alert's
    assert fields == {
        "channel": "telegram",
        "alert_level": AlertLevel.GO_LIVE.value,
        "error": "ConnectTimeout",
    }


async def test_a_crash_with_no_message_is_still_named(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.DAILY.value]
        ),
    )
    recorder = RecordingLogger()
    monkeypatch.setattr("src.notifications.dispatch.logger", recorder)

    async def crashing_send_to_channel(channel, config, text, *, transport=None):
        raise TimeoutError

    monkeypatch.setattr("src.notifications.dispatch.send_to_channel", crashing_send_to_channel)

    results = await notify(AlertLevel.DAILY, title="Daily", body="summary")

    assert results[0].error == "TimeoutError"
    by_event = {event: fields for _, event, fields in recorder.events}
    assert by_event["notifications.send_crashed"] == {
        "channel": "telegram",
        "alert_level": AlertLevel.DAILY.value,
    }
    assert by_event["notifications.alert_failed"]["error"] == "TimeoutError"


async def test_a_delivered_alert_is_logged_with_its_alert_level(monkeypatch, tmp_path):
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(
            bot_token="t", chat_id="c", alert_levels=[AlertLevel.KILL_SWITCH.value]
        ),
    )
    recorder = RecordingLogger()
    monkeypatch.setattr("src.notifications.dispatch.logger", recorder)

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        from src.notifications.senders import SendResult

        return SendResult(channel, True, 200, None)

    monkeypatch.setattr("src.notifications.dispatch.send_to_channel", fake_send_to_channel)

    await notify(AlertLevel.KILL_SWITCH, title="Tripped", body="paper")

    assert recorder.events == [
        (
            "info",
            "notifications.alert_sent",
            {"channel": "telegram", "alert_level": AlertLevel.KILL_SWITCH.value},
        )
    ]


async def test_notify_returns_empty_list_when_no_encryption_key_configured(monkeypatch):
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_encryption_key", None)
    get_notification_channel_store.cache_clear()

    results = await notify(AlertLevel.DAILY, title="Daily", body="summary")
    assert results == []
