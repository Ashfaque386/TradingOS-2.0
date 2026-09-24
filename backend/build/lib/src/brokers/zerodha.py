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

Kite Connect has no single "option chain" endpoint. `get_option_chain`/
`get_expiries` build one the way Kite Connect's own real API actually
supports it (Phase 17 real-world testing pass, following up on the
originally-deferred gap documented in docs/phase17-realworld-testing.md):
download and filter the real NFO instruments dump (`GET
/instruments/NFO`, a CSV of every tradingsymbol/strike/expiry Kite
Connect lists) to resolve per-strike tradingsymbols for the requested
underlying/expiry, then batch-quote them via `GET /quote` (which accepts
multiple `i=` params and, for F&O instruments, returns a real `oi`
field). Kite Connect has no options-greeks field anywhere in either
response, so `call_iv`/`put_iv` stay genuinely `None` for this broker
always -- not a temporary gap, a permanent one, unlike Upstox (whose
`get_option_chain` does report real IV). The NFO dump is large (every
F&O instrument Kite Connect lists, not just one underlying) and doesn't
change intraday, so it's cached at module level for
`_NFO_INSTRUMENTS_CACHE_TTL_SECONDS` -- same "single uvicorn process, no
`--workers N`, a module-level singleton genuinely is the one shared
instance every caller sees" reasoning as `src.brokers.breaker_registry`.
The CSV column layout and quote response shape below follow Kite
Connect's own publicly documented API from training knowledge; this
sandbox has no live Zerodha credentials and no egress to api.kite.trade
to verify them against a real response, the same caveat Follow-up D's
original Upstox implementation carried and later confirmed correct via
a genuine (sandbox-blocked) live attempt.

This sandbox has no live Zerodha credentials and no egress to
api.kite.trade, so this adapter is exercised in tests only against an
injected `httpx.MockTransport` -- the real endpoint is never hit here or
in CI, same honest-stub posture as src.agents.llm_router's provider
clients.
"""

import csv
import io
import time
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

# 15 minutes -- the NFO instrument dump (strikes/expiries/tradingsymbols)
# doesn't change intraday, so re-downloading the full CSV on every single
# option-chain/expiries call would be wasteful. Module-level, not
# per-instance: a fresh ZerodhaKiteAdapter is constructed on every call to
# src.brokers.factory.build_configured_adapter, so an instance attribute
# would never actually get reused. The cache holds public exchange data
# (not personalized to a specific API key), so it's safe to share across
# whichever credentials happen to be configured for this single-operator
# deployment.
_NFO_INSTRUMENTS_CACHE_TTL_SECONDS = 900
_nfo_instruments_cache: list[dict] | None = None
_nfo_instruments_cache_at: float = 0.0


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

    async def _get_nfo_instruments(self) -> list[dict]:
        global _nfo_instruments_cache, _nfo_instruments_cache_at
        now = time.monotonic()
        if (
            _nfo_instruments_cache is not None
            and (now - _nfo_instruments_cache_at) < _NFO_INSTRUMENTS_CACHE_TTL_SECONDS
        ):
            return _nfo_instruments_cache

        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            resp = await client.get("/instruments/NFO", headers=self._headers())
        if resp.status_code >= 500:
            raise BrokerServerError(
                resp.status_code, f"zerodha GET /instruments/NFO: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise BrokerRequestError(
                resp.status_code, f"zerodha GET /instruments/NFO: {resp.text[:200]}"
            )

        rows = list(csv.DictReader(io.StringIO(resp.text)))
        _nfo_instruments_cache = rows
        _nfo_instruments_cache_at = now
        return rows

    async def _batch_quote(self, tradingsymbols: list[str]) -> dict[str, dict]:
        """Kite Connect's real `/quote` endpoint accepts multiple `i=`
        params in one call (documented cap: 500 instruments) -- an option
        chain's CE+PE legs across every strike for one expiry is well
        within that, so no chunking is needed for this call site."""
        if not tradingsymbols:
            return {}
        params = [("i", f"NFO:{symbol}") for symbol in tradingsymbols]
        data = await self._request("GET", "/quote", params=params)
        return data.get("data", {})

    async def get_option_chain(self, underlying: str, expiry: str) -> list[OptionChainEntry]:
        """Real Kite Connect data end to end: strikes/tradingsymbols
        resolved from the real NFO instrument dump, LTP/OI from a real
        batch `/quote` call. `call_iv`/`put_iv` are always `None` for this
        broker -- not a gap in this implementation, a genuine absence in
        Kite Connect's own API (no options-greeks field anywhere), unlike
        Upstox's `get_option_chain`. `GET /api/v1/market-data/option-chain`
        (src.api.routes.market_data) fills that gap at the API layer with
        a Black-Scholes-computed estimate (src.engine.options_pricing)
        whenever a spot price and forward-looking expiry are available --
        this adapter itself stays broker-honest and never invents one."""
        instruments = await self._get_nfo_instruments()
        strikes: dict[float, dict[str, dict]] = {}
        for row in instruments:
            if row.get("name") != underlying or row.get("expiry") != expiry:
                continue
            option_type = row.get("instrument_type")
            if option_type not in ("CE", "PE"):
                continue
            try:
                strike = float(row.get("strike") or 0.0)
            except ValueError:
                continue
            strikes.setdefault(strike, {})[option_type] = row

        if not strikes:
            return []

        tradingsymbols = [
            row["tradingsymbol"] for legs in strikes.values() for row in legs.values()
        ]
        quotes = await self._batch_quote(tradingsymbols)

        entries = []
        for strike in sorted(strikes):
            legs = strikes[strike]
            call_row = legs.get("CE")
            put_row = legs.get("PE")
            call_symbol = call_row["tradingsymbol"] if call_row else None
            put_symbol = put_row["tradingsymbol"] if put_row else None
            call_quote = quotes.get(f"NFO:{call_symbol}") if call_symbol else None
            put_quote = quotes.get(f"NFO:{put_symbol}") if put_symbol else None
            entries.append(
                OptionChainEntry(
                    strike=strike,
                    call_symbol=call_symbol,
                    put_symbol=put_symbol,
                    call_ltp=call_quote.get("last_price") if call_quote else None,
                    put_ltp=put_quote.get("last_price") if put_quote else None,
                    call_oi=call_quote.get("oi") if call_quote else None,
                    put_oi=put_quote.get("oi") if put_quote else None,
                    call_iv=None,
                    put_iv=None,
                )
            )
        return entries

    async def get_expiries(self, underlying: str) -> list[str]:
        """Derived from the same real NFO instrument dump
        `get_option_chain` uses -- Kite Connect has no dedicated
        expiries endpoint of its own."""
        instruments = await self._get_nfo_instruments()
        expiries = {
            row["expiry"]
            for row in instruments
            if row.get("name") == underlying
            and row.get("instrument_type") in ("CE", "PE")
            and row.get("expiry")
        }
        return sorted(expiries)
