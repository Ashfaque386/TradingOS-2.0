"""Redis Stream tick feed tests (Build Spec §11): a stream-based drain-
since-cursor read, not pub/sub -- a consumer catching up later must still
see every tick.
"""

import asyncio

import pytest

from src.engine.paper_trading import tick_feed as tick_feed_module
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    Tick,
    TickPublishError,
    TickSourceUnavailableError,
    get_latest_tick,
    publish_tick,
    publish_ticks_once,
    read_new_ticks,
    tick_stream_key,
)


async def test_publish_then_read_from_scratch(redis_client):
    symbol = "TESTTICKFEED_A"
    await redis_client.delete(tick_stream_key(symbol))

    source = MockTickSource()
    published = await publish_ticks_once(redis_client, source, [symbol])

    ticks, cursor = await read_new_ticks(redis_client, symbol)
    assert len(ticks) == 1
    assert ticks[0].price == published[0].price
    assert cursor != "0"

    await redis_client.delete(tick_stream_key(symbol))


async def test_reading_again_with_the_same_cursor_returns_nothing_new(redis_client):
    symbol = "TESTTICKFEED_B"
    await redis_client.delete(tick_stream_key(symbol))

    source = MockTickSource()
    await publish_ticks_once(redis_client, source, [symbol])
    _first_ticks, cursor = await read_new_ticks(redis_client, symbol)

    ticks_again, cursor_again = await read_new_ticks(redis_client, symbol, last_id=cursor)
    assert ticks_again == []
    assert cursor_again == cursor

    await redis_client.delete(tick_stream_key(symbol))


async def test_a_consumer_that_falls_behind_still_sees_every_tick(redis_client):
    """The whole reason for a Stream over pub/sub: a consumer that wasn't
    listening yet still catches every published tick once it reads."""
    symbol = "TESTTICKFEED_C"
    await redis_client.delete(tick_stream_key(symbol))

    source = MockTickSource()
    published = []
    for _ in range(5):
        published += await publish_ticks_once(redis_client, source, [symbol])

    ticks, _cursor = await read_new_ticks(redis_client, symbol)
    assert len(ticks) == 5
    assert [t.price for t in ticks] == [p.price for p in published]

    await redis_client.delete(tick_stream_key(symbol))


async def test_publish_tick_writes_to_the_symbol_specific_stream(redis_client):
    symbol = "TESTTICKFEED_D"
    await redis_client.delete(tick_stream_key(symbol))

    await publish_tick(redis_client, Tick(symbol=symbol, price=123.45, timestamp_ms=1_000_000))
    ticks, _cursor = await read_new_ticks(redis_client, symbol)
    assert len(ticks) == 1
    assert ticks[0].price == 123.45

    await redis_client.delete(tick_stream_key(symbol))


async def test_get_latest_tick_returns_none_when_nothing_has_been_published(redis_client):
    symbol = "TESTTICKFEED_E"
    await redis_client.delete(tick_stream_key(symbol))

    assert await get_latest_tick(redis_client, symbol) is None


async def test_get_latest_tick_returns_the_most_recently_published_tick(redis_client):
    symbol = "TESTTICKFEED_F"
    await redis_client.delete(tick_stream_key(symbol))

    await publish_tick(redis_client, Tick(symbol=symbol, price=100.0, timestamp_ms=1_000_000))
    await publish_tick(redis_client, Tick(symbol=symbol, price=105.5, timestamp_ms=2_000_000))

    latest = await get_latest_tick(redis_client, symbol)
    assert latest is not None
    assert latest.price == 105.5
    assert latest.timestamp_ms == 2_000_000

    await redis_client.delete(tick_stream_key(symbol))


# ---- One symbol's failure must not stop the others ------------------------------
# `publish_ticks_once` used to abort at the first symbol that raised, so a symbol
# the broker could not quote (the live database had an active `DEMOSTOCK`
# subscription Zerodha does not list) stopped every symbol after it from getting a
# tick -- on every firing, for as long as the subscription existed.


class _SelectiveSource:
    """A source that fails for chosen symbols and prices the rest."""

    def __init__(self, failing: dict[str, Exception]) -> None:
        self.failing = failing
        self.attempted: list[str] = []

    async def next_tick(self, symbol: str) -> Tick:
        self.attempted.append(symbol)
        if symbol in self.failing:
            raise self.failing[symbol]
        return Tick(symbol=symbol, price=100.0 + len(self.attempted), timestamp_ms=1_000)


