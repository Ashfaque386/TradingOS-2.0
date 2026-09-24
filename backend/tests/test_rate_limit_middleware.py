"""Phase 21 (docs/phase20-old-vs-new-comparison.md item 24): general,
blanket API rate limiting. Uses the real app + real Redis, with
`app.state.rate_limit_per_minute` deliberately overridden low per test
(see conftest's autouse `_disable_ambient_rate_limit`, which sets it high
for every other test in this suite).
"""

import pytest

from src.core.rate_limit import fixed_window_increment
from src.main import app


@pytest.fixture(autouse=True)
async def _clear_ratelimit_keys(redis_client):
    keys = await redis_client.keys("api:ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


@pytest.fixture
def _low_limit():
    app.state.rate_limit_per_minute = 3
    yield 3
    app.state.rate_limit_per_minute = 10**9


async def test_requests_within_the_limit_all_succeed(client, _low_limit):
    for _ in range(3):
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "x"}
        )
        # Real route reached each time (bad credentials, not rate-limited).
        assert resp.status_code == 401


async def test_exceeding_the_limit_returns_429_with_retry_after(client, _low_limit):
    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"})
    resp = await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"})
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "60"


async def test_health_and_metrics_are_never_limited(client, _low_limit):
    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"})
    # /api/v1/* is now exhausted, but /health lives outside that prefix.
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_429_carries_cors_headers_for_an_allowed_origin(client, _low_limit):
    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"})
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "x@example.com", "password": "x"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 429
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_429_has_no_cors_header_for_a_disallowed_origin(client, _low_limit):
    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x"})
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "x@example.com", "password": "x"},
        headers={"Origin": "http://evil.example.com"},
    )
    assert resp.status_code == 429
    assert "access-control-allow-origin" not in resp.headers


async def test_limit_is_independent_per_ip(redis_client):
    # Direct primitive check (the ASGI client fixture can't vary its own
    # source IP) -- confirms the counter itself is correctly IP-scoped,
    # which the HTTP-level tests above can't exercise directly.
    for _ in range(3):
        assert await fixed_window_increment(
            redis_client, key="api:ratelimit:1.1.1.1", limit=3, window_seconds=60
        )
    assert not await fixed_window_increment(
        redis_client, key="api:ratelimit:1.1.1.1", limit=3, window_seconds=60
    )
    assert await fixed_window_increment(
        redis_client, key="api:ratelimit:2.2.2.2", limit=3, window_seconds=60
    )


async def test_redis_unavailable_fails_open(client):
    """A rate limiter that fails closed when its own dependency (Redis) is
    down would turn an infrastructure blip into a full outage -- this
    middleware must let traffic through instead, same posture the LLM
    router and every other real external-dependency call in this codebase
    already takes (a graceful degradation, never an unhandled 500)."""
    app.state.rate_limit_per_minute = 1

    class _BrokenRedis:
        async def incr(self, *_a, **_kw):
            raise ConnectionError("redis unreachable")

    app.state.rate_limit_redis = _BrokenRedis()
    try:
        resp = await client.get("/health")
        assert resp.status_code == 200
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "x"}
        )
        # 401 (bad credentials), not 429 -- the request reached the real route.
        assert resp.status_code == 401
    finally:
        del app.state.rate_limit_redis
        app.state.rate_limit_per_minute = 10**9
