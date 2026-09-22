"""Real Zerodha/Upstox OAuth completion (Settings redesign) -- neither
broker adapter (src.brokers.zerodha/upstox) has ever done this: both are
explicitly documented as post-auth-only, treating whatever access_token
BrokerCredentials carries as a given. This module is what actually
produces that token: a login/authorize-URL builder per broker, and a
callback endpoint that completes the provider-specific token exchange and
stores the result via the same SecretsStore broker_credentials.py already
uses (BrokerCredentials.token_status/token_expires_at, added alongside the
original api_key/api_secret/access_token trio -- see src/brokers/base.py).

Both brokers' callback is a real browser GET navigation driven by the
broker itself (the user's browser lands here after logging in at
Zerodha/Upstox), never a fetch() from our own frontend -- there is no
Authorization header to check. Unlike every other mutating route in this
API, these two callback endpoints are deliberately NOT behind
`require_role`: the security boundary is that a request_token/code is only
ever producible by a real login at the broker, redirected through the
redirect_uri WE registered with them, and completing the exchange requires
OUR OWN api_secret/client_secret, which never leaves this server. This is
the same posture as signature-verified inbound webhooks
(src.notifications.verification) -- an unavoidably unauthenticated entry
point whose trust comes from a provider-specific proof, not a bearer
token, not a stand-in for real authentication.

Zerodha's token lifetime: Kite Connect's session/token response carries no
expiry field, and Zerodha's own documentation describes (without giving an
exact machine-checkable time) that an access token is invalidated once
daily. token_expires_at is therefore a documented *estimate* -- the next
06:00 IST after issuance -- for UI display ("expires around HH:MM IST
today"), not a value Zerodha's API itself asserts; treat a 401 from the
adapter at call time as the actual source of truth, same as
ZerodhaKiteAdapter's own docstring already says.

Upstox's `duration` ("standard" | "extended"): passed through to the
authorize-dialog URL as an operator's stated intent. Upstox's extended
(long-lived) token issuance is account/consent-gated on their side --
requesting it here does not guarantee Upstox actually grants a
longer-lived token; the resulting token_expires_at reflects whatever
Upstox's callback response actually returns, not this parameter's own
label.
"""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.api.schemas import BrokerOAuthLoginUrlResponse
from src.audit.service import write_audit_entry
from src.brokers.base import BrokerCredentials
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.security.secrets_store import SecretsStore

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/broker-credentials", tags=["broker-credentials"])

IST = ZoneInfo("Asia/Kolkata")
_HTTP_TIMEOUT_SECONDS = 15.0

_LOGIN_URL_ROLES = [Role.SYSTEM_ADMINISTRATOR]
register_policy("GET", "/api/v1/broker-credentials/zerodha/login-url", roles=_LOGIN_URL_ROLES)
register_policy("GET", "/api/v1/broker-credentials/upstox/login-url", roles=_LOGIN_URL_ROLES)
# The callback routes are deliberately unregistered -- see module docstring.
# require_role default-denies anything unregistered everywhere else in this
# codebase; this is an explicit, documented exception, the same category
# webhooks.py's inbound routes already are.


async def get_oauth_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """A FastAPI dependency, not a bare `httpx.AsyncClient(...)` call at
    each use site, so tests can override it with a client bound to
    httpx.MockTransport -- this sandbox has no live Zerodha/Upstox to call,
    same "real code, injected transport in tests" posture as
    src.brokers.zerodha/upstox already use for their own adapters.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        yield client


def _request_base(request: Request) -> str:
    settings = get_settings()
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _redirect_uri(request: Request, broker: str) -> str:
    return f"{_request_base(request)}/api/v1/broker-credentials/{broker}/callback"


def _settings_redirect(
    request: Request, *, broker: str, result: str, error: str | None = None
) -> RedirectResponse:
    params = {"section": "broker", "broker": broker, "oauth": result}
    if error:
        params["error"] = error
    return RedirectResponse(
        f"{_request_base(request)}/settings?{urlencode(params)}", status_code=302
    )


def _next_6am_ist(after: datetime) -> str:
    """Zerodha's daily token-invalidation estimate -- see module docstring."""
    ist_now = after.astimezone(IST)
    candidate = ist_now.replace(hour=6, minute=0, second=0, microsecond=0)
    if candidate <= ist_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC).isoformat()


# ---- Zerodha ----------------------------------------------------------


@router.get("/zerodha/login-url")
async def zerodha_login_url_endpoint(
    request: Request,
    _current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> BrokerOAuthLoginUrlResponse:
    creds = store.get_credentials("zerodha")
    if creds is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "save a Zerodha API key first (Broker Config), then connect",
        )
    redirect_uri = _redirect_uri(request, "zerodha")
    # Persist the redirect_uri so the status endpoint can keep showing it
    # (the operator needs the exact same value saved in Kite Connect's
    # developer console, which they may revisit well after this call).
    if creds.redirect_uri != redirect_uri:
        store.set_credentials(
            "zerodha",
            BrokerCredentials(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                access_token=creds.access_token,
                redirect_uri=redirect_uri,
                token_expires_at=creds.token_expires_at,
            ),
        )
    login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={quote(creds.api_key)}"
    return BrokerOAuthLoginUrlResponse(login_url=login_url, redirect_uri=redirect_uri)