async def _clean(redis_client, *symbols: str) -> None:
    for symbol in symbols:
        await redis_client.delete(tick_stream_key(symbol))


async def test_a_failing_symbol_does_not_stop_the_symbols_around_it(redis_client):
    symbols = ["ISOA", "ISOBAD", "ISOC"]
    await _clean(redis_client, *symbols)
    source = _SelectiveSource({"ISOBAD": RuntimeError("no quote")})

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(redis_client, source, symbols)

    # Every symbol was attempted, in order, and the good ones were published.
    assert source.attempted == symbols
    assert await redis_client.xlen(tick_stream_key("ISOA")) == 1
    assert await redis_client.xlen(tick_stream_key("ISOC")) == 1
    assert await redis_client.xlen(tick_stream_key("ISOBAD")) == 0
    assert list(caught.value.failures) == ["ISOBAD"]
    assert [t.symbol for t in caught.value.published] == ["ISOA", "ISOC"]
    assert isinstance(caught.value.__cause__, RuntimeError)  # the real reason stays reachable
    await _clean(redis_client, *symbols)


async def test_the_error_names_the_failed_symbols_and_why(redis_client):
    await _clean(redis_client, "ISOA", "ISOB")
    source = _SelectiveSource({"ISOA": ValueError("bad symbol"), "ISOB": KeyError("boom")})

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(redis_client, source, ["ISOA", "ISOB", "ISOGOOD"])

    assert str(caught.value) == (
        "tick publish failed for 2 of 3 symbols: "
        "ISOA (ValueError: bad symbol); ISOB (KeyError: 'boom')"
    )
    await _clean(redis_client, "ISOGOOD")


async def test_a_long_list_of_failures_is_summarised_not_dumped(redis_client):
    symbols = [f"ISOMANY{i}" for i in range(9)]
    source = _SelectiveSource({s: RuntimeError("x" * 500) for s in symbols})

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(redis_client, source, symbols)

    message = str(caught.value)
    assert message.startswith("tick publish failed for 9 of 9 symbols: ")
    assert message.endswith("; and 4 more")
    assert "ISOMANY4" in message and "ISOMANY5" not in message
    assert "x" * 121 not in message  # each reason is cut short


async def test_when_every_symbol_succeeds_it_still_returns_the_ticks(redis_client):
    await _clean(redis_client, "ISOA", "ISOB")

    ticks = await publish_ticks_once(redis_client, _SelectiveSource({}), ["ISOA", "ISOB"])

    assert [t.symbol for t in ticks] == ["ISOA", "ISOB"]
    await _clean(redis_client, "ISOA", "ISOB")


async def test_a_source_that_is_down_stops_the_round_and_is_not_wrapped(redis_client):
    await _clean(redis_client, "ISOA", "ISOC")
    source = _SelectiveSource({"ISOBAD": TickSourceUnavailableError("broker rejected the token")})

    with pytest.raises(TickSourceUnavailableError, match="broker rejected the token"):
        await publish_ticks_once(redis_client, source, ["ISOA", "ISOBAD", "ISOC"])

    assert source.attempted == ["ISOA", "ISOBAD"]  # nothing after it was tried
    assert await redis_client.xlen(tick_stream_key("ISOA")) == 1  # what went out stays out
    await _clean(redis_client, "ISOA")


async def test_a_redis_failure_for_one_symbol_is_isolated_too(redis_client, monkeypatch):
    await _clean(redis_client, "ISOA", "ISOBAD", "ISOC")
    real_publish = tick_feed_module.publish_tick

    async def flaky_publish(redis, tick):
        if tick.symbol == "ISOBAD":
            raise ConnectionError("stream unavailable")
        return await real_publish(redis, tick)

    monkeypatch.setattr(tick_feed_module, "publish_tick", flaky_publish)

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(redis_client, _SelectiveSource({}), ["ISOA", "ISOBAD", "ISOC"])

    assert list(caught.value.failures) == ["ISOBAD"]
    assert await redis_client.xlen(tick_stream_key("ISOC")) == 1
    await _clean(redis_client, "ISOA", "ISOC")


async def test_cancelling_the_job_is_never_swallowed_as_a_symbol_failure(redis_client):
    source = _SelectiveSource({"ISOA": asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await publish_ticks_once(redis_client, source, ["ISOA", "ISOB"])

    assert source.attempted == ["ISOA"]  # shutdown means stop, not "try the next one"
