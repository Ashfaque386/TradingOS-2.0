"""Response headers that tell a browser how much to trust what it was just sent.

The API only ever answers with JSON (or CSV/NDJSON/Markdown downloads), never a page, so the policy
can be the strictest there is: nothing may be loaded or framed from it. The console's own pages get
their policy from the frontend server (next.config.mjs, and nginx in the all-in-one image); this
covers the API's responses, including the ones a route did not build itself (a 404, a 429 from the
rate limiter).

A header a route already set is left alone, so a handler that needs something different (a file
download that sets its own Cache-Control) is not overridden. Pure ASGI, like the other middleware.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.core.config import get_settings

# Swagger UI and ReDoc load their own script and styles, so they get no CSP of ours (they exist only
# when API_DOCS_ENABLED is set).
_DOCS_PATHS = ("/docs", "/redoc")

_ALWAYS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        hsts = get_settings().hsts_enabled

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                wanted = dict(_ALWAYS)
                if not path.startswith(_DOCS_PATHS):
                    wanted["Content-Security-Policy"] = _API_CSP
                if path.startswith("/api/v1/auth/"):
                    # A sign-in or refresh response carries a credential: nothing may keep a copy.
                    wanted["Cache-Control"] = "no-store"
                if hsts:
                    wanted["Strict-Transport-Security"] = _HSTS
                for name, value in wanted.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
