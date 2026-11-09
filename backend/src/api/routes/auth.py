"""Sign-in, sessions and registration.

A session is a short-lived **access token** (in the response body; the console keeps it in memory
only) and a long-lived **refresh token** (an HttpOnly cookie, never in a body: see
src.core.web_session). Each refresh swaps the cookie for a new token and retires the old one;
presenting a retired token again means a copy of it leaked, so the whole family is revoked and an
audit entry says so. The one exception is a reply that never reached the browser: if the token was
retired only seconds ago and the token that replaced it has not been used, the browser most likely
never got it (a reload cancelled the request after the server had answered), so the retired token is
given a fresh successor instead of ending the session (`_is_a_lost_reply_retried`), and that is
audited too.

Registration is for the very first account only (it becomes the administrator). After that accounts
are made by an administrator (`POST /admin/users`), so the console of a system reachable from a
network is not an open door to a read-only view of the audit trail.

Wrong passwords are counted against the account (src.core.login_lockout), and against the address
they came from; an unknown email costs the same time as a known one (src.core.security).
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from src.api.deps import get_current_user
from src.api.schemas import (
    AccessTokenResponse,
    LoginRequest,
    RegistrationStatusResponse,
    SignInPolicyResponse,
    UserRegisterRequest,
    UserResponse,
    WsTicketResponse,
)
from src.audit.service import write_audit_entry
from src.core.client_ip import scope_client_ip
from src.core.config import get_settings
from src.core.db import get_db
from src.core.login_lockout import (
    LockoutPolicy,
    SignInState,
    after_failure,
    after_success,
    is_locked,
    seconds_until_unlocked,
)
from src.core.rate_limit import fixed_window_increment
from src.core.rbac import register_policy, require_role
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.core.security import (
    InvalidTokenError,
    TokenType,
    burn_password_check_time,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password_async,
    verify_password_async,
)
from src.core.web_session import (
    REFRESH_COOKIE_NAME,
    clear_refresh_cookie,
    origin_allowed,
    set_refresh_cookie,
)
from src.core.ws_ticket import issue_ws_ticket
from src.models.refresh_token import RefreshToken
from src.models.user import User
from src.observability.audit_middleware import skip_audit_entry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

register_policy("POST", "/api/v1/auth/ws-ticket", roles=list(Role))

_invalid_credentials = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
)

REGISTRATION_CLOSED = (
    "Registration is closed. An administrator creates accounts: ask one to add you "
    "(Settings > Users)."
)
REGISTRATION_OPEN = (
    "This system has no accounts yet. The first account you create becomes its administrator."
)

# Serialises "is the table empty, then insert the first user": two requests arriving together on a
# fresh system must not both be told it is empty and both become administrator.
_FIRST_USER_LOCK = "SELECT pg_advisory_xact_lock(hashtext('tradingos.auth.first-user'))"

_WS_TICKETS_PER_MINUTE = 120


def _lockout_policy() -> LockoutPolicy:
    settings = get_settings()
    return LockoutPolicy(
        max_failed_attempts=settings.login_max_failed_attempts,
        failure_window=timedelta(minutes=settings.login_failure_window_minutes),
        lockout_duration=timedelta(minutes=settings.login_lockout_minutes),
    )


def _state_of(user: User) -> SignInState:
    return SignInState(user.failed_login_attempts, user.last_failed_login_at, user.locked_until)


def _store_state(user: User, state: SignInState) -> None:
    user.failed_login_attempts = state.failed_attempts
    user.last_failed_login_at = state.last_failed_at
    user.locked_until = state.locked_until


def _locked_response(seconds: int) -> HTTPException:
    minutes = max(1, -(-seconds // 60))
    return HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=(
            "This account is locked after too many wrong passwords. "
            f"Try again in {minutes} minute{'s' if minutes != 1 else ''}, or ask an administrator "
            "to unlock it."
        ),
        headers={"Retry-After": str(seconds)},
    )


async def _enforce_sign_in_rate_limit(request: Request, redis: Redis) -> None:
    """The per-address ceiling on sign-in attempts. Separate from the account lock: that stops many
    guesses at one account, this stops one address trying many accounts."""
    state_limit = getattr(request.app.state, "login_rate_limit_per_minute", None)
    limit = state_limit if state_limit is not None else get_settings().login_rate_limit_per_minute
    ip = scope_client_ip(request.scope)
    try:
        allowed = await fixed_window_increment(
            redis, key=f"auth:login-rate:{ip}", limit=limit, window_seconds=60
        )
    except Exception:  # noqa: BLE001 - Redis being unreachable must not stop anyone signing in
        logger.warning("auth.login_rate_limit_redis_unavailable", ip=ip)
        return
    if not allowed:
        logger.warning("auth.login_rate_limited", ip=ip)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many sign-in attempts from this address. Wait a minute and try again.",
            headers={"Retry-After": "60"},
        )


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


@router.get("/registration-status", response_model=RegistrationStatusResponse)
async def registration_status(db: AsyncSession = Depends(get_db)) -> RegistrationStatusResponse:
    """Public: the sign-in page asks this to decide whether to offer "create an account"."""
    is_open = await _user_count(db) == 0
    return RegistrationStatusResponse(
        open=is_open, detail=REGISTRATION_OPEN if is_open else REGISTRATION_CLOSED
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    # Closed is answered the same whatever the email is: it must not say which addresses have
    # accounts.
    if await _user_count(db) > 0:
        raise HTTPException(status.HTTP_403_FORBIDDEN, REGISTRATION_CLOSED)

    hashed = await hash_password_async(payload.password)

    await db.execute(text(_FIRST_USER_LOCK))
    if await _user_count(db) > 0:  # someone else registered while this one was hashing
        raise HTTPException(status.HTTP_403_FORBIDDEN, REGISTRATION_CLOSED)

    user = User(email=payload.email, hashed_password=hashed, role=Role.SYSTEM_ADMINISTRATOR)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _start_session(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    family_id: uuid.UUID | None = None,
) -> AccessTokenResponse:
    access = await create_access_token(db, user.id, role=user.role.value)
    refresh_token = await create_refresh_token(db, user.id, family_id=family_id)

    db.add(
        RefreshToken(
            id=refresh_token.jti,
            user_id=user.id,
            family_id=refresh_token.family_id,
            issued_at=datetime.now(UTC),
            expires_at=refresh_token.expires_at,
        )
    )
    await db.commit()

    set_refresh_cookie(response, request.scope, refresh_token.token)
    response.headers["Cache-Control"] = "no-store"
    return AccessTokenResponse(
        access_token=access.token,
        expires_in=int((access.expires_at - datetime.now(UTC)).total_seconds()),
    )


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AccessTokenResponse:
    await _enforce_sign_in_rate_limit(request, redis)

    # Locked for update: two wrong passwords arriving together must both be counted.
    user = (
        await db.execute(select(User).where(User.email == payload.email).with_for_update())
    ).scalar_one_or_none()

    if user is None:
        await burn_password_check_time(payload.password)
        raise _invalid_credentials

    now = datetime.now(UTC)
    state = _state_of(user)
    if is_locked(state, now):
        # Not even checked: a correct password during a lock must not reveal itself.
        raise _locked_response(seconds_until_unlocked(state, now))

    if not await verify_password_async(payload.password, user.hashed_password):
        new_state = after_failure(state, now, _lockout_policy())
        _store_state(user, new_state)
        just_locked = new_state.locked_until is not None
        if just_locked:
            await write_audit_entry(
                db,
                actor=user.email,
                action="auth.account_locked",
                entity_type="user",
                entity_id=str(user.id),
                details={
                    "locked_until": new_state.locked_until.isoformat()
                    if new_state.locked_until
                    else None,
                    "client_ip": scope_client_ip(request.scope),
                },
            )
            logger.warning("auth.account_locked", user_id=str(user.id))
        await db.commit()
        if just_locked:
            raise _locked_response(seconds_until_unlocked(new_state, now))
        raise _invalid_credentials

    if not user.is_active:
        raise _invalid_credentials

    if state != SignInState():
        _store_state(user, after_success(state))
    return await _start_session(request, response, db, user)


def _refresh_rejected(request: Request) -> JSONResponse:
    """A refresh the server looked at and refused: say so and drop the dead cookie, so the browser
    stops presenting it."""
    rejected = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Invalid or expired refresh token"},
    )
    clear_refresh_cookie(rejected, request.scope)
    return rejected


def _check_origin(request: Request) -> None:
    if not origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This request did not come from the console")


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> AccessTokenResponse | JSONResponse:
    _check_origin(request)
    presented = request.cookies.get(REFRESH_COOKIE_NAME)
    if not presented:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session to refresh")

    try:
        claims = await decode_token(db, presented, expected_type=TokenType.REFRESH)
        token_id = uuid.UUID(claims["jti"])
        family_id = uuid.UUID(claims["family_id"])
        user_id = uuid.UUID(claims["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        return _refresh_rejected(request)

    # Locked for update: two copies of one token arriving together are looked at one after the
    # other, the second seeing what the first did.
    stored = (
        await db.execute(select(RefreshToken).where(RefreshToken.id == token_id).with_for_update())
    ).scalar_one_or_none()
    if stored is None or stored.family_id != family_id or stored.user_id != user_id:
        return _refresh_rejected(request)

    now = datetime.now(UTC)

    if stored.revoked_at is not None and stored.used_at is None:
        # Revoked without ever being redeemed: signed out, ended by an administrator, or swept up
        # when a reuse was detected. Nothing leaked here; it is simply no longer a session.
        return _refresh_rejected(request)

    retried = False
    if stored.revoked_at is not None:
        if await _is_a_lost_reply_retried(db, stored, now):
            retried = True
        else:
            # This exact single-use token was already redeemed once before -- presenting it again
            # means either the token leaked or a client raced itself. Treat as compromise: revoke
            # every still-live token in the family so a stolen token can't keep rotating alongside
            # the legitimate client, and leave a record that it happened.
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await write_audit_entry(
                db,
                actor=str(user_id),
                action="auth.refresh_token_reuse_detected",
                entity_type="user",
                entity_id=str(user_id),
                details={"family_id": str(family_id), "client_ip": scope_client_ip(request.scope)},
            )
            await db.commit()
            logger.warning("auth.refresh_token_reuse_detected", user_id=str(user_id))
            return _refresh_rejected(request)

    if stored.expires_at < now:
        if stored.revoked_at is None:
            stored.revoked_at = now
            await db.commit()
        return _refresh_rejected(request)

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        return _refresh_rejected(request)

    if retried:
        assert stored.used_at is not None  # a retry is only ever of a token that was redeemed
        await write_audit_entry(
            db,
            actor=str(user_id),
            action="auth.refresh_token_retried",
            entity_type="user",
            entity_id=str(user_id),
            details={
                "family_id": str(family_id),
                "seconds_since_rotation": round((now - stored.used_at).total_seconds(), 3),
                "client_ip": scope_client_ip(request.scope),
            },
        )
        logger.info("auth.refresh_token_retried", user_id=str(user_id))
        skip_audit_entry(request, because="the retry is recorded by its own entry above")
    else:
        # Not committed here: `_start_session` stores the replacement and commits both together.
        # Committing now would release the row lock while no replacement exists yet, and a second
        # copy waiting on that lock would find none, take itself for a stolen token and end the
        # session (and the family revoke would also miss the replacement stored just after it).
        stored.used_at = now
        stored.revoked_at = now
        skip_audit_entry(
            request, because="a session's token rotating; the sign-in is what is recorded"
        )
    return await _start_session(request, response, db, user, family_id=family_id)


async def _is_a_lost_reply_retried(db: AsyncSession, retired: RefreshToken, now: datetime) -> bool:
    """True when a retired token coming back is most likely the same browser whose reply was lost,
    not a copy that leaked: it was redeemed no more than `refresh_token_reuse_grace_seconds` ago,
    and the token that replaced it is still live and has never been presented. (A reload cancels a
    refresh the server has already answered: the new token is never stored, and the page presents
    the old one again.) Once the replacement has been used, or revoked by a sign-out or an
    administrator, the old token coming back is a replay as ever."""
    grace = timedelta(seconds=get_settings().refresh_token_reuse_grace_seconds)
    if retired.used_at is None or not timedelta(0) <= now - retired.used_at <= grace:
        return False
    successor = (
        await db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.family_id == retired.family_id,
                RefreshToken.id != retired.id,
                RefreshToken.issued_at >= retired.used_at,
            )
            .order_by(RefreshToken.issued_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return successor is not None and successor.used_at is None and successor.revoked_at is None


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    _check_origin(request)
    ended = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(ended, request.scope)

    presented = request.cookies.get(REFRESH_COOKIE_NAME)
    if not presented:
        return ended
    try:
        claims = await decode_token(db, presented, expected_type=TokenType.REFRESH)
        token_id = uuid.UUID(claims["jti"])
    except (InvalidTokenError, KeyError, ValueError):
        return ended

    stored = (
        await db.execute(select(RefreshToken).where(RefreshToken.id == token_id))
    ).scalar_one_or_none()
    if stored is not None:
        # The family, not just this token: a retried refresh leaves two live tokens in it, and
        # signing out must end both.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await db.commit()
    return ended


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("/sign-in-policy", response_model=SignInPolicyResponse)
async def sign_in_policy(_current_user: User = Depends(get_current_user)) -> SignInPolicyResponse:
    """The numbers behind the sign-in lock and the session lengths, for Settings > Security."""
    settings = get_settings()
    return SignInPolicyResponse(
        max_failed_attempts=settings.login_max_failed_attempts,
        failure_window_minutes=settings.login_failure_window_minutes,
        lockout_minutes=settings.login_lockout_minutes,
        access_token_minutes=settings.access_token_expire_minutes,
        session_days=settings.refresh_token_expire_days,
    )


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def ws_ticket(
    request: Request,
    current_user: User = Depends(require_role),
    redis: Redis = Depends(get_redis),
) -> WsTicketResponse:
    """A one-time ticket to open a WebSocket with (src.core.ws_ticket), bought with the access token
    so the token itself never travels in a URL."""
    if not await fixed_window_increment(
        redis,
        key=f"auth:ws-ticket-rate:{current_user.id}",
        limit=_WS_TICKETS_PER_MINUTE,
        window_seconds=60,
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many connections opened; wait a minute.",
            headers={"Retry-After": "60"},
        )
    skip_audit_entry(request, because="a one-time connection ticket, not an action")
    ttl = get_settings().ws_ticket_ttl_seconds
    return WsTicketResponse(
        ticket=await issue_ws_ticket(redis, current_user.id, ttl), expires_in=ttl
    )
