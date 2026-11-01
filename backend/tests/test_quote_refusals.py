"""A quote whose exchange stamp is too old is refused at the one place every consumer reads from.

The tick source (src.brokers.tick_source) judges each quote with the stamp guard and turns a stale
one into a `QuoteRefusedError` instead of a tick; the publisher (`publish_ticks_once`) does not back
the symbol off, remembers the refusal for the desk, and the fallback source does not ask the next
broker. A fresh quote now carries its exchange stamp and circuit limits through Redis to the
consumers.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from src.brokers import feed_health
from src.brokers.base import BrokerQuote
from src.brokers.tick_source import (
    REFUSAL_LOG_INTERVAL_SECONDS,
    BrokerQuoteTickSource,
    FallbackTickSource,
)
from src.core.config import get_settings
from src.engine.paper_trading.tick_feed import (
    QUOTE_REFUSAL_TTL_SECONDS,
    QuoteRefusedError,
    SymbolBackoff,
    Tick,
    TickPublishError,
    get_latest_tick,
    get_quote_refusal,
    note_quote_refusal,
    publish_tick,
    publish_ticks_once,
    quote_refusal_key,
    read_new_ticks,
    tick_circuit_limits,
    tick_stream_key,
)
from src.engine.risk.compliance import ExchangeCircuitLimits
from src.observability.metrics import paper_quotes_refused_exchange_stale_total as refused_counter

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOLS = ("QRA", "QRB")


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [tick_stream_key(s) for s in SYMBOLS] + [quote_refusal_key(s) for s in SYMBOLS]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    feed_health.clear()
    monkeypatch.delenv("PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS", raising=False)
    get_settings.cache_clear()
    yield
    feed_health.clear()
    get_settings.cache_clear()


def _fresh() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=3)


def _yesterday() -> datetime:
    return datetime.now(UTC) - timedelta(days=1, hours=2)


def _quote(symbol, *, stamp, price=100.0, lower=None, upper=None) -> BrokerQuote:
    return BrokerQuote(
        symbol=symbol,
        last_price=price,
        bid=None,
        ask=None,
        timestamp=datetime.now(UTC),
        exchange_time=stamp,
        lower_circuit=lower,
        upper_circuit=upper,
    )


class _Adapter:
    def __init__(self, name, quotes):
        self.broker_name = name
        self.quotes = quotes
        self.requests = []

    async def get_quotes(self, symbols):
        self.requests.append(list(symbols))
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    async def get_quote(self, symbol):
        return self.quotes[symbol]


# ---- what a tick carries ----


async def test_a_tick_carries_its_exchange_stamp_and_circuit_limits_through_redis(redis):
    stamp = datetime(2026, 11, 2, 9, 30, 5, tzinfo=IST)
    tick = Tick(
        "QRA",
        101.5,
        1_700_000_000_000,
        "real",
        exchange_time_ms=int(stamp.timestamp() * 1000),
        lower_circuit=90.0,
        upper_circuit=110.0,
    )

    await publish_tick(redis, tick)

    [read], _cursor = await read_new_ticks(redis, "QRA")
    latest = await get_latest_tick(redis, "QRA")
    for got in (read, latest):
        assert got.exchange_time_ms == int(stamp.timestamp() * 1000)
        assert (got.lower_circuit, got.upper_circuit) == (90.0, 110.0)
        assert got.price == 101.5


async def test_a_tick_without_them_reads_back_with_none_as_old_entries_do(redis):
    await publish_tick(redis, Tick("QRA", 100.0, 1_700_000_000_000, "real"))
    # An entry written before these fields existed.
    await redis.xadd(
        tick_stream_key("QRB"), {"price": "50.0", "timestamp_ms": "1700000000000", "source": "x"}
    )

    for symbol in SYMBOLS:
        tick = await get_latest_tick(redis, symbol)
        assert (tick.exchange_time_ms, tick.lower_circuit, tick.upper_circuit) == (None, None, None)


async def test_only_a_complete_band_is_written(redis):
    await publish_tick(redis, Tick("QRA", 100.0, 1, "real", lower_circuit=90.0))  # one end only

    tick = await get_latest_tick(redis, "QRA")
    assert (tick.lower_circuit, tick.upper_circuit) == (None, None)


def test_a_tick_hands_its_limits_to_the_risk_gate_only_when_it_has_both():
    assert tick_circuit_limits(Tick("X", 1.0, 0)) is None
    assert tick_circuit_limits(Tick("X", 1.0, 0, lower_circuit=9.0)) is None
    assert tick_circuit_limits(Tick("X", 1.0, 0, upper_circuit=11.0)) is None
    assert tick_circuit_limits(Tick("X", 1.0, 0, lower_circuit=9.0, upper_circuit=11.0)) == (
        ExchangeCircuitLimits(lower=9.0, upper=11.0)
    )


# ---- the tick source ----


async def test_a_fresh_quote_becomes_a_tick_with_the_exchanges_stamp_and_limits():
    stamp = _fresh()
    adapter = _Adapter("zerodha", {"QRA": _quote("QRA", stamp=stamp, lower=90.0, upper=110.0)})

    got = (await BrokerQuoteTickSource(adapter).next_ticks(["QRA"]))["QRA"]

    assert isinstance(got, Tick)
    assert got.exchange_time_ms == int(stamp.timestamp() * 1000)
    assert (got.lower_circuit, got.upper_circuit) == (90.0, 110.0)
    assert feed_health.get_quote_refusals("zerodha") is None


async def test_a_quote_stamped_on_a_previous_day_is_refused_not_published():
    adapter = _Adapter(
        "zerodha", {"QRA": _quote("QRA", stamp=_yesterday()), "QRB": _quote("QRB", stamp=_fresh())}
    )
    before = refused_counter.labels(broker="zerodha")._value.get()

    got = await BrokerQuoteTickSource(adapter).next_ticks(["QRA", "QRB"])

    refused = got["QRA"]
    assert isinstance(refused, QuoteRefusedError)
    assert (refused.symbol, refused.broker) == ("QRA", "zerodha")
    assert refused.reason.endswith("is from a previous day")
    assert refused.exchange_time == adapter.quotes["QRA"].exchange_time
    assert isinstance(got["QRB"], Tick)  # the other symbol is unaffected
    assert refused_counter.labels(broker="zerodha")._value.get() == before + 1
    summary = feed_health.get_quote_refusals("zerodha")
    assert (summary.count, summary.last_symbol) == (1, "QRA")
    assert summary.last_reason == refused.reason


async def test_a_single_quote_that_is_refused_raises():
    adapter = _Adapter("upstox", {"QRA": _quote("QRA", stamp=_yesterday())})

    with pytest.raises(QuoteRefusedError, match="upstox quote for QRA refused"):
        await BrokerQuoteTickSource(adapter).next_tick("QRA")


async def test_a_quote_with_no_stamp_is_published_the_guard_cannot_judge_it():
    adapter = _Adapter("upstox", {"QRA": _quote("QRA", stamp=None)})

    got = (await BrokerQuoteTickSource(adapter).next_ticks(["QRA"]))["QRA"]

    assert isinstance(got, Tick)
    assert got.exchange_time_ms is None


async def test_the_optional_age_limit_comes_from_the_setting(monkeypatch):
    adapter = _Adapter(
        "zerodha",
        {
            "QRA": _quote("QRA", stamp=datetime.now(UTC) - timedelta(seconds=45)),
            "QRB": _quote("QRB", stamp=datetime.now(UTC) - timedelta(seconds=10)),
        },
    )
    assert all(
        isinstance(t, Tick)
        for t in (await BrokerQuoteTickSource(adapter).next_ticks(SYMBOLS)).values()
    )

    monkeypatch.setenv("PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS", "30")
    get_settings.cache_clear()
    got = await BrokerQuoteTickSource(adapter).next_ticks(SYMBOLS)

    assert isinstance(got["QRA"], QuoteRefusedError) and "45s old" in got["QRA"].reason
    assert isinstance(got["QRB"], Tick)


class _Log:
    """Stands in for the module's logger: whatever configured structlog is doing in this process
    (other tests configure it and cache loggers), what is logged is recorded here."""

    def __init__(self):
        self.warnings = []

    def warning(self, event, **fields):
        self.warnings.append((event, fields))


async def test_a_refusal_is_logged_once_and_again_only_after_a_while(monkeypatch):
    from src.brokers import tick_source

    log = _Log()
    monkeypatch.setattr(tick_source, "logger", log)
    clock = {"now": 1000.0}
    adapter = _Adapter("zerodha", {"QRA": _quote("QRA", stamp=_yesterday())})
    source = BrokerQuoteTickSource(adapter, clock=lambda: clock["now"])

    await source.next_ticks(["QRA"])
    clock["now"] += REFUSAL_LOG_INTERVAL_SECONDS - 1
    await source.next_ticks(["QRA"])
    clock["now"] += 1
    await source.next_ticks(["QRA"])

    events = [fields for event, fields in log.warnings if event == "tick_source.quote_refused"]
    assert len(events) == 2
    assert events[0]["broker"] == "zerodha" and events[0]["symbol"] == "QRA"
    assert events[0]["reason"].endswith("is from a previous day")
    # Every refusal is still counted, logged or not.
    assert feed_health.get_quote_refusals("zerodha").count == 3


async def test_a_refused_symbol_is_not_handed_to_the_next_broker():
    first = _Adapter("zerodha", {"QRA": _quote("QRA", stamp=_yesterday())})
    second = _Adapter(
        "upstox", {"QRA": _quote("QRA", stamp=None), "QRB": _quote("QRB", stamp=None)}
    )
    fallback = FallbackTickSource([BrokerQuoteTickSource(first), BrokerQuoteTickSource(second)])

    got = await fallback.next_ticks(["QRA", "QRB"])

    assert isinstance(
        got["QRA"], QuoteRefusedError
    )  # not Upstox's unstamped (and no fresher) answer
    assert isinstance(got["QRB"], Tick)  # what Zerodha had no price for does fall through
    assert second.requests == [["QRB"]]
    assert fallback._served_by == "upstox"


async def test_a_broker_that_only_refused_is_not_said_to_be_serving_quotes():
    only = _Adapter("zerodha", {"QRA": _quote("QRA", stamp=_yesterday())})
    fallback = FallbackTickSource([BrokerQuoteTickSource(only)])

    got = await fallback.next_ticks(["QRA"])

    assert isinstance(got["QRA"], QuoteRefusedError)
    assert fallback._served_by is None


# ---- the publisher ----


class _Source:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.asked = []

    async def next_ticks(self, symbols):
        self.asked.append(list(symbols))
        return {s: self.outcomes[s] for s in symbols}


def _refusal(symbol, reason="the exchange stamp (2026-10-30 15:29 IST) is from a previous day"):
    return QuoteRefusedError(
        symbol=symbol,
        broker="zerodha",
        reason=reason,
        exchange_time=datetime(2026, 10, 30, 9, 59, tzinfo=UTC),
    )


async def test_a_refused_quote_is_reported_remembered_and_not_backed_off(redis):
    source = _Source({"QRA": _refusal("QRA"), "QRB": Tick("QRB", 50.0, 1, "real")})
    backoff = SymbolBackoff()

    with pytest.raises(TickPublishError) as raised:
        await publish_ticks_once(redis, source, ["QRA", "QRB"], backoff=backoff)

    assert isinstance(raised.value.failures["QRA"], QuoteRefusedError)
    assert [t.symbol for t in raised.value.published] == ["QRB"]  # the other symbol went out
    assert await get_latest_tick(redis, "QRA") is None  # nothing was published for it
    assert backoff.remaining("QRA") == 0  # asked again next cycle, not 30 s later
    with pytest.raises(TickPublishError):
        await publish_ticks_once(redis, source, ["QRA"], backoff=backoff)
    assert source.asked == [["QRA", "QRB"], ["QRA"]]


async def test_a_refusal_is_remembered_for_the_desk_for_a_short_while(redis):
    await note_quote_refusal(redis, _refusal("QRA"))

    refusal = await get_quote_refusal(redis, "QRA")
    assert (refusal.broker, refusal.reason) == (
        "zerodha",
        "the exchange stamp (2026-10-30 15:29 IST) is from a previous day",
    )
    assert refusal.exchange_time == datetime(2026, 10, 30, 9, 59, tzinfo=UTC)
    assert datetime.now(UTC) - refusal.at < timedelta(seconds=5)
    ttl = await redis.ttl(quote_refusal_key("QRA"))
    assert 0 < ttl <= QUOTE_REFUSAL_TTL_SECONDS == 30
    assert await get_quote_refusal(redis, "QRB") is None


async def test_a_refusal_with_no_stamp_to_show_reads_back_without_one(redis):
    error = QuoteRefusedError(symbol="QRA", broker="zerodha", reason="too old", exchange_time=None)

    await note_quote_refusal(redis, error)

    assert (await get_quote_refusal(redis, "QRA")).exchange_time is None
    assert json.loads(await redis.get(quote_refusal_key("QRA")))["exchange_time"] is None


async def test_an_ordinary_failure_is_still_backed_off(redis):
    source = _Source({"QRA": RuntimeError("the broker is down")})
    backoff = SymbolBackoff()

    with pytest.raises(TickPublishError):
        await publish_ticks_once(redis, source, ["QRA"], backoff=backoff)

    assert backoff.remaining("QRA") > 0
    assert await get_quote_refusal(redis, "QRA") is None  # and it is not mistaken for a refusal


def test_the_error_says_who_refused_what_and_why():
    error = _refusal("QRA", "too old")

    assert str(error) == "zerodha quote for QRA refused: too old"


def test_the_age_limit_is_off_unless_someone_sets_it():
    from src.core.config import Settings

    assert Settings.model_fields["paper_quote_max_exchange_age_seconds"].default == 0.0
