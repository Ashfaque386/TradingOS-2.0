"""Upstox adapter contract tests (Build Spec §13): every operation in the
shared BrokerAdapter surface, exercised against an injected
httpx.MockTransport -- this sandbox has no live Upstox credentials or
egress, so neither api.upstox.com nor api-sandbox.upstox.com is ever hit
here. Also verifies the sandbox/production base-URL switch (`sandbox=`)
and that `has_sandbox` is a fixed broker-level capability flag,
independent of which URL a given instance is pointed at.
"""

import json

import httpx
import pytest

from src.brokers.base import (
    BrokerAuthError,
    BrokerCredentials,
    BrokerQuoteUnavailableError,
    BrokerRequestError,
    OrderRequest,
    OrderSide,
)
from src.brokers.upstox import PRODUCTION_BASE_URL, SANDBOX_BASE_URL, UpstoxAdapter
from src.brokers.upstox_instruments import UpstoxInstrumentNotFoundError
from src.engine.risk.circuit_breaker import BrokerServerError

_CREDENTIALS = BrokerCredentials(api_key="test-key", access_token="test-token")
INFY_KEY = "NSE_EQ|INE009A01021"


class _StubInstruments:
    """Stands in for the downloaded instrument master: the real one would fetch a ~100 MB file
    from assets.upstox.com, which these tests must never do. Mirrors its one rule -- a string that
    is already a key passes through -- and records what was asked."""

    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = {"INFY": INFY_KEY, "NIFTY": "NSE_INDEX|Nifty 50"} | (mapping or {})
        self.asked: list[str] = []

    async def resolve(self, symbol: str) -> str:
        self.asked.append(symbol)
        if "|" in symbol:
            return symbol
        if symbol not in self.mapping:
            raise UpstoxInstrumentNotFoundError(symbol)
        return self.mapping[symbol]


def _adapter(handler, *, sandbox: bool = False, instruments=None) -> UpstoxAdapter:
    return UpstoxAdapter(
        _CREDENTIALS,
        sandbox=sandbox,
        transport=httpx.MockTransport(handler),
        instruments=instruments or _StubInstruments(),
    )


def test_broker_identity_and_has_sandbox_is_a_capability_flag():
    prod = _adapter(lambda request: httpx.Response(200, json={}), sandbox=False)
    sandboxed = _adapter(lambda request: httpx.Response(200, json={}), sandbox=True)
    assert prod.broker_name == sandboxed.broker_name == "upstox"
    # has_sandbox says "this broker has a sandbox capability", not "this
    # instance is pointed at it" -- true for both regardless of `sandbox=`.
    assert prod.has_sandbox is True
    assert sandboxed.has_sandbox is True
    assert prod.pointed_at_sandbox is False
    assert sandboxed.pointed_at_sandbox is True


async def test_sandbox_flag_selects_the_real_sandbox_base_url():
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": {"order_id": "OID1"}})

    order = OrderRequest(symbol="NSE_EQ|INE1", side=OrderSide.BUY, quantity=1)
    await _adapter(handler, sandbox=True).place_order(order)
    await _adapter(handler, sandbox=False).place_order(order)

    assert seen_urls[0].startswith(SANDBOX_BASE_URL)
    assert seen_urls[1].startswith(PRODUCTION_BASE_URL)


async def test_place_order_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/order/place"
        return httpx.Response(200, json={"data": {"order_id": "OID42"}})

    order = OrderRequest(symbol="NSE_EQ|INE1", side=OrderSide.BUY, quantity=10)
    result = await _adapter(handler).place_order(order)
    assert result.broker_order_id == "OID42"


async def test_place_order_5xx_raises_broker_server_error():
    adapter = _adapter(lambda request: httpx.Response(502, text="bad gateway"))
    order = OrderRequest(symbol="NSE_EQ|INE1", side=OrderSide.BUY, quantity=10)
    with pytest.raises(BrokerServerError) as exc_info:
        await adapter.place_order(order)
    assert exc_info.value.status_code == 502


async def test_place_order_4xx_raises_broker_request_error():
    adapter = _adapter(lambda request: httpx.Response(422, text="invalid instrument_token"))
    order = OrderRequest(symbol="NSE_EQ|BOGUS", side=OrderSide.BUY, quantity=10)
    with pytest.raises(BrokerRequestError) as exc_info:
        await adapter.place_order(order)
    assert exc_info.value.status_code == 422


async def test_get_order_book_parses_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "order_id": "OID1",
                        "trading_symbol": "RELIANCE",
                        "transaction_type": "SELL",
                        "quantity": 5,
                        "status": "complete",
                        "average_price": 2500.0,
                    }
                ]
            },
        )

    orders = await _adapter(handler).get_order_book()
    assert orders[0].side == OrderSide.SELL
    assert orders[0].symbol == "RELIANCE"


async def test_get_margin_parses_equity_block():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"equity": {"available_margin": 75_000.0, "used_margin": 5_000.0}}},
        )

    margin = await _adapter(handler).get_margin()
    assert margin.available_margin == 75_000.0
    assert margin.used_margin == 5_000.0


