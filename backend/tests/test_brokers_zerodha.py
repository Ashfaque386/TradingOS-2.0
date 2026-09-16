"""Zerodha Kite Connect adapter contract tests (Build Spec §13): every
operation in the shared BrokerAdapter surface, exercised against an
injected httpx.MockTransport -- this sandbox has no live Zerodha
credentials or egress, so the real api.kite.trade endpoint is never hit
here.
"""

import httpx
import pytest

from src.brokers.base import (
    BrokerCredentials,
    BrokerRequestError,
    OrderRequest,
    OrderSide,
    OrderType,
)
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.risk.circuit_breaker import BrokerServerError

_CREDENTIALS = BrokerCredentials(api_key="test-key", access_token="test-token")


def _adapter(handler) -> ZerodhaKiteAdapter:
    return ZerodhaKiteAdapter(_CREDENTIALS, transport=httpx.MockTransport(handler))


def test_broker_identity():
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    assert adapter.broker_name == "zerodha"
    assert adapter.has_sandbox is False


def test_build_order_payload_is_pure_and_market_by_default():
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    order = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=10)
    payload = adapter.build_order_payload(order)
    assert payload["tradingsymbol"] == "INFY"
    assert payload["transaction_type"] == "BUY"
    assert payload["order_type"] == "MARKET"
    assert "price" not in payload


def test_build_order_payload_limit_requires_price():
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    order = OrderRequest(symbol="INFY", side=OrderSide.SELL, quantity=5, order_type=OrderType.LIMIT)
    with pytest.raises(ValueError):
        adapter.build_order_payload(order)


async def test_place_order_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/orders/regular"
        return httpx.Response(200, json={"data": {"order_id": "OID123"}})

    adapter = _adapter(handler)
    order = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=10)
    result = await adapter.place_order(order)
    assert result.broker_order_id == "OID123"
    assert result.status == "submitted"


async def test_place_order_5xx_raises_broker_server_error():
    adapter = _adapter(lambda request: httpx.Response(503, text="upstream broker outage"))
    order = OrderRequest(symbol="INFY", side=OrderSide.BUY, quantity=10)
    with pytest.raises(BrokerServerError) as exc_info:
        await adapter.place_order(order)
    assert exc_info.value.status_code == 503


async def test_place_order_4xx_raises_broker_request_error_not_server_error():
    adapter = _adapter(lambda request: httpx.Response(400, text="bad tradingsymbol"))
    order = OrderRequest(symbol="BOGUS", side=OrderSide.BUY, quantity=10)
    with pytest.raises(BrokerRequestError) as exc_info:
        await adapter.place_order(order)
    assert exc_info.value.status_code == 400


async def test_modify_and_cancel_order():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert request.url.path == "/orders/regular/OID123"
            return httpx.Response(200, json={"data": {"order_id": "OID123"}})
        assert request.method == "DELETE"
        return httpx.Response(200, json={"data": {"order_id": "OID123"}})

    adapter = _adapter(handler)
    modified = await adapter.modify_order("OID123", price=101.5)
    assert modified.status == "modified"
    cancelled = await adapter.cancel_order("OID123")
    assert cancelled.status == "cancelled"


async def test_get_order_book_parses_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "order_id": "OID1",
                        "tradingsymbol": "INFY",
                        "transaction_type": "BUY",
                        "quantity": 10,
                        "status": "COMPLETE",
                        "average_price": 1500.5,
                    }
                ]
            },
        )

    orders = await _adapter(handler).get_order_book()
    assert len(orders) == 1
    assert orders[0].symbol == "INFY"
    assert orders[0].side == OrderSide.BUY


async def test_get_margin_parses_equity_block():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "equity": {
                        "available": {"cash": 50_000.0},
                        "utilised": {"debits": 12_000.0},
                    }
                }
            },
        )

    margin = await _adapter(handler).get_margin()
    assert margin.available_margin == 50_000.0
    assert margin.used_margin == 12_000.0


async def test_get_positions_parses_net_book():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "net": [
                        {
                            "tradingsymbol": "INFY",
                            "quantity": 10,
                            "average_price": 1500.0,
                            "pnl": 250.0,
                        }
                    ]
                }
            },
        )

    positions = await _adapter(handler).get_positions()
    assert len(positions) == 1
    assert positions[0].pnl == 250.0


async def test_get_quote_parses_depth():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["i"] == "INFY"
        return httpx.Response(
            200,
            json={
                "data": {
                    "INFY": {
                        "last_price": 1502.3,
                        "depth": {
                            "buy": [{"price": 1502.0, "quantity": 100}],
                            "sell": [{"price": 1502.5, "quantity": 50}],
                        },
                    }
                }
            },
        )

    quote = await _adapter(handler).get_quote("INFY")
    assert quote.last_price == 1502.3
    assert quote.bid == 1502.0
    assert quote.ask == 1502.5


async def test_option_chain_and_expiries_are_honestly_not_implemented():
    """Kite Connect has no dedicated option-chain endpoint -- this must
    never silently return an empty list, which would read as "no options
    exist" instead of "not wired up yet"."""
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    with pytest.raises(NotImplementedError):
        await adapter.get_option_chain("NIFTY", "2025-01-30")
    with pytest.raises(NotImplementedError):
        await adapter.get_expiries("NIFTY")
