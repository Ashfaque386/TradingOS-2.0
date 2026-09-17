"""Notification channel config store tests (Build Spec §18, §20):
encrypted round-trip, same posture as Phase 8's secrets_store tests.
"""

import pytest
from cryptography.fernet import Fernet

from src.notifications.channel_store import (
    NotificationChannelConfig,
    NotificationChannelStore,
    NotificationChannelStoreError,
)
from src.notifications.types import AlertLevel, NotificationChannel


def _store(tmp_path) -> NotificationChannelStore:
    return NotificationChannelStore(tmp_path / "channels.enc", Fernet.generate_key().decode())


def test_get_config_returns_none_when_nothing_is_configured(tmp_path):
    store = _store(tmp_path)
    assert store.get_config(NotificationChannel.TELEGRAM) is None


def test_set_then_get_round_trips(tmp_path):
    store = _store(tmp_path)
    config = NotificationChannelConfig(
        bot_token="abc123",
        chat_id="chat-1",
        allowed_sender_ids=["u1", "u2"],
        alert_levels=["kill-switch", "go-live"],
    )
    store.set_config(NotificationChannel.TELEGRAM, config)

    round_tripped = store.get_config(NotificationChannel.TELEGRAM)
    assert round_tripped.bot_token == "abc123"
    assert round_tripped.chat_id == "chat-1"
    assert round_tripped.allowed_sender_ids == ["u1", "u2"]
    assert round_tripped.alert_levels == ["kill-switch", "go-live"]


def test_delete_config_removes_it(tmp_path):
    store = _store(tmp_path)
    store.set_config(
        NotificationChannel.DISCORD, NotificationChannelConfig(webhook_url="https://x")
    )
    assert store.delete_config(NotificationChannel.DISCORD) is True
    assert store.get_config(NotificationChannel.DISCORD) is None
    assert store.delete_config(NotificationChannel.DISCORD) is False


def test_channels_subscribed_to_filters_by_alert_level_and_enabled(tmp_path):
    store = _store(tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(enabled=True, alert_levels=["kill-switch", "go-live"]),
    )
    store.set_config(
        NotificationChannel.DISCORD,
        NotificationChannelConfig(enabled=True, alert_levels=["go-live"]),
    )
    store.set_config(
        NotificationChannel.SLACK,
        NotificationChannelConfig(enabled=False, alert_levels=["kill-switch"]),
    )

    kill_switch_subs = store.channels_subscribed_to(AlertLevel.KILL_SWITCH)
    go_live_subs = store.channels_subscribed_to(AlertLevel.GO_LIVE)

    assert kill_switch_subs == [NotificationChannel.TELEGRAM]
    assert set(go_live_subs) == {NotificationChannel.TELEGRAM, NotificationChannel.DISCORD}


def test_two_stores_different_keys_cannot_decrypt_each_others_file(tmp_path):
    path = tmp_path / "shared.enc"
    store_a = NotificationChannelStore(path, Fernet.generate_key().decode())
    store_a.set_config(NotificationChannel.TELEGRAM, NotificationChannelConfig(bot_token="secret"))

    store_b = NotificationChannelStore(path, Fernet.generate_key().decode())

    with pytest.raises(NotificationChannelStoreError):
        store_b.get_config(NotificationChannel.TELEGRAM)
