"""The broker's live depth as paper fills use it (real-market-data plan, phase 3).

A tick can carry the broker's 5-level book; publishing the tick stores it as the symbol's latest
snapshot. A paper fill walks that snapshot -- and refuses when there is none, when it is stale, or
when the side the order needs is empty, rather than filling against a book that is not there.
"""

import time

import pytest

from src.engine.paper_trading.depth import (
    DepthSnapshot,
    DepthUnavailableError,
    RealDepthOrderBookProvider,
    build_order_book_provider,
    depth_key,
    load_depth,
    publish_depth,
)
from src.engine.paper_trading.order_book import (
    MockOrderBookProvider,
    OrderBookLevel,
    book_for,
    simulate_fill,
)
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.models.data_source import DataSource


def _depth(*, age_seconds: float = 0.0, bids=True, asks=True) -> DepthSnapshot:
    return DepthSnapshot(
        bids=(OrderBookLevel(100.0, 50), OrderBookLevel(99.9, 200)) if bids else (),
        asks=(OrderBookLevel(100.1, 30), OrderBookLevel(100.2, 400)) if asks else (),
        timestamp_ms=int((time.time() - age_seconds) * 1000),
        open_interest=1234.0,
    )


@pytest.fixture
async def clean(redis_client):
    keys = [depth_key("DEPTHSYM"), tick_stream_key("DEPTHSYM")]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


def test_a_snapshot_survives_the_trip_through_json():
    snapshot = _depth()

    assert DepthSnapshot.from_json(snapshot.to_json()) == snapshot


def test_a_snapshot_with_no_levels_is_empty():
    assert DepthSnapshot((), (), 0).is_empty()
    assert not _depth(asks=False).is_empty()


async def test_published_depth_is_the_latest_snapshot_and_expires(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)

    stored = await load_depth(clean, "DEPTHSYM")
    assert stored is not None and stored.asks[0] == OrderBookLevel(100.1, 30)
    assert 50_000 < await clean.pttl(depth_key("DEPTHSYM")) <= 60_000  # 60 s, not 60 ms


async def test_a_symbol_with_no_depth_loads_as_none(clean):
    assert await load_depth(clean, "DEPTHSYM") is None


# ---- publishing a tick ----


async def test_a_tick_with_depth_stores_the_book_and_appends_only_the_price(clean):
    tick = Tick("DEPTHSYM", 100.05, int(time.time() * 1000), DataSource.REAL, depth=_depth())

    await publish_tick(clean, tick, depth_ttl_seconds=60)

    assert (await load_depth(clean, "DEPTHSYM")) == tick.depth
    [(_, fields)] = await clean.xrange(tick_stream_key("DEPTHSYM"))
    assert set(fields) == {"price", "timestamp_ms", "source"}  # the book is not in the stream


async def test_a_tick_without_depth_leaves_the_stored_book_alone(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)

    await publish_tick(clean, Tick("DEPTHSYM", 100.0, int(time.time() * 1000), DataSource.REAL))

    assert await load_depth(clean, "DEPTHSYM") is not None


async def test_a_tick_with_an_empty_book_does_not_replace_a_good_one(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)
    empty = Tick(
        "DEPTHSYM", 100.0, int(time.time() * 1000), DataSource.REAL, depth=DepthSnapshot((), (), 1)
    )

    await publish_tick(clean, empty)

    stored = await load_depth(clean, "DEPTHSYM")
    assert stored is not None and not stored.is_empty()


async def test_the_tick_stream_is_trimmed_to_about_maxlen(clean):
    for i in range(300):
        await publish_tick(clean, Tick("DEPTHSYM", 100.0 + i, i, DataSource.REAL), maxlen=100)

    length = await clean.xlen(tick_stream_key("DEPTHSYM"))
    assert 100 <= length < 300  # approximate trim: never below maxlen, never unbounded
    newest = (await clean.xrevrange(tick_stream_key("DEPTHSYM"), count=1))[0][1]
    assert float(newest["price"]) == 399.0  # the newest tick is the one kept


