"""Keeps credentials that ride in a URL out of uvicorn's request log.

A browser cannot set an `Authorization` header on a WebSocket, so every socket here authenticates
with the JWT as a `?token=` query parameter (`src.api.ws_auth`). uvicorn logs the full path, query
string included, for every WebSocket connection and every HTTP request -- so each connection wrote
the user's live access token into `docker logs`, and every broker OAuth redirect wrote its one-time
`code` / `request_token` the same way. The tokens are short-lived (15 minutes) but a log is kept,
shipped and read by people who should not be able to act as that user.

The filter rewrites the *log record*, not the request: the app still receives the real values.
"""

import logging
import re
from typing import Any

# Query parameters whose value is a credential or a one-time secret.
SENSITIVE_PARAMS = (
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "request_token",
    "code",
    "state",
    "api_key",
    "apikey",
    "api_secret",
    "client_secret",
    "secret",
    "password",
)
REDACTED = "<redacted>"

# `?token=...` / `&code=...` up to the next `&`, whitespace or quote.
_PARAM = re.compile(
    r"(?P<lead>[?&](?:" + "|".join(SENSITIVE_PARAMS) + r")=)(?P<value>[^&\s\"']*)",
    re.IGNORECASE,
)
# A JWT wherever it turns up (header.payload.signature, each part base64url): defence in depth for
# a token that reaches a log line some way other than a query parameter.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")

# The loggers uvicorn writes requests to: HTTP lines go to `uvicorn.access`, WebSocket
# connect/accept/close lines to `uvicorn.error`.
UVICORN_LOGGER_NAMES = ("uvicorn.access", "uvicorn.error")


def redact(text: str) -> str:
    text = _PARAM.sub(lambda m: m.group("lead") + REDACTED, text)
    return _JWT.sub("<jwt-redacted>", text)


class QueryCredentialRedactionFilter(logging.Filter):
    """Rewrites credentials in a record's message and %-format arguments. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_clean(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _clean(value) for key, value in record.args.items()}
        return True


def _clean(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


def install_uvicorn_log_redaction() -> list[QueryCredentialRedactionFilter]:
    """Attach the filter to uvicorn's request loggers (idempotent). Called from `configure_logging`,
    which runs when the app is imported -- after uvicorn has set up its own loggers, so the filter
    is not wiped by their configuration."""
    installed = []
    for name in UVICORN_LOGGER_NAMES:
        logger = logging.getLogger(name)
        existing = next(
            (f for f in logger.filters if isinstance(f, QueryCredentialRedactionFilter)), None
        )
        if existing is None:
            existing = QueryCredentialRedactionFilter()
            logger.addFilter(existing)
        installed.append(existing)
    return installed
