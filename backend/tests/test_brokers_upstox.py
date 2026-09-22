"""Upstox adapter contract tests (Build Spec §13): every operation in the
shared BrokerAdapter surface, exercised against an injected
httpx.MockTransport -- this sandbox has no live Upstox credentials or
egress, so neither api.upstox.com nor api-sandbox.upstox.com is ever hit
here. Also verifies the sandbox/production base-URL switch (`sandbox=`)
and that `has_sandbox` is a fixed broker-level capability flag,
independent of which URL a given instance is pointed at.
"""

import httpx
import pytest

from src.brokers.base import BrokerCredentials, BrokerRequestError, OrderRequest, OrderSide
from src.brokers.upstox import PRODUCTION_BASE_URL, SANDBOX_BASE_URL, UpstoxAdapter
from src.engine.risk.circuit_breaker import BrokerServerError

_CREDENTIALS = BrokerCredentials(api_key="test-key", access_token="test-token")


def _adapter(handler, *, sandbox: bool = False) -> UpstoxAdapter:
    return UpstoxAdapter(_CREDENTIALS, sandbox=sandbox, transport=httpx.MockTransport(handler))


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
    order = OrderRequest(symbol="BOGUS", side=OrderSide.BUY, quantity=10)
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
