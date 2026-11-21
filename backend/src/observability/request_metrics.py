"""Times every HTTP request into `tradingos_http_request_duration_seconds`.

Pure ASGI, like the other middlewares here. The route label is the matched route's template
(`/api/v1/strategies/{strategy_id}`), read after the app has run, when the router has put the
route on the scope; a request that matched no route is labelled "unmatched", so a stream of bad
URLs cannot create unbounded series. `/metrics` itself (Prometheus's own scrape) and WebSocket
connections are not timed.
"""

import time
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from src.observability.metrics import http_request_duration_seconds
from src.observability.route_template import route_template

UNMATCHED_ROUTE = "unmatched"
_NOT_TIMED_PATHS = frozenset({"/metrics"})


class RequestMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _NOT_TIMED_PATHS:
            await self.app(scope, receive, send)
            return

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            status = status_holder.get("status", 500)
            http_request_duration_seconds.labels(
                method=scope["method"],
                route=route_template(scope) or UNMATCHED_ROUTE,
                status_class=f"{status // 100}xx",
            ).observe(elapsed)
