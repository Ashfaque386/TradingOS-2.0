"""Inbound webhook signature verification tests (Build Spec §18): the
explicit acceptance criterion -- "signature verification rejects a
forged/replayed webhook" -- for all three channels' real schemes
(Telegram secret-token, Discord Ed25519, Slack HMAC-SHA256).
"""

import hashlib
import hmac
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.notifications.channel_store import NotificationChannelConfig
from src.notifications.verification import (
    verify_discord_signature,
    verify_slack_signature,
    verify_telegram_secret,
)

# --- Telegram ----------------------------------------------------------


def test_telegram_accepts_the_correct_secret_token():
    config = NotificationChannelConfig(webhook_secret_token="real-secret")
    assert verify_telegram_secret(config, "real-secret") is True


def test_telegram_rejects_a_forged_secret_token():
    config = NotificationChannelConfig(webhook_secret_token="real-secret")
    assert verify_telegram_secret(config, "forged-secret") is False


def test_telegram_rejects_a_missing_header():
    config = NotificationChannelConfig(webhook_secret_token="real-secret")
    assert verify_telegram_secret(config, None) is False


def test_telegram_rejects_when_no_secret_is_configured():
    config = NotificationChannelConfig(webhook_secret_token=None)
    assert verify_telegram_secret(config, "anything") is False


# --- Discord (Ed25519) --------------------------------------------------


def _discord_config_and_key() -> tuple[NotificationChannelConfig, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    config = NotificationChannelConfig(public_key=public_bytes.hex())
    return config, private_key


def _sign_discord(private_key: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return private_key.sign(timestamp.encode("utf-8") + body).hex()


def test_discord_accepts_a_genuinely_signed_request():
    config, private_key = _discord_config_and_key()
    body = b'{"type": 1}'
    timestamp = "1700000000"
    signature = _sign_discord(private_key, timestamp, body)

    assert (
        verify_discord_signature(
            config, signature_header=signature, timestamp_header=timestamp, body=body
        )
        is True
    )


def test_discord_rejects_a_forged_signature():
    config, private_key = _discord_config_and_key()
    body = b'{"type": 1}'
    timestamp = "1700000000"
    real_signature = _sign_discord(private_key, timestamp, body)
    # Flip the last hex character -- a forged signature of the right shape.
    forged = real_signature[:-1] + ("0" if real_signature[-1] != "0" else "1")

    assert (
        verify_discord_signature(
            config, signature_header=forged, timestamp_header=timestamp, body=body
        )
        is False
    )


def test_discord_rejects_a_signature_for_a_tampered_body():
    config, private_key = _discord_config_and_key()
    timestamp = "1700000000"
    signature = _sign_discord(private_key, timestamp, b'{"type": 1}')

    assert (
        verify_discord_signature(
            config,
            signature_header=signature,
            timestamp_header=timestamp,
            body=b'{"type": 2, "malicious": true}',
        )
        is False
    )


def test_discord_rejects_missing_headers():
    config, _ = _discord_config_and_key()
    assert (
        verify_discord_signature(
            config, signature_header=None, timestamp_header="1700000000", body=b"{}"
        )
        is False
    )


# --- Slack (HMAC-SHA256) -------------------------------------------------


def _sign_slack(signing_secret: str, timestamp: str, body: bytes) -> str:
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    return "v0=" + hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()


def test_slack_accepts_a_genuinely_signed_request():
    config = NotificationChannelConfig(signing_secret="shh")
    body = b'{"type": "url_verification", "challenge": "abc"}'
    timestamp = str(int(time.time()))
    signature = _sign_slack("shh", timestamp, body)

    assert (
        verify_slack_signature(
            config, signature_header=signature, timestamp_header=timestamp, body=body
        )
        is True
    )


def test_slack_rejects_a_forged_signature():
    config = NotificationChannelConfig(signing_secret="shh")
    body = b'{"text": "hello"}'
    timestamp = str(int(time.time()))
    forged = _sign_slack("wrong-secret", timestamp, body)

    assert (
        verify_slack_signature(
            config, signature_header=forged, timestamp_header=timestamp, body=body
        )
        is False
    )


def test_slack_rejects_a_stale_timestamp_even_with_a_correct_signature():
    """This is the replay-defense half of Slack's own signing spec --
    a correctly-signed but old request (a replayed, once-valid payload)
    must still be rejected."""
    config = NotificationChannelConfig(signing_secret="shh")
    body = b'{"text": "hello"}'
    stale_timestamp = str(int(time.time()) - 3600)
    signature = _sign_slack("shh", stale_timestamp, body)

    assert (
        verify_slack_signature(
            config, signature_header=signature, timestamp_header=stale_timestamp, body=body
        )
        is False
    )


def test_slack_rejects_when_no_signing_secret_is_configured():
    config = NotificationChannelConfig(signing_secret=None)
    timestamp = str(int(time.time()))
    signature = _sign_slack("shh", timestamp, b"{}")
    assert (
        verify_slack_signature(
            config, signature_header=signature, timestamp_header=timestamp, body=b"{}"
        )
        is False
    )
