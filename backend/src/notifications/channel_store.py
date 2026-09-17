"""Encrypted local store for notification channel config (Build Spec §18,
§20): Telegram/Discord/Slack bot tokens, webhook URLs, inbound signing
secrets, per-channel alert-level subscriptions, and the verified-sender
allowlist that gates inbound message routing. Same Fernet-encrypted-JSON-
file design as Phase 8's `src.security.secrets_store.SecretsStore` -- a
second, separate encrypted file (`settings.notification_channel_store_path`)
under the *same* encryption key, rather than a modification to that
already-well-tested broker-credentials store: the two domains have
genuinely different shapes (a `BrokerCredentials` dataclass vs. a
per-channel bag of tokens/secrets/allowlists) and keeping them in separate
files means a corrupted or rotated key for one never touches the other.

**Verified-sender allowlist, deny-by-default.** `allowed_sender_ids`
starts empty for every channel -- an operator must explicitly list which
Telegram/Discord/Slack user/account ids may command the system before any
inbound message is ever routed to the CEO Agent (src.notifications.
inbound_router). An empty allowlist is not "allow everyone"; it is "no one
is verified yet."
"""

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import get_settings
from src.notifications.types import AlertLevel, NotificationChannel


class NotificationChannelStoreError(Exception):
    pass


@dataclass
class NotificationChannelConfig:
    enabled: bool = True

    # Outbound. Telegram sends via the Bot API (bot_token + chat_id);
    # Discord/Slack send via an Incoming Webhook URL.
    bot_token: str | None = None
    chat_id: str | None = None
    webhook_url: str | None = None

    # Inbound signature verification (src.notifications.verification).
    # Telegram: a shared secret Telegram echoes back in
    # X-Telegram-Bot-Api-Secret-Token on every webhook call. Discord: the
    # application's Ed25519 public key. Slack: the app's signing secret.
    webhook_secret_token: str | None = None
    public_key: str | None = None
    signing_secret: str | None = None

    # Inbound routing (src.notifications.inbound_router).
    allowed_sender_ids: list[str] = field(default_factory=list)

    # Outbound alert subscriptions -- which of the four fixed AlertLevel
    # values this channel receives. Defaults to none (opt-in), matching
    # "deny by default" the same way allowed_sender_ids does.
    alert_levels: list[str] = field(default_factory=list)

    def receives(self, level: AlertLevel) -> bool:
        return self.enabled and level.value in self.alert_levels


class NotificationChannelStore:
    def __init__(self, path: str | Path, encryption_key: str):
        self._path = Path(path)
        try:
            self._fernet = Fernet(encryption_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise NotificationChannelStoreError(
                "invalid notification channel store encryption key"
            ) from exc

    def _read_all(self) -> dict:
        if not self._path.exists():
            return {}
        ciphertext = self._path.read_bytes()
        if not ciphertext:
            return {}
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise NotificationChannelStoreError(
                f"notification channel store at {self._path} could not be decrypted "
                "-- wrong encryption key or a corrupted file"
            ) from exc
        return json.loads(plaintext)

    def _write_all(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(data).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        self._path.write_bytes(ciphertext)
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def get_config(self, channel: NotificationChannel) -> NotificationChannelConfig | None:
        row = self._read_all().get(channel.value)
        if row is None:
            return None
        return NotificationChannelConfig(**row)

    def set_config(self, channel: NotificationChannel, config: NotificationChannelConfig) -> None:
        data = self._read_all()
        data[channel.value] = asdict(config)
        self._write_all(data)

    def delete_config(self, channel: NotificationChannel) -> bool:
        data = self._read_all()
        if channel.value not in data:
            return False
        del data[channel.value]
        self._write_all(data)
        return True

    def list_configured_channels(self) -> list[str]:
        return sorted(self._read_all().keys())

    def channels_subscribed_to(self, level: AlertLevel) -> list[NotificationChannel]:
        result = []
        for channel in NotificationChannel:
            config = self.get_config(channel)
            if config is not None and config.receives(level):
                result.append(channel)
        return result


@lru_cache
def get_notification_channel_store() -> NotificationChannelStore:
    settings = get_settings()
    if not settings.secrets_encryption_key:
        raise NotificationChannelStoreError(
            "SECRETS_ENCRYPTION_KEY is not configured -- generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it in .env'
        )
    return NotificationChannelStore(
        settings.notification_channel_store_path, settings.secrets_encryption_key
    )
