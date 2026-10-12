"""JWT signing and verification (src/core/security.py), now on PyJWT.

python-jose was replaced in 2026-10 (CVE-2026-85394, an HS256 algorithm-confusion
flaw with no fix, and its `ecdsa` dependency). A library swap in the code that
decides who is logged in has to prove three things, and these tests pin them:

1. Nothing already issued breaks. Tokens minted by python-jose -- the exact
   payload shapes this module builds, signed with a known key -- must still
   verify, so no signed-in operator is logged out by the upgrade. (They are
   literal fixtures: python-jose is no longer a dependency to mint them here.)
2. What this module mints is plain, standard HS256 -- checked with an
   independent HMAC computation rather than by trusting the library that wrote
   it -- so any JWT implementation, including the old one, verifies it.
3. Everything that must be rejected still is, and only ever as
   `InvalidTokenError`: no library exception leaks out as a 500.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import uuid
from typing import Any

import jwt
import pytest

from src.core import security
from src.core.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    invalidate_signing_key_cache,
)
from src.models.jwt_signing_key import JwtSigningKey

KEY = "compat-fixture-key-0123456789-abcdefghijklmnopqrstuvwxyz"

# Minted with python-jose 3.5.0 (the previous implementation), payload shapes
# copied from create_access_token / create_refresh_token, iat 2026-01-01,
# exp 2100-01-01 so the fixtures never expire under the test suite.
OLD_LIBRARY_ACCESS_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2xlIjoiU3lzdGVtQWRtaW5pc3RyYXRvciIs"
    "InR5cGUiOiJhY2Nlc3MiLCJpYXQiOjE3NjcyMjU2MDAsImV4cCI6NDEwMjQ0NDgwMCwianRpIjoiMjIyMjIyMjItMjIyMi00MjIy"
    "LTgyMjItMjIyMjIyMjIyMjIyIn0."
    "W9GVWne3j_5G9Fi93D58A9yNGtJRVXNVnz9rML7rWUA"
)
OLD_LIBRARY_REFRESH_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJ0eXBlIjoicmVmcmVzaCIsImlhdCI6MTc2NzIy"
    "NTYwMCwiZXhwIjo0MTAyNDQ0ODAwLCJqdGkiOiIzMzMzMzMzMy0zMzMzLTQzMzMtODMzMy0zMzMzMzMzMzMzMzMiLCJmYW1pbHlf"
    "aWQiOiI0NDQ0NDQ0NC00NDQ0LTQ0NDQtODQ0NC00NDQ0NDQ0NDQ0NDQifQ."
    "Gy1ZcK7rsaUCfc2tUTMfOOF3qEwOrVpuwjuCpDa4XSQ"
)


async def _use_key(db_session_factory, key: str = KEY) -> None:
    """Make `key` the deployment's current signing key, as the DB row
    `get_current_signing_key` reads (and drop any key cached from before)."""
    async with db_session_factory() as db:
        db.add(JwtSigningKey(key=key, created_at=dt.datetime.now(dt.UTC), created_by="test"))
        await db.commit()
    invalidate_signing_key_cache()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _claims(**overrides: Any) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "role": "SystemAdministrator",
        "type": "access",
        "iat": now,
        "exp": now + dt.timedelta(minutes=15),
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return claims


def _hand_signed(header: dict[str, Any], payload: dict[str, Any], key: str | None = KEY) -> str:
    """A token assembled by hand (no JWT library), HS256-signed with `key`, or
    unsigned when `key` is None -- for shapes a library will not produce."""
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    if key is None:
        return signing_input + "."
    signature = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


# ---- 1. tokens issued before the swap keep working ---------------------------------


async def test_an_access_token_minted_by_the_previous_library_still_verifies(db_session_factory):
    await _use_key(db_session_factory)

    async with db_session_factory() as db:
        payload = await decode_token(db, OLD_LIBRARY_ACCESS_TOKEN, TokenType.ACCESS)

    assert payload == {
        "sub": "11111111-1111-4111-8111-111111111111",
        "role": "SystemAdministrator",
        "type": "access",
        "iat": 1767225600,
        "exp": 4102444800,
        "jti": "22222222-2222-4222-8222-222222222222",
    }


async def test_a_refresh_token_minted_by_the_previous_library_still_verifies(db_session_factory):
    await _use_key(db_session_factory)

    async with db_session_factory() as db:
        payload = await decode_token(db, OLD_LIBRARY_REFRESH_TOKEN, TokenType.REFRESH)

    assert payload["sub"] == "11111111-1111-4111-8111-111111111111"
    assert payload["family_id"] == "44444444-4444-4444-8444-444444444444"
    assert payload["jti"] == "33333333-3333-4333-8333-333333333333"


async def test_the_old_tokens_are_still_bound_to_the_signing_key(db_session_factory):
    """Rotation is a hard cutover: the very tokens that verify above must stop
    verifying the moment the key changes."""
    await _use_key(db_session_factory, "a-completely-different-signing-key-0123456789abcd")

    async with db_session_factory() as db:
        with pytest.raises(InvalidTokenError):
            await decode_token(db, OLD_LIBRARY_ACCESS_TOKEN, TokenType.ACCESS)


# ---- 2. what this module mints ------------------------------------------------------


async def test_a_minted_access_token_round_trips_with_every_claim(db_session_factory):
    await _use_key(db_session_factory)
    user_id = uuid.uuid4()

    async with db_session_factory() as db:
        issued = await create_access_token(db, user_id, "RiskManager")
        payload = await decode_token(db, issued.token, TokenType.ACCESS)

    assert payload["sub"] == str(user_id)
    assert payload["role"] == "RiskManager"
    assert payload["type"] == "access"
    assert payload["jti"] == str(issued.jti)
    assert payload["exp"] == int(issued.expires_at.timestamp())
    assert isinstance(payload["iat"], int) and payload["iat"] <= payload["exp"]


async def test_a_minted_refresh_token_round_trips_with_its_family(db_session_factory):
    await _use_key(db_session_factory)
    family = uuid.uuid4()

    async with db_session_factory() as db:
        issued = await create_refresh_token(db, uuid.uuid4(), family_id=family)
        payload = await decode_token(db, issued.token, TokenType.REFRESH)

    assert payload["family_id"] == str(family) and issued.family_id == family
    assert payload["jti"] == str(issued.jti)


async def test_a_minted_token_is_plain_hs256_checked_without_the_library_that_wrote_it(
    db_session_factory,
):
    await _use_key(db_session_factory)

    async with db_session_factory() as db:
        token = (await create_access_token(db, uuid.uuid4(), "ReadOnlyAuditor")).token

    header_b64, payload_b64, signature_b64 = token.split(".")
    assert json.loads(_b64_decode(header_b64)) == {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(KEY.encode(), signing_input, hashlib.sha256).digest()
    assert _b64_decode(signature_b64) == expected  # standard HMAC-SHA256, byte for byte


# ---- 3. what must be rejected, and only ever as InvalidTokenError ------------------


async def _decode(db_session_factory, token: str, expected: TokenType = TokenType.ACCESS):
    async with db_session_factory() as db:
        return await decode_token(db, token, expected)


async def test_a_token_signed_with_another_secret_is_rejected(db_session_factory):
    await _use_key(db_session_factory)
    attacker_secret = "attacker-chosen-secret-0123456789-abcdefghij"
    forged = jwt.encode(_claims(), attacker_secret, algorithm="HS256")

    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, forged)


async def test_an_unsigned_alg_none_token_is_rejected(db_session_factory):
    await _use_key(db_session_factory)
    unsigned = _hand_signed({"alg": "none", "typ": "JWT"}, _claims(iat=1, exp=4102444800), key=None)

    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, unsigned)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
async def test_the_algorithm_is_pinned_a_valid_signature_under_another_hmac_is_rejected(
    db_session_factory, algorithm
):
    await _use_key(db_session_factory)
    token = jwt.encode(_claims(), KEY, algorithm=algorithm)  # right secret, wrong algorithm

    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, token)


async def test_the_pinned_algorithm_comes_from_settings_not_from_the_token(
    db_session_factory, monkeypatch: pytest.MonkeyPatch
):
    await _use_key(db_session_factory)
    hs256 = jwt.encode(_claims(), KEY, algorithm="HS256")
    hs512 = jwt.encode(_claims(), KEY, algorithm="HS512")
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS512")

    assert (await _decode(db_session_factory, hs512))["type"] == "access"
    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, hs256)


async def test_an_expired_token_is_rejected(db_session_factory):
    await _use_key(db_session_factory)
    expired = jwt.encode(
        _claims(exp=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)), KEY, algorithm="HS256"
    )

    with pytest.raises(InvalidTokenError, match="expired"):
        await _decode(db_session_factory, expired)


async def test_a_payload_edited_after_signing_is_rejected(db_session_factory):
    """The privilege-escalation case: keep the signature, change the role."""
    await _use_key(db_session_factory)
    token = jwt.encode(_claims(role="ReadOnlyAuditor"), KEY, algorithm="HS256")
    header, payload, signature = token.split(".")
    escalated = json.loads(_b64_decode(payload)) | {"role": "SystemAdministrator"}

    with pytest.raises(InvalidTokenError):
        await _decode(
            db_session_factory, f"{header}.{_b64(json.dumps(escalated).encode())}.{signature}"
        )


async def test_a_token_with_its_signature_removed_is_rejected(db_session_factory):
    await _use_key(db_session_factory)
    header, payload, _ = jwt.encode(_claims(), KEY, algorithm="HS256").split(".")

    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, f"{header}.{payload}.")


@pytest.mark.parametrize(
    ("issued_as", "presented_as"),
    [("access", TokenType.REFRESH), ("refresh", TokenType.ACCESS)],
)
async def test_a_token_of_the_wrong_type_is_rejected(db_session_factory, issued_as, presented_as):
    await _use_key(db_session_factory)
    token = jwt.encode(_claims(type=issued_as), KEY, algorithm="HS256")

    with pytest.raises(InvalidTokenError, match="expected token type"):
        await _decode(db_session_factory, token, presented_as)


def _signed_payload(payload: Any) -> str:
    """A correctly signed token whose payload is not what the app issues."""
    return _hand_signed({"alg": "HS256", "typ": "JWT"}, payload)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-jwt",
        "a.b",
        "a.b.c",
        "....",
        "Bearer abc.def.ghi",
        "é.é.é",
        "x" * 100_000,
        "eyJhbGciOiJIUzI1NiJ9.!!!not-base64!!!.sig",
        _signed_payload(["a", "json", "array"]),  # signed, but not a JSON object
        _signed_payload(_claims(iat=1, exp="tomorrow")),  # signed, exp is not a number
        _signed_payload(_claims(iat=1, exp=4102444800, sub=5)),  # signed, sub is not a string
    ],
    ids=[
        "empty",
        "plain-text",
        "two-segments",
        "three-junk-segments",
        "only-dots",
        "bearer-prefix",
        "non-ascii",
        "100kB-of-junk",
        "bad-base64-payload",
        "payload-is-a-json-array",
        "exp-is-a-string",
        "sub-is-an-int",
    ],
)
async def test_malformed_tokens_only_ever_surface_as_invalid_token_error(db_session_factory, token):
    """Whatever the input, the only exception that escapes is `InvalidTokenError`
    -- which the callers map to a 401. A library exception escaping here would
    be a 500 on every protected route."""
    await _use_key(db_session_factory)

    with pytest.raises(InvalidTokenError):
        await _decode(db_session_factory, token)


# ---- a default deployment signs with a short secret ---------------------------------


async def test_a_short_secret_still_works_but_warns_the_operator(db_session_factory):
    """docker-compose.yml's JWT_SECRET_KEY defaults to a short placeholder, and
    the signing-key row is seeded from it. PyJWT warns about an HMAC key under
    32 bytes -- it must not refuse to run, but the warning must exist."""
    await _use_key(db_session_factory, "change-me-in-production")

    with pytest.warns(jwt.warnings.InsecureKeyLengthWarning, match="32 bytes"):
        async with db_session_factory() as db:
            token = (await create_access_token(db, uuid.uuid4(), "ReadOnlyAuditor")).token
            payload = await decode_token(db, token, TokenType.ACCESS)

    assert payload["role"] == "ReadOnlyAuditor"
