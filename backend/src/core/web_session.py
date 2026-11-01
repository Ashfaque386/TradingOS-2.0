"""The browser side of a session: the refresh-token cookie, and the check that a request that
carries it came from a page we served.

The refresh token is a credential that outlives the access token by days, so it is kept where script
cannot read it: an HttpOnly cookie, scoped to `/api/v1/auth` so it is not sent with every other
request, and `SameSite=Strict` by default so another site cannot make the browser send it. A page
that is XSS'd can still ask the API for a fresh access token while it is open (it can do whatever
the user can), but it can no longer carry the long-lived credential away.

A cookie is sent by the browser on its own, which is exactly what makes cross-site request forgery
possible, so the two endpoints that act on it (`/auth/refresh`, `/auth/logout`) also check the
`Origin` header the browser attaches to a cross-origin request. No `Origin` at all means the request
did not come from a browser page (curl, a script), which has no ambient cookie to abuse.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import Response

from src.core.client_ip import scope_came_over_https
from src.core.config import get_settings

REFRESH_COOKIE_NAME = "tradingos_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _secure(scope: Mapping[str, Any]) -> bool:
    settings = get_settings()
    # SameSite=None is only accepted by browsers together with Secure, so it forces it.
    if settings.refresh_cookie_samesite == "none" or settings.refresh_cookie_secure == "true":
        return True
    if settings.refresh_cookie_secure == "false":
        return False
    return scope_came_over_https(scope)


def set_refresh_cookie(response: Response, scope: Mapping[str, Any], value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        value,
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=_secure(scope),
        samesite=settings.refresh_cookie_samesite,
    )


def clear_refresh_cookie(response: Response, scope: Mapping[str, Any]) -> None:
    settings = get_settings()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=_secure(scope),
        samesite=settings.refresh_cookie_samesite,
    )


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Whether a browser request from `origin` may use the session cookie.

    Allowed: no Origin header (not a browser page), an origin named in CORS_ORIGINS, or an origin
    that is the very host the request was addressed to (the console and the API served from one
    address).
    """
    if not origin:
        return True
    if origin in get_settings().cors_origins_list:
        return True
    return bool(host) and urlsplit(origin).netloc == host
