"""The bearer token Alertmanager presents when it posts an alert to this backend.

Alertmanager is a separate container and calls `POST /api/v1/webhooks/alertmanager` with no user
to sign in as, so the call is authenticated by a shared secret. The backend creates it on first
boot (a random 256-bit value) in a file on a volume of its own: written only by the backend, read
only by Alertmanager (`authorization.credentials_file`), and kept apart from the encrypted stores
under /app/secrets, whose volume Alertmanager has no reason to see. Rotating it is deleting the
file and restarting the backend; Alertmanager re-reads the file on every request.

The file is world-readable inside that volume on purpose: Alertmanager runs as a different user
than the backend, and the volume is shared with nothing else and published nowhere.
"""

import hmac
import os
import secrets
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def ensure_token(path: str | Path) -> str | None:
    """The token, creating it on first boot. `None` when no path is configured (the webhook is
    then off) or when it cannot be written -- logged, and the webhook refuses every call."""
    if not str(path):
        return None
    target = Path(path)
    try:
        existing = read_token(target)
        if existing is not None:
            return existing
        target.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        # Written to a temporary name and renamed, so Alertmanager never reads half a token.
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_text(token, encoding="utf-8")
        os.chmod(scratch, 0o644)
        os.replace(scratch, target)
        logger.info("alertmanager_token.created", path=str(target))
        return token
    except OSError:
        logger.exception("alertmanager_token.unavailable", path=str(target))
        return None


def read_token(path: str | Path) -> str | None:
    if not str(path):
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def token_matches(presented: str | None, path: str | Path) -> bool:
    """Constant-time comparison; False when there is nothing to compare against."""
    expected = read_token(path)
    if not presented or expected is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
