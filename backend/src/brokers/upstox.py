"""Upstox (Trading V2) adapter (Build Spec §13).

Upstox publishes a real Sandbox environment at a separate base URL
(`api-sandbox.upstox.com`) that accepts genuine order-placement calls
against simulated fills -- unlike Zerodha, a Shadow Mode dry run against
Upstox is a real network round-trip, not just local payload
construction, and `ShadowModeRun` rows for this broker record that
honestly (`has_sandbox=True`, `confidence="real_sandbox_dry_run"`).

`has_sandbox` is a broker-level capability flag (True always, for this
class) and is independent of which base URL *this particular instance*
happens to be pointed at -- a caller building an adapter for live trading
constructs one with `sandbox=False`; Shadow Mode
(src.orchestration.shadow_mode) is responsible for always constructing
its own instance with `sandbox=True` so a dry run can never accidentally
reach production, regardless of what `has_sandbox` says.

Unlike Zerodha, Upstox's v2 API has dedicated option-chain and
option-contract endpoints, so `get_option_chain`/`get_expiries` are fully
implemented here rather than deferred -- one honest, broker-by-broker
difference among several this integration layer surfaces rather than
papering over (see also the has_sandbox distinction above).

This sandbox has no live Upstox credentials and no egress to
api.upstox.com/api-sandbox.upstox.com, so this adapter is exercised in
tests only against an injected `httpx.MockTransport` -- the real (or
sandbox) endpoint is never hit here or in CI. The JSON response shapes
below follow Upstox's publicly documented v2 API envelope from training
knowledge; this sandbox cannot verify them against a live call.
"""

from datetime import UTC, datetime

import httpx

from src.brokers.base import (
    BrokerCredentials,
    BrokerOrder,
    BrokerOrderResult,
    BrokerPosition,
    BrokerQuote,
    BrokerRequestError,
    MarginInfo,
    OptionChainEntry,
    OrderRequest,
    OrderSide,
    OrderType,
)
from src.engine.risk.circuit_breaker import BrokerServerError

PRODUCTION_BASE_URL = "https://api.upstox.com/v2"
SANDBOX_BASE_URL = "https://api-sandbox.upstox.com/v2"
DEFAULT_TIMEOUT_SECONDS = 10.0


class UpstoxAdapter:
    broker_name = "upstox"
    has_sandbox = True

    def __init__(
        self,
        credentials: BrokerCredentials,
        *,
        sandbox: bool = False,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._credentials = credentials
        self._base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL
        self.pointed_at_sandbox = sandbox
        self._transport = transport
        self._timeout = timeout

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.request(method, path, headers=self._headers(), **kwargs)
        if resp.status_code >= 500:
            raise BrokerServerError(resp.status_code, f"upstox {method} {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise BrokerRequestError(resp.status_code, f"upstox {method} {path}: {resp.text[:200]}")
        return resp.json()

    def build_order_payload(self, order: OrderRequest) -> dict:
        payload: dict = {
            "instrument_token": order.symbol,
            "quantity": order.quantity,
            "product": order.product,
            "validity": "DAY",
            "order_type": "MARKET" if order.order_type == OrderType.MARKET else "LIMIT",
            "transaction_type": "BUY" if order.side == OrderSide.BUY else "SELL",
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise ValueError("a limit order requires a price")
            payload["price"] = order.price
        else:
            payload["price"] = 0
        return payload

    async def place_order(self, order: OrderRequest) -> BrokerOrderResult:
        payload = self.build_order_payload(order)
        data = await self._request("POST", "/order/place", json=payload)
        order_id = str(data.get("data", {}).get("order_id", ""))
        return BrokerOrderResult(broker_order_id=order_id, status="submitted", raw=data)

    async def modify_order(
        self, broker_order_id: str, *, price: float | None = None, quantity: int | None = None
    ) -> BrokerOrderResult:
        payload: dict = {"order_id": broker_order_id}
        if price is not None:
            payload["price"] = price
        if quantity is not None:
            payload["quantity"] = quantity
        data = await self._request("PUT", "/order/modify", json=payload)
        return BrokerOrderResult(broker_order_id=broker_order_id, status="modified", raw=data)

    async def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        data = await self._request("DELETE", "/order/cancel", params={"order_id": broker_order_id})
        return BrokerOrderResult(broker_order_id=broker_order_id, status="cancelled", raw=data)

    async def get_order_book(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/order/retrieve-all")
        orders = []
        for row in data.get("data", []):
            orders.append(
                BrokerOrder(
                    broker_order_id=str(row.get("order_id", "")),
                    symbol=row.get("trading_symbol", ""),
                    side=OrderSide.BUY if row.get("transaction_type") == "BUY" else OrderSide.SELL,
                    quantity=int(row.get("quantity", 0)),
                    status=row.get("status", ""),
                    average_price=row.get("average_price"),
                )
            )
        return orders

    async def get_margin(self) -> MarginInfo:
        data = await self._request("GET", "/user/get-funds-and-margin")
        equity = data.get("data", {}).get("equity", {})
        return MarginInfo(
            available_margin=float(equity.get("available_margin", 0.0)),
            used_margin=float(equity.get("used_margin", 0.0)),
            raw=data,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/portfolio/short-term-positions")
        positions = []
        for row in data.get("data", []):
            positions.append(
                BrokerPosition(
                    symbol=row.get("trading_symbol", ""),
                    quantity=int(row.get("quantity", 0)),
                    average_price=float(row.get("average_price", 0.0)),
                    pnl=float(row.get("pnl", 0.0)),
                )
            )
        return positions

    async def get_quote(self, symbol: str) -> BrokerQuote:
        data = await self._request("GET", "/market-quote/quotes", params={"symbol": symbol})
        row = data.get("data", {}).get(symbol, {})
        depth = row.get("depth", {})
        buy_levels = depth.get("buy") or []
        sell_levels = depth.get("sell") or []
        return BrokerQuote(
            symbol=symbol,
            last_price=float(row.get("last_price", 0.0)),
            bid=buy_levels[0].get("price") if buy_levels else None,
            ask=sell_levels[0].get("price") if sell_levels else None,
            timestamp=datetime.now(UTC),
        )

    async def get_option_chain(self, underlying: str, expiry: str) -> list[OptionChainEntry]:
        data = await self._request(
            "GET", "/option/chain", params={"instrument_key": underlying, "expiry_date": expiry}
        )
        entries = []
        for row in data.get("data", []):
            call = row.get("call_options") or {}
            put = row.get("put_options") or {}
            call_market = call.get("market_data") or {}
            put_market = put.get("market_data") or {}
            call_greeks = call.get("option_greeks") or {}
            put_greeks = put.get("option_greeks") or {}
            entries.append(
                OptionChainEntry(
                    strike=float(row.get("strike_price", 0.0)),
                    call_symbol=call.get("instrument_key"),
                    put_symbol=put.get("instrument_key"),
                    call_ltp=call_market.get("ltp"),
                    put_ltp=put_market.get("ltp"),
                    call_oi=call_market.get("oi"),
                    put_oi=put_market.get("oi"),
                    call_iv=call_greeks.get("iv"),
                    put_iv=put_greeks.get("iv"),
                )
            )
        return entries

    async def get_expiries(self, underlying: str) -> list[str]:
        data = await self._request("GET", "/option/contract", params={"instrument_key": underlying})
        expiries: list[str] = []
        for row in data.get("data", []):
            expiry = row.get("expiry")
            if expiry and expiry not in expiries:
                expiries.append(expiry)
        return sorted(expiries)