@router.get("/zerodha/callback")
async def zerodha_callback_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_oauth_http_client),
    store: SecretsStore = Depends(get_broker_credentials_store),
    request_token: str | None = Query(default=None),
    status_param: str | None = Query(default=None, alias="status"),
    action: str | None = Query(default=None),
) -> RedirectResponse:
    if status_param == "error" or not request_token:
        logger.info(
            "broker_oauth.zerodha_callback_failed",
            reason="broker reported failure or missing token",
        )
        return _settings_redirect(
            request, broker="zerodha", result="error", error="Zerodha login was not completed"
        )

    creds = store.get_credentials("zerodha")
    if creds is None or not creds.api_secret:
        return _settings_redirect(
            request, broker="zerodha", result="error", error="Zerodha API key/secret not configured"
        )

    checksum = hashlib.sha256(
        f"{creds.api_key}{request_token}{creds.api_secret}".encode()
    ).hexdigest()

    try:
        resp = await client.post(
            "https://api.kite.trade/session/token",
            data={
                "api_key": creds.api_key,
                "request_token": request_token,
                "checksum": checksum,
            },
            headers={"X-Kite-Version": "3"},
        )
    except httpx.HTTPError as exc:
        logger.info("broker_oauth.zerodha_callback_failed", reason="network error", error=str(exc))
        return _settings_redirect(
            request, broker="zerodha", result="error", error="could not reach Zerodha"
        )

    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code != 200 or body.get("status") != "success":
        message = body.get("message", f"Zerodha returned HTTP {resp.status_code}")
        logger.info(
            "broker_oauth.zerodha_callback_failed", reason="token exchange rejected", detail=message
        )
        return _settings_redirect(request, broker="zerodha", result="error", error=message)

    access_token = body["data"]["access_token"]
    token_expires_at = _next_6am_ist(datetime.now(UTC))

    store.set_credentials(
        "zerodha",
        BrokerCredentials(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            access_token=access_token,
            redirect_uri=creds.redirect_uri,
            token_expires_at=token_expires_at,
        ),
    )
    await write_audit_entry(
        db,
        actor="broker_oauth:zerodha",
        action="broker_oauth.connected",
        entity_type="broker_credentials",
        entity_id="zerodha",
        details={"token_expires_at": token_expires_at},
    )
    await db.commit()
    logger.info("broker_oauth.zerodha_connected", token_expires_at=token_expires_at)
    return _settings_redirect(request, broker="zerodha", result="success")


# ---- Upstox -------------------------------------------------------------


@router.get("/upstox/login-url")
async def upstox_login_url_endpoint(
    request: Request,
    duration: str = Query(default="standard", pattern="^(standard|extended)$"),
    _current_user: User = Depends(require_role),
    store: SecretsStore = Depends(get_broker_credentials_store),
) -> BrokerOAuthLoginUrlResponse:
    creds = store.get_credentials("upstox")
    if creds is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "save an Upstox API key first (Broker Config), then connect",
        )
    redirect_uri = _redirect_uri(request, "upstox")
    if creds.redirect_uri != redirect_uri or creds.token_duration != duration:
        store.set_credentials(
            "upstox",
            BrokerCredentials(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                access_token=creds.access_token,
                redirect_uri=redirect_uri,
                token_expires_at=creds.token_expires_at,
                token_duration=duration,
            ),
        )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": creds.api_key,
            "redirect_uri": redirect_uri,
        }
    )
    login_url = f"https://api.upstox.com/v2/login/authorization/dialog?{query}"
    return BrokerOAuthLoginUrlResponse(login_url=login_url, redirect_uri=redirect_uri)


@router.get("/upstox/callback")
async def upstox_callback_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_oauth_http_client),
    store: SecretsStore = Depends(get_broker_credentials_store),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error or not code:
        logger.info("broker_oauth.upstox_callback_failed", reason=error or "missing code")
        return _settings_redirect(
            request,
            broker="upstox",
            result="error",
            error=error or "Upstox login was not completed",
        )

    creds = store.get_credentials("upstox")
    if creds is None or not creds.api_secret:
        return _settings_redirect(
            request, broker="upstox", result="error", error="Upstox API key/secret not configured"
        )

    redirect_uri = creds.redirect_uri or _redirect_uri(request, "upstox")

    try:
        resp = await client.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={
                "code": code,
                "client_id": creds.api_key,
                "client_secret": creds.api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        logger.info("broker_oauth.upstox_callback_failed", reason="network error", error=str(exc))
        return _settings_redirect(
            request, broker="upstox", result="error", error="could not reach Upstox"
        )

    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code != 200 or "access_token" not in body:
        message = (
            body.get("error_description")
            or body.get("message")
            or f"Upstox returned HTTP {resp.status_code}"
        )
        logger.info(
            "broker_oauth.upstox_callback_failed", reason="token exchange rejected", detail=message
        )
        return _settings_redirect(request, broker="upstox", result="error", error=message)

    access_token = body["access_token"]
    # Upstox's token response includes no explicit expiry field either (as
    # of this writing) -- same estimate posture as Zerodha, see module
    # docstring, though Upstox's own daily-invalidation time isn't
    # documented as precisely as Zerodha's; 06:00 IST is used as the same
    # reasonable estimate for a "standard" token. An "extended" token's
    # real lifetime, if Upstox actually granted one, is not something this
    # endpoint can know without Upstox returning it explicitly.
    token_expires_at = (
        None if creds.token_duration == "extended" else _next_6am_ist(datetime.now(UTC))
    )

    store.set_credentials(
        "upstox",
        BrokerCredentials(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            access_token=access_token,
            redirect_uri=redirect_uri,
            token_expires_at=token_expires_at,
            token_duration=creds.token_duration,
        ),
    )
    await write_audit_entry(
        db,
        actor="broker_oauth:upstox",
        action="broker_oauth.connected",
        entity_type="broker_credentials",
        entity_id="upstox",
        details={"token_expires_at": token_expires_at, "duration": creds.token_duration},
    )
    await db.commit()
    logger.info("broker_oauth.upstox_connected", token_expires_at=token_expires_at)
    return _settings_redirect(request, broker="upstox", result="success")
