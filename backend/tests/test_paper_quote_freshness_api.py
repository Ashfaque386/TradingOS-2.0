"""GET /api/v1/paper-trading/subscriptions shows how fresh a price is by the exchange's own clock,
the exchange's circuit limits where the quote carried them, and a refused quote -- so the desk can
say so instead of showing a price that only looks live.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from src.core.redis_client import get_redis
from src.core.roles import Role
from src.engine.paper_trading.tick_feed import (
    QuoteRefusedError,
    Tick,
    note_quote_refusal,
    publish_tick,
    quote_refusal_key,
    tick_stream_key,
)
from src.main import app
from tests.test_engine_alert_events_wiring import _subscription

URL = "/api/v1/paper-trading/subscriptions"


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [tick_stream_key("DEMOSTOCK"), quote_refusal_key("DEMOSTOCK")]
    await redis_client.delete(*keys)
    app.dependency_overrides[get_redis] = lambda: redis_client
    yield redis_client
    app.dependency_overrides.pop(get_redis, None)
    await redis_client.delete(*keys)


async def _headers(client, make_user):
    await make_user("fresh@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "fresh@example.com", "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _row(client, headers):
    [row] = (await client.get(URL, headers=headers)).json()
    return row


async def test_a_price_carries_the_exchanges_stamp_its_age_and_the_circuit_limits(
    client, make_user, db_session_factory, redis
):
    await _subscription(db_session_factory)
    stamp = datetime.now(UTC) - timedelta(seconds=7)
    await publish_tick(
        redis,
        Tick(
            "DEMOSTOCK",
            101.5,
            int(time.time() * 1000),
            "real",
            exchange_time_ms=int(stamp.timestamp() * 1000),
            lower_circuit=90.0,
            upper_circuit=110.0,
        ),
    )

    row = await _row(client, await _headers(client, make_user))

    tick = row["last_tick"]
    assert tick["price"] == 101.5
    assert tick["exchange_time"].startswith(stamp.strftime("%Y-%m-%dT%H:%M:%S"))
    assert 6.5 < tick["exchange_age_seconds"] < 15  # the exchange's clock, not ours
    assert (tick["lower_circuit"], tick["upper_circuit"]) == (90.0, 110.0)
    assert row["quote_refusal"] is None


async def test_a_price_without_them_says_so_with_nulls(
    client, make_user, db_session_factory, redis
):
    await _subscription(db_session_factory)
    await publish_tick(redis, Tick("DEMOSTOCK", 100.0, int(time.time() * 1000), "real"))

    tick = (await _row(client, await _headers(client, make_user)))["last_tick"]

    assert tick["exchange_time"] is None and tick["exchange_age_seconds"] is None
    assert tick["lower_circuit"] is None and tick["upper_circuit"] is None


async def test_the_fallback_band_is_the_reference_tables_for_that_symbol(
    client, make_user, db_session_factory, redis
):
    await _subscription(db_session_factory)

    row = await _row(client, await _headers(client, make_user))

    assert row["circuit_fallback_band_pct"] == 20.0  # the default for a symbol with no entry


async def test_a_refused_quote_is_shown_with_who_refused_it_and_why(
    client, make_user, db_session_factory, redis
):
    await _subscription(db_session_factory)
    await note_quote_refusal(
        redis,
        QuoteRefusedError(
            symbol="DEMOSTOCK",
            broker="zerodha",
            reason="the exchange stamp (2026-10-30 15:29 IST) is from a previous day",
            exchange_time=datetime(2026, 10, 30, 9, 59, tzinfo=UTC),
        ),
    )

    refusal = (await _row(client, await _headers(client, make_user)))["quote_refusal"]

    assert refusal["broker"] == "zerodha"
    assert refusal["reason"] == "the exchange stamp (2026-10-30 15:29 IST) is from a previous day"
    assert refusal["exchange_time"].startswith("2026-10-30T09:59:00")
    assert refusal["at"] is not None