async def test_without_a_maxlen_the_stream_is_not_trimmed(clean):
    for i in range(150):
        await publish_tick(clean, Tick("DEPTHSYM", 100.0, i, DataSource.REAL))

    assert await clean.xlen(tick_stream_key("DEPTHSYM")) == 150


# ---- the provider ----


async def test_fresh_depth_is_returned_as_the_book(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    book = await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.05)

    assert [(level.price, level.quantity) for level in book.asks] == [(100.1, 30), (100.2, 400)]
    assert [(level.price, level.quantity) for level in book.bids] == [(100.0, 50), (99.9, 200)]


async def test_an_order_walks_the_real_levels_and_pays_the_average_across_them(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    book = await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.05)
    fill = simulate_fill(side="buy", quantity=130, book=book)

    # 30 @ 100.1 + 100 @ 100.2
    assert fill.filled_quantity == 130 and fill.fully_filled
    assert fill.avg_fill_price == pytest.approx((30 * 100.1 + 100 * 100.2) / 130)


async def test_a_thin_book_gives_an_honest_partial_fill(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    book = await book_for(provider, "DEPTHSYM", side="sell", mid_price=100.0)
    fill = simulate_fill(side="sell", quantity=1000, book=book)

    assert fill.filled_quantity == 250 and fill.remaining_quantity == 750
    assert not fill.fully_filled


async def test_no_snapshot_means_no_book(clean):
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    with pytest.raises(DepthUnavailableError, match="no live order book"):
        await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.0)


async def test_a_stale_snapshot_is_refused_and_says_how_old(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(age_seconds=45), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    with pytest.raises(DepthUnavailableError, match="45s old"):
        await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.0)


async def test_a_snapshot_just_inside_the_limit_is_used(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(age_seconds=29), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    assert (await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.0)).asks


@pytest.mark.parametrize(
    ("side", "empty", "word"),
    [("buy", {"asks": False}, "asks"), ("sell", {"bids": False}, "bids")],
)
async def test_a_book_empty_on_the_side_the_order_needs_is_refused(clean, side, empty, word):
    await publish_depth(clean, "DEPTHSYM", _depth(**empty), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    with pytest.raises(DepthUnavailableError, match=word):
        await book_for(provider, "DEPTHSYM", side=side, mid_price=100.0)


async def test_the_other_side_being_empty_does_not_stop_an_order(clean):
    await publish_depth(clean, "DEPTHSYM", _depth(bids=False), ttl_seconds=60)
    provider = RealDepthOrderBookProvider(clean, max_age_seconds=30)

    assert (await book_for(provider, "DEPTHSYM", side="buy", mid_price=100.0)).asks


def test_the_synchronous_snapshot_refuses_instead_of_filling_on_nothing(redis_client):
    provider = RealDepthOrderBookProvider(redis_client)

    with pytest.raises(DepthUnavailableError):
        provider.snapshot("DEPTHSYM", mid_price=100.0)


async def test_a_book_from_real_depth_labels_a_fill_real():
    assert RealDepthOrderBookProvider.data_source == DataSource.REAL


async def test_the_synthetic_provider_is_still_answered_directly():
    book = await book_for(MockOrderBookProvider(), "ANY", side="buy", mid_price=100.0)

    assert len(book.asks) == 5


# ---- which provider the app uses ----


@pytest.mark.parametrize(
    ("synthetic", "expected"),
    [("true", MockOrderBookProvider), ("false", RealDepthOrderBookProvider)],
)
def test_synthetic_mode_is_the_only_way_to_get_the_generated_book(
    monkeypatch, redis_client, synthetic, expected
):
    from src.core.config import get_settings

    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", synthetic)
    get_settings.cache_clear()
    try:
        assert isinstance(build_order_book_provider(redis_client), expected)
    finally:
        get_settings.cache_clear()


def test_the_real_provider_uses_the_configured_max_age(monkeypatch, redis_client):
    from src.core.config import get_settings

    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "false")
    monkeypatch.setenv("PAPER_DEPTH_MAX_AGE_SECONDS", "7")
    get_settings.cache_clear()
    try:
        provider = build_order_book_provider(redis_client)
        assert isinstance(provider, RealDepthOrderBookProvider)
        assert provider.max_age_seconds == 7.0
    finally:
        get_settings.cache_clear()
