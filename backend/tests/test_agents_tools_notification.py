"""notification-send skill tests (Build Spec §16/§18): the Phase 3 stub
now genuinely wired to real outbound sends, via the async-skill path
src.agents.tools.registry.execute_skill added in this phase.
"""

from cryptography.fernet import Fernet

from src.agents.tools.registry import execute_skill
from src.gateway.apply import apply_config_text
from src.notifications.channel_store import (
    NotificationChannelConfig,
    get_notification_channel_store,
)
from src.notifications.senders import SendResult
from src.notifications.types import NotificationChannel

_GRANT_CONFIG = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
  agents: {
    entries: {
      'notification-agent': { skills: ['notification-send'] },
    },
  },
}
"""


async def _grant(db_session_factory) -> None:
    async with db_session_factory() as db:
        await apply_config_text(db, _GRANT_CONFIG, source="test")


def _configure_store(monkeypatch, tmp_path):
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "notification_channel_store_path", str(tmp_path / "ch.enc"))
    monkeypatch.setattr(settings, "secrets_encryption_key", Fernet.generate_key().decode())
    get_notification_channel_store.cache_clear()
    return get_notification_channel_store()


async def test_notification_send_reports_unconfigured_channel(
    monkeypatch, tmp_path, db_session_factory
):
    await _grant(db_session_factory)
    _configure_store(monkeypatch, tmp_path)

    result = await execute_skill(
        "notification-agent", "notification-send", {"channel": "telegram", "message": "hi"}
    )

    assert result["sent"] is False
    assert result["channel"] == "telegram"
    assert "not configured" in result["error"]


async def test_notification_send_rejects_an_unknown_channel(
    monkeypatch, tmp_path, db_session_factory
):
    await _grant(db_session_factory)
    _configure_store(monkeypatch, tmp_path)

    result = await execute_skill(
        "notification-agent", "notification-send", {"channel": "carrier-pigeon", "message": "hi"}
    )

    assert result["sent"] is False
    assert "unknown channel" in result["error"]


async def test_notification_send_calls_the_real_sender_for_a_configured_channel(
    monkeypatch, tmp_path, db_session_factory
):
    await _grant(db_session_factory)
    store = _configure_store(monkeypatch, tmp_path)
    store.set_config(
        NotificationChannel.TELEGRAM,
        NotificationChannelConfig(bot_token="t", chat_id="c"),
    )

    calls = []

    async def fake_send_to_channel(channel, config, text, *, transport=None):
        calls.append((channel, text))
        return SendResult(channel, True, 200, None)

    monkeypatch.setattr("src.agents.tools.notification.send_to_channel", fake_send_to_channel)

    result = await execute_skill(
        "notification-agent",
        "notification-send",
        {"channel": "telegram", "message": "tick tock"},
    )

    assert result["sent"] is True
    assert calls == [(NotificationChannel.TELEGRAM, "tick tock")]
