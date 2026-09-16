"""Redis Stream tick feed tests (Build Spec §11): a stream-based drain-
since-cursor read, not pub/sub -- a consumer catching up later must still
see every tick.
"""

from src.engine.paper_trading.tick_feed import (
    MockTickSource,
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
    from src.engine.paper_trading.tick_feed import Tick

    symbol = "TESTTICKFEED_D"
    await redis_client.delete(tick_stream_key(symbol))

    await publish_tick(redis_client, Tick(symbol=symbol, price=123.45, timestamp_ms=1_000_000))
    ticks, _cursor = await read_new_ticks(redis_client, symbol)
    assert len(ticks) == 1
    assert ticks[0].price == 123.45

    await redis_client.delete(tick_stream_key(symbol))
