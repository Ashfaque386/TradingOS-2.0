"""Zerodha Kite Connect adapter (Build Spec §13).

Kite Connect has no sandbox/paper environment at all -- every
authenticated call this adapter makes is a real call against production.
That is exactly why Shadow Mode (src.orchestration.shadow_mode) never
calls `place_order` for this broker: it only ever exercises
`build_order_payload`, a pure function with no network I/O, and every
`ShadowModeRun` row this adapter is associated with honestly records that
(`has_sandbox=False`, `confidence="local_payload_only"`).

Daily OAuth: Kite Connect access tokens expire once a day and are
obtained through Kite's interactive login-redirect flow, which is outside
this adapter's scope -- that is a human step, not an autonomous one. This
adapter simply uses whatever `access_token` its `BrokerCredentials`
carries at call time; a stale token surfaces as an ordinary
`BrokerRequestError(401, ...)`, the same as any other 4xx.

Kite Connect has no single "option chain" endpoint -- building one
requires downloading and filtering the full NFO instruments dump, which
is Phase 10's instrument-master-sync territory, not this adapter's job.
`get_option_chain`/`get_expiries` raise `NotImplementedError` with a
clear reason rather than silently returning an empty list, which would
read as "no options exist" instead of "not wired up yet".

This sandbox has no live Zerodha credentials and no egress to
api.kite.trade, so this adapter is exercised in tests only against an
injected `httpx.MockTransport` -- the real endpoint is never hit here or
in CI, same honest-stub posture as src.agents.llm_router's provider
clients.
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

DEFAULT_BASE_URL = "https://api.kite.trade"
DEFAULT_TIMEOUT_SECONDS = 10.0


class ZerodhaKiteAdapter:
    broker_name = "zerodha"
    has_sandbox = False

    def __init__(
        self,
        credentials: BrokerCredentials,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._credentials = credentials
        self._base_url = base_url
        # `transport` is only ever non-None in tests (httpx.MockTransport)
        # -- production code leaves it None and gets a real network
        # transport, same shape as src.agents.llm_router's clients.
        self._transport = transport
        self._timeout = timeout

    def _headers(self) -> dict:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self._credentials.api_key}:{self._credentials.access_token}",
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.request(method, path, headers=self._headers(), **kwargs)
        if resp.status_code >= 500:
            raise BrokerServerError(resp.status_code, f"zerodha {method} {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise BrokerRequestError(
                resp.status_code, f"zerodha {method} {path}: {resp.text[:200]}"
            )
        return resp.json()

    def build_order_payload(self, order: OrderRequest) -> dict:
        payload: dict = {
            "tradingsymbol": order.symbol,
            "exchange": "NSE",
            "transaction_type": "BUY" if order.side == OrderSide.BUY else "SELL",
            "quantity": order.quantity,
            "order_type": "MARKET" if order.order_type == OrderType.MARKET else "LIMIT",
            "product": order.product,
        }
        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise ValueError("a limit order requires a price")
            payload["price"] = order.price
        return payload

    async def place_order(self, order: OrderRequest) -> BrokerOrderResult:
        payload = self.build_order_payload(order)
        data = await self._request("POST", "/orders/regular", data=payload)
        order_id = str(data.get("data", {}).get("order_id", ""))
        return BrokerOrderResult(broker_order_id=order_id, status="submitted", raw=data)

    async def modify_order(
        self, broker_order_id: str, *, price: float | None = None, quantity: int | None = None
    ) -> BrokerOrderResult:
        payload: dict = {}
        if price is not None:
            payload["price"] = price
        if quantity is not None:
            payload["quantity"] = quantity
        data = await self._request("PUT", f"/orders/regular/{broker_order_id}", data=payload)
        return BrokerOrderResult(broker_order_id=broker_order_id, status="modified", raw=data)

    async def cancel_order(self, broker_order_id: str) -> BrokerOrderResult:
        data = await self._request("DELETE", f"/orders/regular/{broker_order_id}")
        return BrokerOrderResult(broker_order_id=broker_order_id, status="cancelled", raw=data)

    async def get_order_book(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/orders")
        orders = []
        for row in data.get("data", []):
            orders.append(
                BrokerOrder(
                    broker_order_id=str(row.get("order_id", "")),
                    symbol=row.get("tradingsymbol", ""),
                    side=OrderSide.BUY if row.get("transaction_type") == "BUY" else OrderSide.SELL,
                    quantity=int(row.get("quantity", 0)),
                    status=row.get("status", ""),
                    average_price=row.get("average_price"),
                )
            )
        return orders

    async def get_margin(self) -> MarginInfo:
        data = await self._request("GET", "/user/margins")
        equity = data.get("data", {}).get("equity", {})
        return MarginInfo(
            available_margin=float(equity.get("available", {}).get("cash", 0.0)),
            used_margin=float(equity.get("utilised", {}).get("debits", 0.0)),
            raw=data,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/portfolio/positions")
        positions = []
        for row in data.get("data", {}).get("net", []):
            positions.append(
                BrokerPosition(
                    symbol=row.get("tradingsymbol", ""),
                    quantity=int(row.get("quantity", 0)),
                    average_price=float(row.get("average_price", 0.0)),
                    pnl=float(row.get("pnl", 0.0)),
                )
            )
        return positions

    async def get_quote(self, symbol: str) -> BrokerQuote:
        data = await self._request("GET", "/quote", params={"i": symbol})
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
        raise NotImplementedError(
            "zerodha: Kite Connect has no bulk option-chain endpoint (unlike "
            "Upstox's /option/chain). Building this would require downloading "
            "Kite Connect's own NFO instrument dump (GET /instruments/NFO) to "
            "resolve per-strike tradingsymbols for this underlying/expiry, then "
            "batch-quoting them via GET /quote -- real open interest is present "
            "in that quote response, but Kite Connect has no options-greeks "
            "field anywhere, so implied volatility would stay unavailable for "
            "this broker regardless (Phase 17 real-world testing pass; see "
            "docs/phase17-realworld-testing.md)"
        )

    async def get_expiries(self, underlying: str) -> list[str]:
        raise NotImplementedError(
            "zerodha: Kite Connect has no dedicated option-expiries endpoint; "
            "expiries would need to be derived from the same NFO instrument "
            "dump get_option_chain would need (Phase 17 real-world testing "
            "pass; see docs/phase17-realworld-testing.md)"
        )
