"""Live quotes: one batched call per cycle, with the next broker as the fallback (phase 3).

The tick job used to ask the broker about each symbol in turn. It now asks the first connected
broker for all of them at once; what that broker cannot answer -- symbols it has no price for, or
everything when it is down or refused the credentials -- goes to the next connected broker.
"""

import time

import pytest

from src.brokers.base import BrokerAuthError, BrokerQuote, DepthLevel
from src.brokers.tick_source import BrokerQuoteTickSource, FallbackTickSource
from src.engine.paper_trading.depth import load_depth
from src.engine.paper_trading.tick_feed import (
    NoQuoteError,
    SymbolBackoff,
    TickPublishError,
    TickSourceUnavailableError,
    publish_ticks_once,
    read_new_ticks,
    tick_stream_key,
)
from src.engine.risk.circuit_breaker import BrokerServerError
from src.models.data_source import DataSource


def _quote(symbol: str, price: float = 100.0, *, depth: bool = True) -> BrokerQuote:
    from datetime import UTC, datetime

    return BrokerQuote(
        symbol=symbol,
        last_price=price,
        bid=price - 0.05 if depth else None,
        ask=price + 0.05 if depth else None,
        timestamp=datetime.now(UTC),
        depth_bids=(DepthLevel(price - 0.05, 100),) if depth else (),
        depth_asks=(DepthLevel(price + 0.05, 200),) if depth else (),
        open_interest=None,
    )


class _Broker:
    """A broker adapter that prices the symbols in `prices` and records every request."""

    def __init__(self, name: str, prices: dict[str, float], *, error: Exception | None = None):
        self.broker_name = name
        self.prices = prices
        self.error = error
        self.requests: list[list[str]] = []

    async def get_quotes(self, symbols):
        self.requests.append(list(symbols))
        if self.error is not None:
            raise self.error
        return {s: _quote(s, self.prices[s]) for s in symbols if s in self.prices}

    async def get_quote(self, symbol):
        quotes = await self.get_quotes([symbol])
        return quotes[symbol]


def _source(*brokers: _Broker, **kwargs) -> FallbackTickSource:
    return FallbackTickSource([BrokerQuoteTickSource(b, **kwargs) for b in brokers])


# ---- one broker ----


async def test_the_first_broker_is_asked_once_for_every_symbol():
    zerodha = _Broker("zerodha", {"INFY": 1500.0, "TCS": 3800.0})
    upstox = _Broker("upstox", {"INFY": 1501.0, "TCS": 3801.0})

    outcomes = await _source(zerodha, upstox).next_ticks(["INFY", "TCS"])

    assert zerodha.requests == [["INFY", "TCS"]]
    assert upstox.requests == []  # the fallback is not asked when the first one answered
    assert outcomes["INFY"].price == 1500.0 and outcomes["TCS"].price == 3800.0


async def test_a_tick_is_real_and_carries_the_brokers_book():
    tick = (await _source(_Broker("zerodha", {"INFY": 1500.0})).next_ticks(["INFY"]))["INFY"]

    assert tick.source == DataSource.REAL
    assert tick.depth is not None
    assert tick.depth.asks[0].price == pytest.approx(1500.05) and tick.depth.asks[0].quantity == 200
    assert tick.depth.bids[0].quantity == 100


async def test_a_quote_without_a_book_gives_a_tick_without_depth():
    class _NoBook(_Broker):
        async def get_quotes(self, symbols):
            return {s: _quote(s, depth=False) for s in symbols}

    tick = (await _source(_NoBook("zerodha", {})).next_ticks(["INFY"]))["INFY"]

    assert tick.depth is None


# ---- falling back ----


async def test_symbols_the_first_broker_cannot_price_go_to_the_second():
    zerodha = _Broker("zerodha", {"INFY": 1500.0})
    upstox = _Broker("upstox", {"INFY": 1501.0, "TCS": 3801.0})

    outcomes = await _source(zerodha, upstox).next_ticks(["INFY", "TCS"])

    assert upstox.requests == [["TCS"]]  # only what is still missing
    assert outcomes["INFY"].price == 1500.0  # preferred broker wins where it answered
    assert outcomes["TCS"].price == 3801.0


