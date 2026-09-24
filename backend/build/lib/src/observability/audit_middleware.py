"""Retrofits an audit entry onto every mutating HTTP action across every
prior phase (Build Spec §19's acceptance line: "every mutating action from
every previous phase now produces a verifiable, exportable audit entry"),
without hand-editing the ~40 existing POST/PUT/PATCH/DELETE route handlers
across Phases 0-10. Pure ASGI middleware, added innermost (see main.py's
`add_middleware` ordering — `CorrelationIdMiddleware` wraps this one) so
`write_audit_entry`'s automatic correlation-id pickup sees the same ID
that request's log lines already carry.

Fires only for a non-GET/HEAD/OPTIONS request that reached a real route
and returned a 2xx -- a 401/403/404/422/5xx never mutated anything, so it
never gets an entry here. The handful of orchestration functions that
already call `write_audit_entry` directly with rich, business-specific
detail (live trading approvals, Agent Gateway config apply/reject, Shadow
Mode) are unaffected and still fire in addition to this generic one; this
middleware's own entry is deliberately minimal (method, path, path/query
params, a best-effort entity id) because it cannot know a route's business
semantics the way that route's own code does -- it exists for blanket
coverage, not for detail.
"""

import json

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.audit.service import write_audit_entry

logger = structlog.get_logger(__name__)

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class AuditLoggingMiddleware:
    def __init__(self, app: ASGIApp, *, session_factory_attr: str = "db_session_factory") -> None:
        self.app = app
        self._session_factory_attr = session_factory_attr

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _MUTATING_METHODS:
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})
        request = Request(scope, receive=receive)
        status_holder: dict[str, int] = {}
        body_chunks: list[bytes] = []

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status_code"] = message["status"]
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, send_wrapper)

        status_code = status_holder.get("status_code", 500)
        if not (200 <= status_code < 300):
            return

        route = scope.get("route")
        path_template = route.path if route is not None else scope.get("path", "")

        try:
            async with request.app.state.db_session_factory() as db:
                await write_audit_entry(
                    db,
                    actor=self._extract_actor(scope),
                    action=f"{scope['method']} {path_template}",
                    entity_type=self._extract_entity_type(path_template),
                    entity_id=self._extract_entity_id(dict(request.path_params), body_chunks),
                    details={
                        "status_code": status_code,
                        "path_params": dict(request.path_params),
                        "query_params": dict(request.query_params),
                    },
                )
                await db.commit()
        except AttributeError:
            logger.warning("audit_middleware.no_session_factory_configured")
        except Exception:  # noqa: BLE001 - a failed audit write must never break a response already sent
            logger.exception("audit_middleware.write_failed", path=path_template)

    @staticmethod
    def _extract_actor(scope: Scope) -> str:
        user = scope.get("state", {}).get("current_user")
        return user.email if user is not None else "anonymous"

    @staticmethod
    def _extract_entity_type(path_template: str) -> str | None:
        # ".../api/v1/<resource>/..." -- the segment right after the
        # version prefix is the resource this route belongs to.
        parts = [p for p in path_template.split("/") if p and not p.startswith("{")]
        if len(parts) >= 3 and parts[0] == "api":
            return parts[2]
        return parts[-1] if parts else None

    @staticmethod
    def _extract_entity_id(path_params: dict, body_chunks: list[bytes]) -> str | None:
        if path_params:
            return str(next(iter(path_params.values())))
        body = b"".join(body_chunks)
        if not body:
            return None
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
        return None