async def test_get_positions_parses_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "trading_symbol": "RELIANCE",
                        "quantity": 5,
                        "average_price": 2500.0,
                        "pnl": -50.0,
                    }
                ]
            },
        )

    positions = await _adapter(handler).get_positions()
    assert positions[0].pnl == -50.0


async def test_get_quote_parses_depth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "NSE_EQ|RELIANCE": {
                        "last_price": 2501.0,
                        "depth": {
                            "buy": [{"price": 2500.5, "quantity": 20}],
                            "sell": [{"price": 2501.5, "quantity": 30}],
                        },
                    }
                }
            },
        )

    quote = await _adapter(handler).get_quote("NSE_EQ|RELIANCE")
    assert quote.last_price == 2501.0
    assert quote.bid == 2500.5
    assert quote.ask == 2501.5


async def test_get_option_chain_is_fully_implemented_unlike_zerodha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/option/chain"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "strike_price": 2500,
                        "call_options": {
                            "instrument_key": "NSE_FO|CALL1",
                            "market_data": {"ltp": 10.5},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|PUT1",
                            "market_data": {"ltp": 8.2},
                        },
                    }
                ]
            },
        )

    entries = await _adapter(handler).get_option_chain("NSE_EQ|RELIANCE", "2025-01-30")
    assert len(entries) == 1
    assert entries[0].strike == 2500.0
    assert entries[0].call_ltp == 10.5
    assert entries[0].put_ltp == 8.2
    # Neither market_data.oi nor option_greeks was present in this mock
    # response at all -- honestly None, never a fabricated 0.0.
    assert entries[0].call_oi is None
    assert entries[0].call_iv is None


async def test_get_option_chain_extracts_real_oi_and_iv_when_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "strike_price": 2500,
                        "call_options": {
                            "instrument_key": "NSE_FO|CALL1",
                            "market_data": {"ltp": 10.5, "oi": 125000.0},
                            "option_greeks": {"iv": 18.4},
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|PUT1",
                            "market_data": {"ltp": 8.2, "oi": 98000.0},
                            "option_greeks": {"iv": 21.1},
                        },
                    }
                ]
            },
        )

    entries = await _adapter(handler).get_option_chain("NSE_EQ|RELIANCE", "2025-01-30")
    assert entries[0].call_oi == 125000.0
    assert entries[0].put_oi == 98000.0
    assert entries[0].call_iv == 18.4
    assert entries[0].put_iv == 21.1


async def test_get_expiries_deduplicates_and_sorts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/option/contract"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"expiry": "2025-02-27"},
                    {"expiry": "2025-01-30"},
                    {"expiry": "2025-01-30"},
                ]
            },
        )

    expiries = await _adapter(handler).get_expiries("NSE_EQ|RELIANCE")
    assert expiries == ["2025-01-30", "2025-02-27"]


# ---- A quote the broker did not give is an error, not a price of 0.0 ------------
# Both adapters used to answer 0.0 when the response had no entry for the symbol.
# Every consumer takes `last_price` at face value: the paper tick feed read it as
# a total price collapse and stopped out any open position, and a live order
# intent was built with a zero price and a zero position value.


@pytest.mark.parametrize(
    "body",
    [
        {"data": {}},  # no entry for the symbol at all
        {"data": {"OTHER": {"last_price": 100.0}}},  # an entry, but for another symbol
        {"data": {INFY_KEY: {"last_price": 0}}},  # the broker's own zero
        {"data": {INFY_KEY: {"last_price": None}}},
        {"data": {INFY_KEY: {}}},
        {},
    ],
)
async def test_a_missing_or_zero_quote_is_an_error_not_a_zero_price(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(BrokerQuoteUnavailableError) as caught:
        await _adapter(handler).get_quote("INFY")

    assert caught.value.status_code == 404
    assert caught.value.symbol == "INFY"
    assert "no usable quote for INFY" in str(caught.value)
    assert isinstance(caught.value, BrokerRequestError)  # an ordinary 4xx: never trips the breaker
    assert not isinstance(caught.value, BrokerAuthError)


# ---- An unreachable broker is an outage, not a bare httpx error -------------------
# A connect/read timeout used to escape as a raw httpx exception: not a
# `BrokerServerError`, so the circuit breaker never counted it and an unreachable
# broker was called on every firing for ever -- and the log line named neither the
# broker nor the call.


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("name resolution failed"),
        httpx.RemoteProtocolError("connection reset"),
    ],
)
async def test_a_network_failure_is_a_server_side_outage_that_names_the_broker_and_call(error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(BrokerServerError) as caught:
        await _adapter(handler).get_quote("INFY")

    assert caught.value.status_code == 503
    message = str(caught.value)
    assert message.startswith("upstox GET /market-quote/quotes: unreachable (")
    assert type(error).__name__ in message
    assert isinstance(caught.value.__cause__, httpx.TransportError)


async def test_a_response_that_arrives_is_not_called_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with pytest.raises(BrokerRequestError) as caught:
        await _adapter(handler).get_quote("INFY")

    assert "unreachable" not in str(caught.value)


# ---- Upstox's quotes endpoint takes `instrument_key` ----
# The adapter sent `symbol`, which the documented endpoint does not read; rows are matched
# on the `instrument_token` each carries because the response object's own key format is
# not documented.


async def test_the_quote_request_uses_the_documented_instrument_key_parameter():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"data": {"NSE_EQ|INE848E01016": {"last_price": 55.5}}})

    await _adapter(handler).get_quote("NSE_EQ|INE848E01016")

    assert seen == {"instrument_key": "NSE_EQ|INE848E01016"}


