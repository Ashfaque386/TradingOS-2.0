"""Redis Stream tick feed tests (Build Spec §11): a stream-based drain-
since-cursor read, not pub/sub -- a consumer catching up later must still
see every tick.
"""

import asyncio

import pytest

from src.engine.paper_trading import tick_feed as tick_feed_module
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    SymbolBackedOffError,
    SymbolBackoff,
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

    async def flaky_publish(redis, tick, **kwargs):
        if tick.symbol == "ISOBAD":
            raise ConnectionError("stream unavailable")
        return await real_publish(redis, tick, **kwargs)

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


# ---- A symbol the broker cannot quote is not asked about every round ---------------
# One broker call per firing for as long as the subscription exists: 1,200 an hour at
# the 3-second cadence, for a symbol that will not start working by itself.


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def _round(redis_client, source, symbols, backoff):
    try:
        return await publish_ticks_once(redis_client, source, symbols, backoff=backoff)
    except TickPublishError as exc:
        return exc


async def test_a_failed_symbol_is_skipped_until_its_backoff_expires(redis_client):
    await _clean(redis_client, "BOGOOD", "BOBAD")
    clock = _Clock()
    backoff = SymbolBackoff(initial_seconds=30, max_seconds=600, clock=clock)
    source = _SelectiveSource({"BOBAD": RuntimeError("no quote")})

    first = await _round(redis_client, source, ["BOGOOD", "BOBAD"], backoff)
    asked_after_first = source.attempted.count("BOBAD")
    clock.now += 29
    second = await _round(redis_client, source, ["BOGOOD", "BOBAD"], backoff)

    assert asked_after_first == 1
    assert source.attempted.count("BOBAD") == 1  # not asked again inside the window
    assert source.attempted.count("BOGOOD") == 2  # the good symbol is asked every round
    # Still reported -- the job keeps failing visibly -- but as backed off, with the reason.
    assert isinstance(first, TickPublishError) and isinstance(second, TickPublishError)
    skipped = second.failures["BOBAD"]
    assert isinstance(skipped, SymbolBackedOffError)
    assert "backing off for 1s after: RuntimeError: no quote" in str(skipped)
    assert [t.symbol for t in second.published] == ["BOGOOD"]
    await _clean(redis_client, "BOGOOD", "BOBAD")


async def test_the_delay_doubles_on_each_failed_attempt_up_to_the_cap(redis_client):
    clock = _Clock()
    backoff = SymbolBackoff(initial_seconds=30, max_seconds=100, clock=clock)
    source = _SelectiveSource({"BODOUBLE": RuntimeError("no quote")})

    delays = []
    for _ in range(5):
        await _round(redis_client, source, ["BODOUBLE"], backoff)
        delays.append(backoff.remaining("BODOUBLE"))
        clock.now += backoff.remaining("BODOUBLE") + 0.1  # let it expire, then probe again

    assert delays == [30, 60, 100, 100, 100]
    assert source.attempted.count("BODOUBLE") == 5  # one real attempt per expiry, no more


async def test_a_success_clears_the_backoff_and_says_so(redis_client, monkeypatch):
    class _Log:
        def __init__(self) -> None:
            self.events: list[tuple[str, str, dict]] = []

        def warning(self, event, **kw):
            self.events.append(("warning", event, kw))

        def info(self, event, **kw):
            self.events.append(("info", event, kw))

    log = _Log()
    monkeypatch.setattr(tick_feed_module, "logger", log)
    await _clean(redis_client, "BOHEAL")
    clock = _Clock()
    backoff = SymbolBackoff(initial_seconds=30, clock=clock)
    source = _SelectiveSource({"BOHEAL": RuntimeError("no quote")})

    await _round(redis_client, source, ["BOHEAL"], backoff)
    source.failing.clear()  # the broker starts quoting it
    clock.now += 31
    healed = await _round(redis_client, source, ["BOHEAL"], backoff)
    again = await _round(redis_client, source, ["BOHEAL"], backoff)

    assert [t.symbol for t in healed] == ["BOHEAL"] and [t.symbol for t in again] == ["BOHEAL"]
    assert backoff.remaining("BOHEAL") == 0
    failed = [e for e in log.events if e[1] == "tick_feed.symbol_failed"]
    recovered = [e for e in log.events if e[1] == "tick_feed.symbol_recovered"]
    assert len(failed) == 1 and failed[0][2]["symbol"] == "BOHEAL"
    assert failed[0][2]["next_attempt_in_seconds"] == 30
    assert len(recovered) == 1  # said once, on the transition -- not on every good round
    await _clean(redis_client, "BOHEAL")


async def test_a_new_tick_source_clears_the_backoff(redis_client):
    """A reconnect builds a fresh source (`get_tick_source`); whatever made the
    symbols fail may be gone, so they are asked about again at once."""
    clock = _Clock()
    backoff = SymbolBackoff(initial_seconds=30, clock=clock)
    old = _SelectiveSource({"BONEW": RuntimeError("no quote")})
    await _round(redis_client, old, ["BONEW"], backoff)
    assert backoff.remaining("BONEW") > 0

    new = _SelectiveSource({})
    ticks = await _round(redis_client, new, ["BONEW"], backoff)

    assert [t.symbol for t in ticks] == ["BONEW"]
    assert new.attempted == ["BONEW"]
    await _clean(redis_client, "BONEW")


async def test_without_a_backoff_every_round_asks_about_every_symbol(redis_client):
    source = _SelectiveSource({"BONONE": RuntimeError("no quote")})

    for _ in range(3):
        with pytest.raises(TickPublishError):
            await publish_ticks_once(redis_client, source, ["BONONE"])

    assert source.attempted == ["BONONE"] * 3  # unchanged behaviour when none is given


async def test_a_source_that_is_down_is_not_a_symbol_failure_and_sets_no_backoff(redis_client):
    backoff = SymbolBackoff(clock=_Clock())
    source = _SelectiveSource({"BODOWN": TickSourceUnavailableError("token rejected")})

    with pytest.raises(TickSourceUnavailableError):
        await publish_ticks_once(redis_client, source, ["BODOWN"], backoff=backoff)

    assert backoff.remaining("BODOWN") == 0  # it is the source, not this symbol