async def test_a_broker_that_is_down_hands_everything_to_the_next_one():
    zerodha = _Broker("zerodha", {}, error=BrokerServerError(503, "zerodha is down"))
    upstox = _Broker("upstox", {"INFY": 1501.0, "TCS": 3801.0})

    outcomes = await _source(zerodha, upstox).next_ticks(["INFY", "TCS"])

    assert upstox.requests == [["INFY", "TCS"]]
    assert {s: t.price for s, t in outcomes.items()} == {"INFY": 1501.0, "TCS": 3801.0}


async def test_a_broker_that_refused_the_credentials_is_skipped_until_it_is_fixed():
    zerodha = _Broker("zerodha", {}, error=BrokerAuthError(403, "token expired"))
    upstox = _Broker("upstox", {"INFY": 1501.0})
    source = _source(zerodha, upstox)

    first = await source.next_ticks(["INFY"])
    second = await source.next_ticks(["INFY"])

    assert first["INFY"].price == second["INFY"].price == 1501.0
    assert len(zerodha.requests) == 1  # asked once, refused, then left alone
    assert len(upstox.requests) == 2


async def test_a_symbol_no_broker_prices_is_reported_as_failed_not_dropped():
    outcomes = await _source(
        _Broker("zerodha", {"INFY": 1500.0}), _Broker("upstox", {"INFY": 1501.0})
    ).next_ticks(["INFY", "GHOST"])

    assert outcomes["INFY"].price == 1500.0
    assert isinstance(outcomes["GHOST"], NoQuoteError)


async def test_when_every_broker_failed_each_symbol_carries_the_failure():
    error = BrokerServerError(503, "down")
    outcomes = await _source(
        _Broker("zerodha", {}, error=error), _Broker("upstox", {}, error=error)
    ).next_ticks(["INFY", "TCS"])

    assert outcomes["INFY"] is error and outcomes["TCS"] is error


async def test_when_every_broker_is_unavailable_the_source_is_unavailable_and_says_why():
    source = _source(
        _Broker("zerodha", {}, error=BrokerAuthError(403, "no")),
        _Broker("upstox", {}, error=BrokerAuthError(401, "no")),
    )

    with pytest.raises(TickSourceUnavailableError) as caught:
        await source.next_ticks(["INFY"])

    assert "zerodha rejected" in str(caught.value) and "upstox rejected" in str(caught.value)


async def test_one_unavailable_broker_does_not_hide_the_others_failure():
    """Zerodha refused the token, Upstox is up but cannot price GHOST: the source is not
    'unavailable', the symbol simply has no quote."""
    outcomes = await _source(
        _Broker("zerodha", {}, error=BrokerAuthError(403, "no")),
        _Broker("upstox", {"INFY": 1501.0}),
    ).next_ticks(["INFY", "GHOST"])

    assert outcomes["INFY"].price == 1501.0 and isinstance(outcomes["GHOST"], NoQuoteError)


async def test_a_single_tick_request_uses_the_same_path():
    source = _source(_Broker("zerodha", {}), _Broker("upstox", {"INFY": 1501.0}))

    assert (await source.next_tick("INFY")).price == 1501.0
    with pytest.raises(NoQuoteError):
        await source.next_tick("GHOST")


async def test_symbols_listed_twice_are_asked_about_once():
    zerodha = _Broker("zerodha", {"INFY": 1500.0})

    await _source(zerodha).next_ticks(["INFY", "INFY"])

    assert zerodha.requests == [["INFY"]]


# ---- the publishing job ----


@pytest.fixture
async def clean(redis_client):
    symbols = ["FBA", "FBB", "FBC"]
    keys = [k for s in symbols for k in (tick_stream_key(s), f"paper:depth:{s}")]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


async def test_a_cycle_makes_one_broker_call_however_many_symbols(clean):
    broker = _Broker("zerodha", {"FBA": 10.0, "FBB": 20.0, "FBC": 30.0})

    published = await publish_ticks_once(clean, _source(broker), ["FBA", "FBB", "FBC"])

    assert broker.requests == [["FBA", "FBB", "FBC"]]
    assert [t.price for t in published] == [10.0, 20.0, 30.0]
    for symbol, price in (("FBA", 10.0), ("FBB", 20.0), ("FBC", 30.0)):
        ticks, _ = await read_new_ticks(clean, symbol)
        assert [t.price for t in ticks] == [price]


