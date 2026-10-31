"""The measurements that say whether polling is fast enough (phase 7): how long a batched quote
call takes, and how old the exchange says a quote was when it arrived."""

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY

import src.brokers.tick_source as tick_source_module
from src.brokers.base import BrokerQuote, quote_from_row
from src.brokers.tick_source import BrokerQuoteTickSource
from src.engine.risk.circuit_breaker import BrokerServerError
from tests.clock import freeze_clock


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _quote(symbol: str, exchange_time: datetime | None) -> BrokerQuote:
    return BrokerQuote(
        symbol=symbol,
        last_price=100.0,
        bid=None,
        ask=None,
        timestamp=datetime.now(UTC),
        exchange_time=exchange_time,
    )


class _Broker:
    def __init__(self, name: str, quotes: dict[str, BrokerQuote], error: Exception | None = None):
        self.broker_name = name
        self.quotes = quotes
        self.error = error

    async def get_quotes(self, symbols):
        if self.error is not None:
            raise self.error
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    async def get_quote(self, symbol):
        return (await self.get_quotes([symbol]))[symbol]


# ---- reading the exchange's own stamp ----


def test_kites_quote_timestamp_is_read_as_nse_time():
    quote = quote_from_row(
        "INFY", {"last_price": 1500.0, "timestamp": "2026-10-19 10:15:30"}, broker="zerodha"
    )
    assert quote.exchange_time == datetime(2026, 10, 19, 4, 45, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw", [None, "", "not a time", 1697624972130, "2026-10-19T10:15:30+05:30"]
)
def test_an_unreadable_exchange_stamp_is_none_not_a_guess(raw):
    row = {"last_price": 1500.0, "timestamp": raw}
    assert quote_from_row("INFY", row, broker="zerodha").exchange_time is None


def test_upstox_has_no_exchange_stamp_until_a_real_payload_settles_its_fields():
    row = {
        "last_price": 1500.0,
        "timestamp": "2026-10-19 10:15:30",  # readable as a Kite stamp, and still not trusted
        "last_trade_time": "1697624972130",
    }
    assert quote_from_row("INFY", row, broker="upstox").exchange_time is None


# ---- the quote call ----


async def test_a_quote_call_is_timed_by_broker_and_outcome():
    ok = {"broker": "zerodha", "outcome": "ok"}
    err = {"broker": "zerodha", "outcome": "error"}
    ok_before = _sample("tradingos_paper_quote_fetch_seconds_count", ok)
    err_before = _sample("tradingos_paper_quote_fetch_seconds_count", err)

    good = BrokerQuoteTickSource(_Broker("zerodha", {"INFY": _quote("INFY", None)}))
    await good.next_ticks(["INFY"])
    assert _sample("tradingos_paper_quote_fetch_seconds_count", ok) == ok_before + 1

    bad = BrokerQuoteTickSource(_Broker("zerodha", {}, error=BrokerServerError(503, "down")))
    with pytest.raises(BrokerServerError):
        await bad.next_ticks(["INFY"])
    assert _sample("tradingos_paper_quote_fetch_seconds_count", err) == err_before + 1
    assert _sample("tradingos_paper_quote_fetch_seconds_count", ok) == ok_before + 1


# ---- the exchange lag ----


async def test_a_quotes_exchange_lag_is_recorded_when_the_broker_sends_one(monkeypatch):
    labels = {"broker": "zerodha"}
    count = "tradingos_paper_quote_exchange_lag_seconds_count"
    total = "tradingos_paper_quote_exchange_lag_seconds_sum"
    before_count, before_total = _sample(count, labels), _sample(total, labels)
    now = datetime(2026, 3, 10, 5, 0, tzinfo=UTC)
    freeze_clock(monkeypatch, tick_source_module, now)  # so "7 seconds old" is exactly 7
    stamp = now - timedelta(seconds=7)
    await BrokerQuoteTickSource(_Broker("zerodha", {"INFY": _quote("INFY", stamp)})).next_ticks(
        ["INFY"]
    )
    assert _sample(count, labels) == before_count + 1
    assert _sample(total, labels) - before_total == pytest.approx(7.0, abs=1e-6)


async def test_a_missing_or_future_exchange_stamp_is_counted_not_measured():
    count = "tradingos_paper_quote_exchange_lag_seconds_count"
    unusable = "tradingos_paper_quote_exchange_lag_unusable_total"
    broker = {"broker": "upstox"}
    absent, future = {**broker, "reason": "absent"}, {**broker, "reason": "future"}
    before = (_sample(count, broker), _sample(unusable, absent), _sample(unusable, future))

    source = BrokerQuoteTickSource(
        _Broker(
            "upstox",
            {
                "A": _quote("A", None),
                "B": _quote("B", datetime.now(UTC) + timedelta(seconds=60)),
            },
        )
    )
    await source.next_ticks(["A", "B"])

    assert _sample(count, broker) == before[0]
    assert _sample(unusable, absent) == before[1] + 1
    assert _sample(unusable, future) == before[2] + 1


async def test_a_stamp_a_second_ahead_is_clock_skew_and_reads_as_zero_lag():
    labels = {"broker": "zerodha"}
    count = "tradingos_paper_quote_exchange_lag_seconds_count"
    total = "tradingos_paper_quote_exchange_lag_seconds_sum"
    before, before_total = _sample(count, labels), _sample(total, labels)
    stamp = datetime.now(UTC) + timedelta(seconds=1)
    await BrokerQuoteTickSource(_Broker("zerodha", {"X": _quote("X", stamp)})).next_ticks(["X"])
    assert _sample(count, labels) == before + 1
    assert _sample(total, labels) >= before_total  # never a negative lag
