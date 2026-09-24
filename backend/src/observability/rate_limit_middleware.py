"""General, blanket API rate limiting (Phase 21,
docs/phase20-old-vs-new-comparison.md item 24) -- a coarse ceiling across
every `/api/v1/*` HTTP request, independent of the narrower, already-real
limits scoped to specific safety-critical surfaces (live trading intents,
inbound webhooks, notification senders). Pure ASGI middleware, same shape
as `AuditLoggingMiddleware`/`CorrelationIdMiddleware`.

Keyed by client IP, not by authenticated user: this middleware runs
before FastAPI resolves `Depends(get_current_user)` (a route's
dependencies haven't executed yet when middleware decides whether to let
a request through at all), so no user identity exists to key on at this
point. IP-based is also what a *blanket* limiter is for -- catching a
compromised or misbehaving client before it can even reach a route,
regardless of whether it presents a valid token.

Scoped to `/api/v1/` paths only: `/health` and `/metrics` (mounted
outside that prefix, see `src.api.routes.health`) are excluded so a
monitoring probe polling frequently never trips it, and WebSocket
connections (`scope["type"] != "http"`) are long-lived, not repeated
requests, so they're out of scope for a per-request counter too.

The limit and Redis client are read from `scope["app"].state` on every
call, falling back to real settings/`get_redis()` -- the same
`app.state`-override shape `AuditLoggingMiddleware` already uses for its
own DB session factory, not a new pattern. This is what lets
`tests/conftest.py` set `app.state.rate_limit_per_minute` to something
huge (in effect off) for the whole ambient test suite, which shares one
fixed `127.0.0.1` client IP via `httpx.ASGITransport` and would otherwise
trip a production-sized limit partway through an unrelated test; this
middleware's own tests then override it back down to a small number to
exercise the real 429 path in isolation.
"""

import structlog
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.config import get_settings
from src.core.rate_limit import fixed_window_increment
from src.core.redis_client import get_redis

logger = structlog.get_logger(__name__)

_LIMITED_PREFIX = "/api/v1/"
_WINDOW_SECONDS = 60


def _request_origin(scope: Scope) -> str | None:
    headers = dict(scope.get("headers") or [])
    origin = headers.get(b"origin")
    return origin.decode("latin-1") if origin else None


def _client_ip(scope: Scope) -> str:
    # Trust X-Forwarded-For only if this deployment is actually behind a
    # proxy that sets it (Build Spec's loopback-by-default posture means
    # it usually isn't) -- fall back to the raw ASGI client tuple either
    # way, never trusting an unset/spoofable value silently.
    headers = dict(scope.get("headers") or [])
    forwarded = headers.get(b"x-forwarded-for")
    if forwarded:
        return forwarded.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(_LIMITED_PREFIX):
            await self.app(scope, receive, send)
            return

        app_state = scope["app"].state
        limit = getattr(app_state, "rate_limit_per_minute", None)
        if limit is None:
            limit = get_settings().api_rate_limit_per_minute
        redis = getattr(app_state, "rate_limit_redis", None) or get_redis()
        ip = _client_ip(scope)

        try:
            allowed = await fixed_window_increment(
                redis,
                key=f"api:ratelimit:{ip}",
                limit=limit,
                window_seconds=_WINDOW_SECONDS,
            )
        except Exception:  # noqa: BLE001 - Redis being unreachable must never block real traffic
            logger.warning("rate_limit_middleware.redis_unavailable", ip=ip)
            allowed = True

        if not allowed:
            logger.warning("rate_limit_middleware.exceeded", ip=ip, path=scope["path"])
            headers = {"Retry-After": str(_WINDOW_SECONDS)}
            # This response never reaches the real route, so it never
            # passes through CORSMiddleware (more inward in the stack) to
            # get its usual CORS headers added -- without this, a
            # cross-origin frontend sees an opaque CORS failure instead of
            # a legible 429. Replicated by hand here rather than reordering
            # the existing, already-tested middleware stack.
            origin = _request_origin(scope)
            if origin in get_settings().cors_origins_list:
                headers["Access-Control-Allow-Origin"] = origin
                headers["Access-Control-Allow-Credentials"] = "true"
                headers["Vary"] = "Origin"
            response = PlainTextResponse("Rate limit exceeded", status_code=429, headers=headers)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