async def test_the_books_are_stored_beside_the_ticks(clean):
    await publish_ticks_once(clean, _source(_Broker("zerodha", {"FBA": 10.0})), ["FBA"])

    depth = await load_depth(clean, "FBA")
    assert depth is not None and depth.asks[0].quantity == 200
    assert abs(depth.timestamp_ms - time.time() * 1000) < 5_000


async def test_one_symbol_without_a_quote_does_not_starve_the_others(clean):
    broker = _Broker("zerodha", {"FBA": 10.0, "FBC": 30.0})

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(clean, _source(broker), ["FBA", "FBB", "FBC"])

    assert list(caught.value.failures) == ["FBB"]
    assert [t.symbol for t in caught.value.published] == ["FBA", "FBC"]
    assert (await read_new_ticks(clean, "FBC"))[0]  # still published


async def test_a_symbol_that_failed_is_backed_off_and_left_out_of_the_next_call(clean):
    broker = _Broker("zerodha", {"FBA": 10.0})
    backoff = SymbolBackoff(initial_seconds=60, max_seconds=600, clock=lambda: 1000.0)
    source = _source(broker)

    with pytest.raises(TickPublishError):
        await publish_ticks_once(clean, source, ["FBA", "GHOST"], backoff=backoff)
    with pytest.raises(TickPublishError):
        await publish_ticks_once(clean, source, ["FBA", "GHOST"], backoff=backoff)

    assert broker.requests == [["FBA", "GHOST"], ["FBA"]]  # GHOST not asked about again


async def test_a_whole_cycle_failure_fails_each_symbol_and_backs_each_off(clean):
    broker = _Broker("zerodha", {}, error=BrokerServerError(503, "down"))
    backoff = SymbolBackoff(initial_seconds=60, max_seconds=600, clock=lambda: 1000.0)

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(clean, _source(broker), ["FBA", "FBB"], backoff=backoff)

    assert set(caught.value.failures) == {"FBA", "FBB"}
    assert backoff.remaining("FBA") > 0 and backoff.remaining("FBB") > 0


async def test_an_unavailable_source_stops_the_cycle_unwrapped(clean):
    source = _source(_Broker("zerodha", {}, error=BrokerAuthError(403, "no")))

    with pytest.raises(TickSourceUnavailableError):
        await publish_ticks_once(clean, source, ["FBA"])


async def test_the_stream_is_trimmed_as_ticks_are_published(clean):
    broker = _Broker("zerodha", {"FBA": 10.0})
    source = _source(broker)

    for _ in range(300):
        await publish_ticks_once(clean, source, ["FBA"], maxlen=100)

    assert 100 <= await clean.xlen(tick_stream_key("FBA")) < 300


async def test_a_batch_source_that_simply_omits_a_symbol_fails_only_that_symbol(clean):
    """`BrokerQuoteTickSource.next_ticks` leaves out a symbol the broker has no price for."""
    source = BrokerQuoteTickSource(_Broker("zerodha", {"FBA": 10.0, "FBC": 30.0}))

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(clean, source, ["FBA", "FBB", "FBC"])

    assert list(caught.value.failures) == ["FBB"]
    assert isinstance(caught.value.failures["FBB"], NoQuoteError)
    assert [t.symbol for t in caught.value.published] == ["FBA", "FBC"]


async def test_a_batch_call_that_raises_fails_every_symbol_in_it_not_the_job(clean):
    source = BrokerQuoteTickSource(_Broker("zerodha", {}, error=ConnectionError("reset")))

    with pytest.raises(TickPublishError) as caught:
        await publish_ticks_once(clean, source, ["FBA", "FBB"])

    assert set(caught.value.failures) == {"FBA", "FBB"}
    assert all(isinstance(e, ConnectionError) for e in caught.value.failures.values())


async def test_a_refused_broker_and_one_pricing_nothing_is_a_failed_symbol_not_a_dead_feed():
    outcomes = await _source(
        _Broker("zerodha", {}, error=BrokerAuthError(403, "no")), _Broker("upstox", {})
    ).next_ticks(["GHOST"])

    assert isinstance(outcomes["GHOST"], NoQuoteError)
