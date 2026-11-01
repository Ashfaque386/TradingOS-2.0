"""The headers every API response carries, and the API docs being off unless asked for."""

import os
import subprocess
import sys

import httpx
import pytest

from src.core.config import get_settings
from src.main import app
from src.observability.security_headers import SecurityHeadersMiddleware

EXPECTED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-site",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


async def test_an_ordinary_response_carries_the_headers(client):
    resp = await client.get("/health")

    for name, value in EXPECTED.items():
        assert resp.headers[name] == value, name
    assert "camera=()" in resp.headers["permissions-policy"]


async def test_an_error_nobody_built_a_handler_for_carries_them_too(client):
    resp = await client.get("/api/v1/nothing-here")

    assert resp.status_code == 404
    for name, value in EXPECTED.items():
        assert resp.headers[name] == value, name


async def test_a_refusal_carries_them(client):
    resp = await client.get("/api/v1/auth/me")

    assert resp.status_code in (401, 403)
    assert resp.headers["x-content-type-options"] == "nosniff"


class _CountingRedis:
    """Just the two calls the rate limiter makes, so this test needs no Redis."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        return None


async def test_the_rate_limiters_own_429_carries_them(client):
    app.state.rate_limit_per_minute = 0
    app.state.rate_limit_redis = _CountingRedis()
    try:
        resp = await client.get("/api/v1/auth/registration-status")
    finally:
        del app.state.rate_limit_redis
        app.state.rate_limit_per_minute = 10**9

    assert resp.status_code == 429
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == EXPECTED["content-security-policy"]


async def test_a_response_with_a_credential_in_it_is_not_cacheable(client):
    resp = await client.get("/api/v1/auth/registration-status")

    assert resp.headers["cache-control"] == "no-store"


async def test_strict_transport_security_is_off_until_the_operator_says_the_api_is_https(client):
    assert "strict-transport-security" not in (await client.get("/health")).headers


async def test_strict_transport_security_when_enabled(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "hsts_enabled", True)

    resp = await client.get("/health")

    assert resp.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


# ---- the middleware on its own ----


async def _through_the_middleware(path: str, response_headers: list[tuple[bytes, bytes]]):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": response_headers})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = SecurityHeadersMiddleware(inner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://t"
    ) as c:
        return await c.get(path)


async def test_a_header_the_route_already_set_is_left_alone():
    resp = await _through_the_middleware(
        "/api/v1/auth/export",
        [(b"cache-control", b"max-age=5"), (b"x-frame-options", b"SAMEORIGIN")],
    )

    assert resp.headers["cache-control"] == "max-age=5"
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert resp.headers["x-content-type-options"] == "nosniff"  # the ones it did not set are added


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
async def test_the_api_docs_pages_get_no_csp_because_they_load_their_own_script(path):
    resp = await _through_the_middleware(path, [])

    assert "content-security-policy" not in resp.headers
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_a_websocket_is_passed_through_untouched():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await SecurityHeadersMiddleware(inner)({"type": "websocket", "path": "/ws"}, None, None)

    assert seen == ["websocket"]


# ---- the docs switch ----


async def test_the_api_docs_do_not_exist_by_default(client):
    assert get_settings().api_docs_enabled is False
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404, path
    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)


def _app_urls(env_value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "API_DOCS_ENABLED"}
    if env_value is not None:
        env["API_DOCS_ENABLED"] = env_value
    result = subprocess.run(  # noqa: S603 - this test's own fixed interpreter and snippet
        [
            sys.executable,
            "-c",
            "from src.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def test_api_docs_enabled_turns_the_three_urls_on():
    assert _app_urls("true") == "/docs /redoc /openapi.json"


def test_unset_leaves_them_off():
    assert _app_urls(None) == "None None None"
