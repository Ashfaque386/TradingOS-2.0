"""Credentials in a URL must not reach uvicorn's request log (src/observability/log_redaction.py).

Every WebSocket here authenticates with `?token=<JWT>` (browsers cannot set a header on a socket),
and broker OAuth redirects carry a one-time `code` / `request_token`; uvicorn logged both, in full,
for every connection. The shapes below are the ones uvicorn really writes.
"""

import io
import logging

import pytest

from src.observability.log_redaction import (
    REDACTED,
    SENSITIVE_PARAMS,
    QueryCredentialRedactionFilter,
    install_uvicorn_log_redaction,
    redact,
)

JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiI5MWFmNGZjOS03ZTVhLTRmMjUtYjkzMy1hODc2OWQyNDlhZDYiLCJyb2xlIjoiQWRtaW4ifQ."
    "c2lnbmF0dXJlX3BhcnRfMTIzNDU2Nzg5MA"
)


def _format(logger_name: str, message: str, *args) -> str:
    """Log through a real logger + handler, the way uvicorn does, and return the output line."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    flt = QueryCredentialRedactionFilter()
    logger.addFilter(flt)
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info(message, *args)
    finally:
        logger.removeFilter(flt)
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return stream.getvalue().strip()


def test_a_websocket_line_loses_the_token_but_keeps_everything_else():
    line = _format(
        "uvicorn.error",
        '%s - "WebSocket %s" [accepted]',
        ("172.19.0.1", 55584),
        f"/api/v1/ws/activity-feed?token={JWT}",
    )

    assert JWT not in line and "eyJ" not in line
    assert "/api/v1/ws/activity-feed?token=<redacted>" in line
    assert "172.19.0.1" in line and "[accepted]" in line


def test_an_http_access_line_loses_the_oauth_code_and_request_token():
    line = _format(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "172.19.0.1:54724",
        "GET",
        "/api/v1/broker-credentials/zerodha/callback?status=success&request_token=eUJxPmXK72&action=login&type=login",
        "1.1",
        302,
    )

    assert "eUJxPmXK72" not in line
    assert (
        "/callback?status=success&request_token=<redacted>&action=login&type=login" in line
    )  # the harmless parameters are untouched
    assert line.endswith(" 302")


def test_the_upstox_authorization_code_is_redacted():
    out = redact("GET /api/v1/broker-credentials/upstox/callback?code=8tU1yp HTTP/1.1")

    assert out == f"GET /api/v1/broker-credentials/upstox/callback?code={REDACTED} HTTP/1.1"


@pytest.mark.parametrize("name", SENSITIVE_PARAMS)
def test_every_listed_parameter_is_redacted_first_or_later_in_the_query(name):
    assert redact(f"/x?{name}=secret1") == f"/x?{name}={REDACTED}"
    assert redact(f"/x?a=1&{name}=secret1&b=2") == f"/x?a=1&{name}={REDACTED}&b=2"


def test_the_parameter_name_is_matched_case_insensitively():
    assert redact("/x?Token=abc&ACCESS_TOKEN=def") == f"/x?Token={REDACTED}&ACCESS_TOKEN={REDACTED}"


@pytest.mark.parametrize(
    "text",
    [
        "/api/v1/runs?status=done&limit=20",
        "/api/v1/market-data/option-chain/NSE_INDEX|Nifty%2050?expiry=2026-12-31",
        "/x?tokenizer=gpt&encoded=1&barcode=7&state_name=x",  # names that merely start with one
        "no query string at all",
        "",
    ],
)
def test_ordinary_urls_and_lookalike_names_are_left_alone(text):
    assert redact(text) == text


def test_a_bare_jwt_is_redacted_wherever_it_appears():
    assert redact(f"authorization failed for {JWT} (expired)") == (
        "authorization failed for <jwt-redacted> (expired)"
    )


def test_a_value_with_no_equals_or_a_dotted_non_jwt_is_not_mangled():
    assert redact("version 1.2.3 build eyJ") == "version 1.2.3 build eyJ"


def test_the_filter_never_drops_a_record_and_leaves_other_argument_types_alone():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %d", ("/x?token=abc", 200), None
    )

    assert QueryCredentialRedactionFilter().filter(record) is True
    assert record.args == (f"/x?token={REDACTED}", 200)


def test_a_literal_message_with_no_arguments_is_redacted_too():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "connect /ws?token=abc", None, None
    )

    QueryCredentialRedactionFilter().filter(record)

    assert record.getMessage() == f"connect /ws?token={REDACTED}"


def test_it_is_installed_on_both_uvicorn_loggers_exactly_once():
    first = install_uvicorn_log_redaction()
    second = install_uvicorn_log_redaction()

    assert first == second  # idempotent: the same filter objects, not a second pair
    for name in ("uvicorn.access", "uvicorn.error"):
        installed = [
            f
            for f in logging.getLogger(name).filters
            if isinstance(f, QueryCredentialRedactionFilter)
        ]
        assert len(installed) == 1


def test_importing_the_app_installs_it_so_a_real_uvicorn_line_is_protected():
    """`src.main` calls `configure_logging()` at import; this is the wiring that matters in
    production, where nothing else would install the filter."""
    import src.main  # noqa: F401

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info('%s - "WebSocket %s" [accepted]', ("1.2.3.4", 1), f"/ws/ticks?token={JWT}")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert JWT not in stream.getvalue()
    assert f"/ws/ticks?token={REDACTED}" in stream.getvalue()