@pytest.mark.parametrize(
    "key_in_response",
    ["NSE_EQ|INE848E01016", "NSE_EQ:INE848E01016", "NSE_EQ:SOMETRADINGSYMBOL"],
)
async def test_a_row_is_found_whichever_way_the_response_keys_it(key_in_response):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    key_in_response: {
                        "instrument_token": "NSE_EQ|INE848E01016",
                        "last_price": 55.5,
                    }
                }
            },
        )

    assert (await _adapter(handler).get_quote("NSE_EQ|INE848E01016")).last_price == 55.5


async def test_a_row_for_a_different_instrument_is_not_taken():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {"NSE_EQ:OTHER": {"instrument_token": "NSE_EQ|OTHER", "last_price": 1.0}}
            },
        )

    with pytest.raises(BrokerQuoteUnavailableError):
        await _adapter(handler).get_quote("NSE_EQ|INE848E01016")


async def test_a_colon_keyed_row_without_an_instrument_token_is_still_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"NSE_EQ:INE848E01016": {"last_price": 55.5}}})

    assert (await _adapter(handler).get_quote("NSE_EQ|INE848E01016")).last_price == 55.5


# ---- A trading symbol is not an instrument key ----------------------------------
# Found against a live token: the quote request sent `instrument_key=INFY` and Upstox answered
# UDAPI1087 ("One of either symbol or instrument_key is invalid"). The mocked tests above accepted
# any string, so none of them could have seen it. A symbol is now resolved to its key first
# (src/brokers/upstox_instruments.py) -- for quotes, orders, and the option endpoints.


async def test_a_bare_symbol_is_sent_to_the_quotes_endpoint_as_its_instrument_key():
    seen = {}
    instruments = _StubInstruments()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(
            200,
            json={"data": {"NSE_EQ:INFY": {"instrument_token": INFY_KEY, "last_price": 1161.2}}},
        )

    quote = await _adapter(handler, instruments=instruments).get_quote("INFY")

    assert seen == {"instrument_key": INFY_KEY}
    assert quote.last_price == 1161.2
    assert quote.symbol == "INFY"  # callers keep the symbol they asked about


async def test_an_unknown_symbol_fails_before_any_request_is_made():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(UpstoxInstrumentNotFoundError) as caught:
        await _adapter(handler).get_quote("NOPE123")

    assert calls == []
    assert caught.value.status_code == 404
    assert isinstance(
        caught.value, BrokerRequestError
    )  # a request mistake: never trips the breaker
    assert not isinstance(caught.value, BrokerAuthError)


async def test_an_order_is_placed_on_the_instrument_key_not_the_symbol():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"order_id": "OID1"}})

    order = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=5)
    await _adapter(handler).place_order(order)

    assert sent["instrument_token"] == INFY_KEY
    assert sent["quantity"] == 5 and sent["transaction_type"] == "BUY"


async def test_an_order_for_an_unknown_symbol_is_never_sent():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"order_id": "OID1"}})

    order = OrderRequest(symbol="NOPE123", side=OrderSide.BUY, quantity=1)
    with pytest.raises(UpstoxInstrumentNotFoundError):
        await _adapter(handler).place_order(order)

    assert calls == []  # nothing reached the broker: no order under a guessed instrument


def test_the_offline_order_payload_still_carries_the_symbol_it_was_given():
    """`build_order_payload` is synchronous and offline (Shadow Mode records it): it cannot look
    the key up, so it keeps the symbol; `place_order` replaces it with the key before sending."""
    payload = _adapter(lambda request: httpx.Response(200, json={})).build_order_payload(
        OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=1)
    )

    assert payload["instrument_token"] == "INFY"


async def test_the_option_endpoints_resolve_an_index_name_to_its_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/option/contract"):
            return httpx.Response(200, json={"data": [{"expiry": "2026-10-27"}]})
        return httpx.Response(200, json={"data": []})

    adapter = _adapter(handler)
    await adapter.get_expiries("NIFTY")
    await adapter.get_option_chain("NIFTY", "2026-10-27")

    assert [params["instrument_key"] for _, params in seen] == ["NSE_INDEX|Nifty 50"] * 2


async def test_an_already_qualified_key_is_not_looked_up_or_altered():
    instruments = _StubInstruments()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"NSE_EQ|X": {"last_price": 3.0}}})

    quote = await _adapter(handler, instruments=instruments).get_quote("NSE_EQ|X")

    assert quote.last_price == 3.0
    assert instruments.asked == ["NSE_EQ|X"]
