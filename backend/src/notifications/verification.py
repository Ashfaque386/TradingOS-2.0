"""Inbound webhook signature verification (Build Spec §18, §20): one
scheme per channel, each channel's own real mechanism, not a single
generic HMAC bolted onto all three:

- **Telegram**: a shared secret Telegram echoes back verbatim on every
  webhook call via `X-Telegram-Bot-Api-Secret-Token` (set once via
  `setWebhook`'s own `secret_token` parameter -- not modeled here, this
  module only verifies the header a real Telegram deployment would send).
  A constant-time string compare, not a cryptographic signature -- that
  is genuinely how Telegram's own webhook security works.
- **Discord**: Ed25519 signature verification over `timestamp + body`
  using the application's public key, per Discord's interactions
  webhook spec. Uses `cryptography`'s Ed25519 primitives (already a
  runtime dependency since Phase 8) -- no new dependency needed.
- **Slack**: HMAC-SHA256 over `v0:{timestamp}:{body}` using the app's
  signing secret, per Slack's request-signing spec. The timestamp
  tolerance check lives here too (Slack's own docs fold replay
  protection into signature verification itself, not a separate step) --
  `src.notifications.replay_guard` adds a second, id-based layer on top
  for all three channels.

Every `verify_*` function returns `bool`, never raises for a malformed/
missing header -- a webhook request with no signature is simply
unverified, not a 500.
"""

import hashlib
import hmac
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from src.notifications.channel_store import NotificationChannelConfig

SLACK_TIMESTAMP_TOLERANCE_SECONDS = 300


def verify_telegram_secret(
    config: NotificationChannelConfig, secret_token_header: str | None
) -> bool:
    if not config.webhook_secret_token or not secret_token_header:
        return False
    return hmac.compare_digest(config.webhook_secret_token, secret_token_header)


def verify_discord_signature(
    config: NotificationChannelConfig,
    *,
    signature_header: str | None,
    timestamp_header: str | None,
    body: bytes,
) -> bool:
    if not config.public_key or not signature_header or not timestamp_header:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(config.public_key))
        signature = bytes.fromhex(signature_header)
    except ValueError:
        return False
    message = timestamp_header.encode("utf-8") + body
    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        return False
    return True


def verify_slack_signature(
    config: NotificationChannelConfig,
    *,
    signature_header: str | None,
    timestamp_header: str | None,
    body: bytes,
    now: float | None = None,
) -> bool:
    if not config.signing_secret or not signature_header or not timestamp_header:
        return False
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if abs(current - timestamp) > SLACK_TIMESTAMP_TOLERANCE_SECONDS:
        return False

    basestring = b"v0:" + timestamp_header.encode("utf-8") + b":" + body
    computed = (
        "v0="
        + hmac.new(config.signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(computed, signature_header)
