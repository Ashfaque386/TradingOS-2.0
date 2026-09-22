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


_NFO_CSV = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange\n"
    "1001,1,NIFTY25JAN23500CE,NIFTY,0,2025-01-30,23500.000000,0.05,50,CE,NFO-OPT,NFO\n"
    "1002,1,NIFTY25JAN23500PE,NIFTY,0,2025-01-30,23500.000000,0.05,50,PE,NFO-OPT,NFO\n"
    "1003,1,NIFTY25JAN24000CE,NIFTY,0,2025-01-30,24000.000000,0.05,50,CE,NFO-OPT,NFO\n"
    "1004,1,NIFTY25JAN24000PE,NIFTY,0,2025-01-30,24000.000000,0.05,50,PE,NFO-OPT,NFO\n"
    "1005,1,NIFTY25FEB24000CE,NIFTY,0,2025-02-27,24000.000000,0.05,50,CE,NFO-OPT,NFO\n"
    "1006,1,BANKNIFTY25JAN51000CE,BANKNIFTY,0,2025-01-30,51000.000000,0.05,15,CE,NFO-OPT,NFO\n"
    "1007,1,NIFTY25JANFUT,NIFTY,0,2025-01-30,0.000000,0.05,50,FUT,NFO-FUT,NFO\n"
)


@pytest.fixture(autouse=True)
def _reset_nfo_instrument_cache():
    """The NFO instrument dump is cached at module level (deliberately,
    per src.brokers.zerodha's own docstring -- a fresh adapter is built
    per call, so an instance attribute would never be reused). Reset it
    around every test so each test's own mock transport is genuinely
    exercised rather than silently reusing another test's cached data."""
    import src.brokers.zerodha as zerodha_module

    zerodha_module._nfo_instruments_cache = None
    zerodha_module._nfo_instruments_cache_at = 0.0
    yield
    zerodha_module._nfo_instruments_cache = None
    zerodha_module._nfo_instruments_cache_at = 0.0


def _nfo_and_quote_handler(quote_data: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/instruments/NFO":
            return httpx.Response(200, text=_NFO_CSV)
        if request.url.path == "/quote":
            requested = request.url.params.get_list("i")
            data = {key: value for key, value in quote_data.items() if key in requested}
            return httpx.Response(200, json={"status": "success", "data": data})
        raise AssertionError(f"unexpected request path {request.url.path}")

    return handler


async def test_get_option_chain_returns_real_ltp_and_oi_never_iv():
    quote_data = {
        "NFO:NIFTY25JAN23500CE": {"last_price": 340.0, "oi": 12000},
        "NFO:NIFTY25JAN23500PE": {"last_price": 40.0, "oi": 22000},
        "NFO:NIFTY25JAN24000CE": {"last_price": 120.5, "oi": 45000},
        "NFO:NIFTY25JAN24000PE": {"last_price": 95.0, "oi": 38000},
    }
    adapter = _adapter(_nfo_and_quote_handler(quote_data))
    entries = await adapter.get_option_chain("NIFTY", "2025-01-30")

    assert [e.strike for e in entries] == [23500.0, 24000.0]
    entry = next(e for e in entries if e.strike == 24000.0)
    assert entry.call_symbol == "NIFTY25JAN24000CE"
    assert entry.put_symbol == "NIFTY25JAN24000PE"
    assert entry.call_ltp == 120.5
    assert entry.put_ltp == 95.0
    assert entry.call_oi == 45000
    assert entry.put_oi == 38000
    # Kite Connect has no options-greeks field anywhere -- always None,
    # not a temporary gap.
    assert entry.call_iv is None
    assert entry.put_iv is None


async def test_get_option_chain_returns_empty_for_an_unmatched_underlying_or_expiry():
    adapter = _adapter(_nfo_and_quote_handler({}))
    assert await adapter.get_option_chain("RELIANCE", "2025-01-30") == []
    assert await adapter.get_option_chain("NIFTY", "2099-12-31") == []


async def test_get_option_chain_handles_a_strike_with_only_one_leg_listed():
    csv_text = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
        "tick_size,lot_size,instrument_type,segment,exchange\n"
        "1,1,NIFTY25JAN23000CE,NIFTY,0,2025-01-30,23000.000000,0.05,50,CE,NFO-OPT,NFO\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/instruments/NFO":
            return httpx.Response(200, text=csv_text)
        if request.url.path == "/quote":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"NFO:NIFTY25JAN23000CE": {"last_price": 500.0, "oi": 100}},
                },
            )
        raise AssertionError(f"unexpected request path {request.url.path}")

    adapter = _adapter(handler)
    entries = await adapter.get_option_chain("NIFTY", "2025-01-30")
    assert len(entries) == 1
    assert entries[0].put_symbol is None
    assert entries[0].put_ltp is None
    assert entries[0].put_oi is None


async def test_get_expiries_returns_sorted_unique_expiries_for_the_underlying():
    adapter = _adapter(_nfo_and_quote_handler({}))
    expiries = await adapter.get_expiries("NIFTY")
    assert expiries == ["2025-01-30", "2025-02-27"]


async def test_get_expiries_excludes_futures_and_other_underlyings():
    adapter = _adapter(_nfo_and_quote_handler({}))
    expiries = await adapter.get_expiries("BANKNIFTY")
    assert expiries == ["2025-01-30"]


async def test_nfo_instrument_dump_is_cached_across_calls_on_the_same_process():
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        if request.url.path == "/instruments/NFO":
            call_count += 1
            return httpx.Response(200, text=_NFO_CSV)
        if request.url.path == "/quote":
            return httpx.Response(200, json={"status": "success", "data": {}})
        raise AssertionError(f"unexpected request path {request.url.path}")

    adapter = _adapter(handler)
    await adapter.get_expiries("NIFTY")
    await adapter.get_expiries("NIFTY")
    await adapter.get_option_chain("NIFTY", "2025-01-30")
    assert call_count == 1


async def test_option_chain_surfaces_a_real_server_error_from_the_instrument_dump():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kite is down")

    adapter = _adapter(handler)
    with pytest.raises(BrokerServerError):
        await adapter.get_option_chain("NIFTY", "2025-01-30")
