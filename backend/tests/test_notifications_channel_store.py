"""Notification channel config store tests (Build Spec §18, §20):
encrypted round-trip, same posture as Phase 8's secrets_store tests.
"""

import json
from dataclasses import asdict

import pytest
from cryptography.fernet import Fernet

from src.notifications.channel_store import (
    SCHEMA_VERSION,
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


# ---- the `engine` alert level arrived after some channels were set up ----


def _legacy(tmp_path, key, channels: dict) -> NotificationChannelStore:
    """A store file as an earlier release wrote it: channels only, no schema version."""
    (tmp_path / "channels.enc").write_bytes(Fernet(key).encrypt(json.dumps(channels).encode()))
    return NotificationChannelStore(tmp_path / "channels.enc", key.decode())


def _raw(tmp_path, key) -> dict:
    return json.loads(Fernet(key).decrypt((tmp_path / "channels.enc").read_bytes()))


def _row(**overrides) -> dict:
    return {**asdict(NotificationChannelConfig()), **overrides}


def test_a_channel_that_took_kill_switch_alerts_is_subscribed_to_engine_once(tmp_path):
    key = Fernet.generate_key()
    store = _legacy(
        tmp_path,
        key,
        {"telegram": _row(bot_token="t", chat_id="c", alert_levels=["kill-switch", "daily"])},
    )

    config = store.get_config(NotificationChannel.TELEGRAM)

    assert config.alert_levels == ["kill-switch", "daily", "engine"]
    assert (config.bot_token, config.chat_id) == ("t", "c")  # nothing else is touched
    assert store.channels_subscribed_to(AlertLevel.ENGINE) == [NotificationChannel.TELEGRAM]
    # And it was saved, with the version that says the upgrade is done.
    saved = _raw(tmp_path, key)
    assert saved["telegram"]["alert_levels"] == ["kill-switch", "daily", "engine"]
    assert saved["_meta"] == {"schema_version": SCHEMA_VERSION}


def test_a_channel_that_never_took_kill_switch_alerts_is_left_alone(tmp_path):
    key = Fernet.generate_key()
    store = _legacy(
        tmp_path,
        key,
        {
            "discord": _row(webhook_url="https://x", alert_levels=["daily"]),
            "slack": _row(webhook_url="https://y", alert_levels=[]),
        },
    )

    assert store.get_config(NotificationChannel.DISCORD).alert_levels == ["daily"]
    assert store.get_config(NotificationChannel.SLACK).alert_levels == []
    assert store.channels_subscribed_to(AlertLevel.ENGINE) == []


def test_a_channel_already_on_engine_is_not_given_it_twice(tmp_path):
    key = Fernet.generate_key()
    store = _legacy(
        tmp_path, key, {"slack": _row(webhook_url="u", alert_levels=["engine", "kill-switch"])}
    )

    assert store.get_config(NotificationChannel.SLACK).alert_levels == ["engine", "kill-switch"]


def test_unticking_engine_afterwards_sticks(tmp_path):
    key = Fernet.generate_key()
    store = _legacy(tmp_path, key, {"telegram": _row(alert_levels=["kill-switch"])})
    config = store.get_config(NotificationChannel.TELEGRAM)  # upgraded here
    config.alert_levels = ["kill-switch"]  # the operator turns engine alerts off
    store.set_config(NotificationChannel.TELEGRAM, config)

    assert store.get_config(NotificationChannel.TELEGRAM).alert_levels == ["kill-switch"]
    assert store.get_config(NotificationChannel.TELEGRAM).alert_levels == ["kill-switch"]


def test_a_channel_set_up_on_this_release_is_never_upgraded(tmp_path):
    store = _store(tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM, NotificationChannelConfig(alert_levels=["kill-switch"])
    )

    assert store.get_config(NotificationChannel.TELEGRAM).alert_levels == ["kill-switch"]


def test_the_version_marker_is_not_a_channel(tmp_path):
    store = _store(tmp_path)
    store.set_config(NotificationChannel.SLACK, NotificationChannelConfig(webhook_url="u"))

    assert store.list_configured_channels() == ["slack"]


def test_an_empty_legacy_store_is_not_rewritten(tmp_path):
    key = Fernet.generate_key()
    store = _legacy(tmp_path, key, {})
    before = (tmp_path / "channels.enc").read_bytes()

    assert store.get_config(NotificationChannel.TELEGRAM) is None
    assert (tmp_path / "channels.enc").read_bytes() == before


def test_an_upgrade_that_cannot_be_saved_still_applies_and_is_tried_again(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    store = _legacy(tmp_path, key, {"telegram": _row(alert_levels=["kill-switch"])})

    def read_only(data):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "_write_all", read_only)

    assert store.get_config(NotificationChannel.TELEGRAM).alert_levels == ["kill-switch", "engine"]
    assert "_meta" not in _raw(tmp_path, key)  # nothing was written, so it will be tried again


def test_engine_is_one_of_the_levels_a_channel_can_receive():
    config = NotificationChannelConfig(alert_levels=["engine"])

    assert config.receives(AlertLevel.ENGINE)
    assert not config.receives(AlertLevel.KILL_SWITCH)
    assert AlertLevel.ENGINE.value == "engine"
