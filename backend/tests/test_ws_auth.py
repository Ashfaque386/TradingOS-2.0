"""Opening a WebSocket: the one-time ticket, and the handshake checks around it.

The access token used to travel in the URL (`?token=`). It must not any more: the page buys a ticket
(POST /auth/ws-ticket), and the ticket works once, for seconds, from the console's own origin.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.api.routes import auth as auth_routes
from src.api.ws_auth import authenticate_websocket
from src.core.config import get_settings
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.core.ws_ticket import issue_ws_ticket, redeem_ws_ticket
from src.main import app
from src.models.audit_log import AuditLog
from src.models.user import User


class FakeWebSocket:
    """Just what `authenticate_websocket` reads and does: headers, the query string, close."""

    def __init__(
        self, *, query: dict[str, str] | None = None, origin: str | None = None, host="test"
    ):
        self.query_params = query or {}
        self.headers = {"host": host}
        if origin is not None:
            self.headers["origin"] = origin
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [k async for k in redis_client.scan_iter("auth:ws-ticket*")]
    if keys:
        await redis_client.delete(*keys)
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield redis_client
    app.dependency_overrides.pop(get_redis, None)
    keys = [k async for k in redis_client.scan_iter("auth:ws-ticket*")]
    if keys:
        await redis_client.delete(*keys)


async def _authenticate(db_session_factory, redis, websocket: FakeWebSocket):
    async with db_session_factory() as db:
        return await authenticate_websocket(websocket, db, redis)  # type: ignore[arg-type]


async def _user(make_user, email="viewer@example.com") -> User:
    return await make_user(email, "supersecret1", Role.READ_ONLY_AUDITOR)


async def _token(client, email="viewer@example.com") -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return resp.json()["access_token"]


# ---- buying a ticket ----


async def test_a_ticket_needs_a_signed_in_user(client, redis):
    resp = await client.post("/api/v1/auth/ws-ticket")

    assert resp.status_code in (401, 403)


async def test_a_signed_in_user_of_any_role_gets_a_short_lived_ticket(client, redis, make_user):
    for role in Role:
        email = f"{role.value.lower()}@example.com"
        await make_user(email, "supersecret1", role)
        token = await _token(client, email)

        resp = await client.post(
            "/api/v1/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"}
        )

        assert resp.status_code == 200, role
        assert resp.json()["expires_in"] == 30
        assert len(resp.json()["ticket"]) >= 40


async def test_the_ticket_is_stored_hashed_and_expires(client, redis, make_user):
    await _user(make_user)
    token = await _token(client)

    ticket = (
        await client.post("/api/v1/auth/ws-ticket", headers={"Authorization": f"Bearer {token}"})
    ).json()["ticket"]

    keys = [k async for k in redis.scan_iter("auth:ws-ticket:*")]
    assert len(keys) == 1
    assert ticket not in keys[0]  # a copy of Redis does not hold a usable ticket
    assert 0 < await redis.ttl(keys[0]) <= 30


async def test_every_ticket_is_different(client, redis, make_user):
    await _user(make_user)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    tickets = {
        (await client.post("/api/v1/auth/ws-ticket", headers=headers)).json()["ticket"]
        for _ in range(5)
    }

    assert len(tickets) == 5


async def test_buying_tickets_is_limited_per_user(client, redis, make_user, monkeypatch):
    monkeypatch.setattr(auth_routes, "_WS_TICKETS_PER_MINUTE", 2)
    monkeypatch.setattr(
        "src.core.rate_limit.time", type("T", (), {"time": staticmethod(lambda: 1.9e9)})
    )
    await _user(make_user)
    headers = {"Authorization": f"Bearer {await _token(client)}"}

    statuses = [
        (await client.post("/api/v1/auth/ws-ticket", headers=headers)).status_code for _ in range(3)
    ]

    assert statuses == [200, 200, 429]


async def test_a_ticket_leaves_nothing_in_the_audit_trail(
    client, redis, make_user, db_session_factory
):
    await _user(make_user)
    headers = {"Authorization": f"Bearer {await _token(client)}"}
    for _ in range(3):
        await client.post("/api/v1/auth/ws-ticket", headers=headers)

    async with db_session_factory() as db:
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert not any("ws-ticket" in action for action in actions)


# ---- redeeming it ----


async def test_a_ticket_works_once(redis):
    user_id = uuid.uuid4()
    ticket = await issue_ws_ticket(redis, user_id, 30)

    assert await redeem_ws_ticket(redis, ticket) == user_id
    assert await redeem_ws_ticket(redis, ticket) is None


async def test_an_unknown_ticket_is_nobody(redis):
    assert await redeem_ws_ticket(redis, "never-issued") is None


async def test_an_expired_ticket_is_nobody(redis):
    ticket = await issue_ws_ticket(redis, uuid.uuid4(), 30)
    key = [k async for k in redis.scan_iter("auth:ws-ticket:*")][0]
    await redis.pexpire(key, 1)
    await asyncio.sleep(0.05)

    assert await redeem_ws_ticket(redis, ticket) is None


async def test_something_that_is_not_a_user_id_stored_under_a_ticket_is_nobody(redis):
    from src.core.ws_ticket import _key

    await redis.set(_key("odd"), "this-is-not-a-uuid", ex=30)

    assert await redeem_ws_ticket(redis, "odd") is None


async def test_a_redis_client_that_returns_bytes_works_too():
    from redis.asyncio import Redis

    raw = Redis.from_url(get_settings().redis_url)  # no decode_responses: GETDEL gives bytes
    try:
        user_id = uuid.uuid4()
        ticket = await issue_ws_ticket(raw, user_id, 30)

        assert await redeem_ws_ticket(raw, ticket) == user_id
    finally:
        await raw.aclose()


async def test_the_lifetime_is_a_setting(client, redis, make_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "ws_ticket_ttl_seconds", 7)
    await _user(make_user)
    headers = {"Authorization": f"Bearer {await _token(client)}"}

    body = (await client.post("/api/v1/auth/ws-ticket", headers=headers)).json()

    assert body["expires_in"] == 7
    key = [k async for k in redis.scan_iter("auth:ws-ticket:*")][0]
    assert 0 < await redis.ttl(key) <= 7


# ---- the handshake ----


async def test_a_valid_ticket_opens_the_socket_for_its_user(db_session_factory, redis, make_user):
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    websocket = FakeWebSocket(query={"ticket": ticket})

    result = await _authenticate(db_session_factory, redis, websocket)

    assert result is not None and result.id == user.id
    assert websocket.closed is None


async def test_the_handshake_does_not_leave_a_connection_held_for_the_life_of_the_socket(
    db_session_factory, redis, make_user
):
    """The route's session lives as long as the socket; one with its transaction left open held a
    pooled connection idle in a transaction until the socket closed."""
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    websocket = FakeWebSocket(query={"ticket": ticket})

    async with db_session_factory() as db:
        result = await authenticate_websocket(websocket, db, redis)  # type: ignore[arg-type]

        assert result is not None and result.email == "viewer@example.com"  # readable: detached
        assert not db.in_transaction()


async def test_a_refused_handshake_leaves_no_transaction_open_either(
    db_session_factory, redis, make_user
):
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    async with db_session_factory() as db:
        row = await db.get(User, user.id)
        row.is_active = False
        await db.commit()

    async with db_session_factory() as db:
        result = await authenticate_websocket(  # type: ignore[arg-type]
            FakeWebSocket(query={"ticket": ticket}), db, redis
        )

        assert result is None
        assert not db.in_transaction()


async def test_the_same_ticket_does_not_open_a_second_socket(db_session_factory, redis, make_user):
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    await _authenticate(db_session_factory, redis, FakeWebSocket(query={"ticket": ticket}))

    second = FakeWebSocket(query={"ticket": ticket})
    result = await _authenticate(db_session_factory, redis, second)

    assert result is None
    assert second.closed == (1008, "invalid ticket")


async def test_no_ticket_is_refused(db_session_factory, redis):
    websocket = FakeWebSocket()

    assert await _authenticate(db_session_factory, redis, websocket) is None
    assert websocket.closed == (1008, "missing ticket")


async def test_the_access_token_in_the_url_no_longer_opens_anything(
    client, db_session_factory, redis, make_user
):
    await _user(make_user)
    websocket = FakeWebSocket(query={"token": await _token(client)})

    assert await _authenticate(db_session_factory, redis, websocket) is None
    assert websocket.closed == (1008, "missing ticket")


async def test_a_made_up_ticket_is_refused(db_session_factory, redis):
    websocket = FakeWebSocket(query={"ticket": "made-up"})

    assert await _authenticate(db_session_factory, redis, websocket) is None
    assert websocket.closed == (1008, "invalid ticket")


async def test_a_deactivated_account_is_refused_even_with_a_ticket(
    db_session_factory, redis, make_user
):
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    async with db_session_factory() as db:
        row = await db.get(User, user.id)
        row.is_active = False
        await db.commit()
    websocket = FakeWebSocket(query={"ticket": ticket})

    assert await _authenticate(db_session_factory, redis, websocket) is None
    assert websocket.closed == (1008, "invalid user")


async def test_a_ticket_for_a_user_who_no_longer_exists_is_refused(db_session_factory, redis):
    ticket = await issue_ws_ticket(redis, uuid.uuid4(), 30)
    websocket = FakeWebSocket(query={"ticket": ticket})

    assert await _authenticate(db_session_factory, redis, websocket) is None
    assert websocket.closed == (1008, "invalid user")


@pytest.mark.parametrize(
    "origin, host",
    [
        (None, "test"),  # not a browser page
        ("http://localhost:3000", "test"),  # the console (CORS_ORIGINS)
        ("http://test", "test"),  # the console served from the API's own address
    ],
)
async def test_the_consoles_own_origins_may_open_a_socket(
    db_session_factory, redis, make_user, origin, host
):
    user = await _user(make_user)
    websocket = FakeWebSocket(
        query={"ticket": await issue_ws_ticket(redis, user.id, 30)}, origin=origin, host=host
    )

    assert await _authenticate(db_session_factory, redis, websocket) is not None


async def test_a_page_on_another_site_cannot_open_a_socket_and_does_not_spend_the_ticket(
    db_session_factory, redis, make_user
):
    user = await _user(make_user)
    ticket = await issue_ws_ticket(redis, user.id, 30)
    hostile = FakeWebSocket(query={"ticket": ticket}, origin="https://evil.example")

    assert await _authenticate(db_session_factory, redis, hostile) is None
    assert hostile.closed == (1008, "origin not allowed")

    # The check comes before the ticket is touched, so the real console can still use it.
    honest = FakeWebSocket(query={"ticket": ticket}, origin="http://localhost:3000")
    assert await _authenticate(db_session_factory, redis, honest) is not None


async def test_every_websocket_route_hands_the_redis_client_to_the_authenticator():
    """A route that forgot would fail only when a browser connects to it."""
    import inspect

    from src.api.routes import websockets

    source = inspect.getsource(websockets)
    calls = source.count("authenticate_websocket(")
    assert calls == 6
    assert source.count("authenticate_websocket(websocket, db, redis)") == calls
    assert "?token" not in source
