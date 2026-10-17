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
`BrokerAuthError` (a `BrokerRequestError`; Kite answers a bad token with a 403
`TokenException`, others with a 401), the same family as any other 4xx.

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
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, cast

import httpx

from src.brokers.base import (
    QUOTE_BATCH_SIZE,
    SEARCHABLE_INDICES,
    BrokerCredentials,
    BrokerOrder,
    BrokerOrderResult,
    BrokerPosition,
    BrokerQuote,
    BrokerQuoteUnavailableError,
    Candle,
    CandleInterval,
    FuturesContract,
    InstrumentNotFoundError,
    MarginInfo,
    OptionChainEntry,
    OptionContract,
    OrderRequest,
    OrderSide,
    OrderType,
    SymbolHit,
    broker_request_error,
    broker_unreachable,
    futures_symbol,
    option_symbol,
    parse_candle,
    parse_option_symbol,
    quote_from_row,
    rank_symbol_hits,
)
from src.engine.risk.circuit_breaker import BrokerServerError

DEFAULT_BASE_URL = "https://api.kite.trade"
DEFAULT_TIMEOUT_SECONDS = 10.0
# Kite's /quote wants `exchange:tradingsymbol` ("NSE:INFY") and keys its response the
# same way; a bare tradingsymbol is read as NSE, the exchange orders are placed on.
DEFAULT_QUOTE_EXCHANGE = "NSE"
# The common index short names are not Kite tradingsymbols: the NIFTY 50 index is `NSE:NIFTY 50`,
# and `NSE:NIFTY` finds nothing (seen against a live token: NIFTY and BANKNIFTY quotes failed while
# every equity worked, which left the option chain without its spot price and ATM strike). Keys
# checked live. Anything not listed -- or already `exchange:symbol` -- is passed through as is.
INDEX_QUOTE_KEYS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX": "BSE:SENSEX",
    "INDIAVIX": "NSE:INDIA VIX",
}

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
_nfo_instruments_cache: list[dict[str, Any]] | None = None
_nfo_instruments_cache_at: float = 0.0
# The option index derived from that dump: (the rows it was built from, the index).
# The NSE cash equities among the tokens above (the rest are indices), refreshed with them.
_nse_equity_symbols: frozenset[str] = frozenset()
_nfo_option_index: tuple[list[dict[str, Any]], dict[tuple[str, date, float, str], Any]] | None = (
    None
)

# The NSE dump (equities and indices, ~10k rows) is the symbol -> instrument_token map the
# historical-data endpoint needs. Public exchange data like the NFO dump, so shared the same way;
# it changes at most daily, hence the long lifetime.
_NSE_INSTRUMENTS_CACHE_TTL_SECONDS = 6 * 3600
_nse_token_cache: dict[str, str] | None = None
_nse_token_cache_at: float = 0.0

# Kite's per-request span limits for historical candles (documented: 2000 days for daily, 60 for
# minute), kept a little inside so a boundary day is never the one that fails.
_HISTORY_SPAN_DAYS = {"day": 1900, "minute": 55}
# Short index names -> Kite's tradingsymbol for the index (INDEX_QUOTE_KEYS holds exchange:symbol).
_INDEX_TRADINGSYMBOLS = {
    name: key.split(":", 1)[1] for name, key in INDEX_QUOTE_KEYS.items() if key.startswith("NSE:")
}


class ZerodhaKiteAdapter:
    broker_name = "zerodha"
    has_sandbox = False

    def __init__(
        self,
        credentials: BrokerCredentials,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._credentials = credentials
        self._base_url = base_url
        # `transport` is only ever non-None in tests (httpx.MockTransport)
        # -- production code leaves it None and gets a real network
        # transport, same shape as src.agents.llm_router's clients.
        self._transport = transport
        self._timeout = timeout

    def _headers(self) -> dict[str, Any]:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self._credentials.api_key}:{self._credentials.access_token}",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, transport=self._transport, timeout=self._timeout
            ) as client:
                resp = await client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.TransportError as exc:
            raise broker_unreachable("zerodha", method, path, exc) from exc
        if resp.status_code >= 500:
            raise BrokerServerError(resp.status_code, f"zerodha {method} {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise broker_request_error(
                resp.status_code, f"zerodha {method} {path}: {resp.text[:200]}"
            )
        # httpx's own Response.json() return type is untyped (Any).
        return cast(dict[str, Any], resp.json())

    def build_order_payload(self, order: OrderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        payload: dict[str, Any] = {}
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
        # Kite documents `exchange:tradingsymbol` for the request and keys the response by it;
        # a symbol that already names its exchange is passed through untouched. (Tolerant on
        # the way back: the bare symbol is also tried, and a missing key is simply "no data".)
        instrument = await self._key_for(symbol)
        if instrument is None:
            raise BrokerQuoteUnavailableError("zerodha", symbol)
        data = await self._request("GET", "/quote", params={"i": instrument})
        rows = data.get("data", {})
        row = rows.get(instrument) or rows.get(symbol) or {}
        return quote_from_row(symbol, row, broker="zerodha")

    @staticmethod
    def _quote_key(symbol: str) -> str:
        if ":" in symbol:
            return symbol
        return INDEX_QUOTE_KEYS.get(symbol.strip().upper(), f"{DEFAULT_QUOTE_EXCHANGE}:{symbol}")

    async def _key_for(self, symbol: str) -> str | None:
        """The `exchange:tradingsymbol` Kite wants for `symbol`. An option contract is named
        `option_symbol(...)` here and is looked up in the NFO dump by underlying, expiry, strike
        and type (`None` if the dump lists no such contract); anything else is as before."""
        parsed = parse_option_symbol(symbol)
        if parsed is None:
            return self._quote_key(symbol)
        entry = (await self._option_index()).get(parsed)
        return f"NFO:{entry[0]}" if entry is not None else None

    async def _option_index(
        self,
    ) -> dict[tuple[str, date, float, str], tuple[str, OptionContract]]:
        """(underlying, expiry, strike, CE/PE) -> (Kite tradingsymbol, contract), built from the
        NFO dump and kept as long as that dump is."""
        global _nfo_option_index
        rows = await self._get_nfo_instruments()
        if _nfo_option_index is not None and _nfo_option_index[0] is rows:
            return _nfo_option_index[1]
        index: dict[tuple[str, date, float, str], tuple[str, OptionContract]] = {}
        for row in rows:
            kind = row.get("instrument_type")
            underlying = (row.get("name") or "").upper()
            tradingsymbol = row.get("tradingsymbol")
            if kind not in ("CE", "PE") or not underlying or not tradingsymbol:
                continue
            try:
                expiry = date.fromisoformat(row["expiry"])
                strike = float(row["strike"])
                lot_size = int(row["lot_size"])
            except (KeyError, ValueError):
                continue
            if lot_size <= 0:
                continue
            contract = OptionContract(
                symbol=option_symbol(underlying, expiry, strike, kind),
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=kind,
                lot_size=lot_size,
                tick_size=float(row.get("tick_size") or 0.0),
            )
            index[(underlying, expiry, strike, kind)] = (tradingsymbol, contract)
        _nfo_option_index = (rows, index)
        return index

    async def get_option_contracts(self, underlying: str) -> list[OptionContract]:
        wanted = underlying.strip().upper()
        return [
            contract
            for (name, _expiry, _strike, _kind), (_symbol, contract) in (
                await self._option_index()
            ).items()
            if name == wanted
        ]

    async def get_quotes(self, symbols: Sequence[str]) -> dict[str, BrokerQuote]:
        """Many quotes per `/quote` call (Kite takes up to 500 `i=` params; chunked well below
        that to keep the URL short). A symbol Kite has no price for is absent from the result."""
        quotes: dict[str, BrokerQuote] = {}
        unique = list(dict.fromkeys(symbols))
        for start in range(0, len(unique), QUOTE_BATCH_SIZE):
            chunk = unique[start : start + QUOTE_BATCH_SIZE]
            keys: dict[str, str] = {}
            for symbol in chunk:
                key = await self._key_for(symbol)
                if key is not None:  # an option the dump does not list has no quote to ask for
                    keys[symbol] = key
            if not keys:
                continue
            data = await self._request(
                "GET", "/quote", params=[("i", key) for key in keys.values()]
            )
            rows = data.get("data", {})
            for symbol, key in keys.items():
                try:
                    quotes[symbol] = quote_from_row(
                        symbol, rows.get(key) or rows.get(symbol) or {}, broker="zerodha"
                    )
                except BrokerQuoteUnavailableError:
                    continue
        return quotes

    async def _get_nfo_instruments(self) -> list[dict[str, Any]]:
        global _nfo_instruments_cache, _nfo_instruments_cache_at
        now = time.monotonic()
        if (
            _nfo_instruments_cache is not None
            and (now - _nfo_instruments_cache_at) < _NFO_INSTRUMENTS_CACHE_TTL_SECONDS
        ):
            return _nfo_instruments_cache

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, transport=self._transport, timeout=self._timeout
            ) as client:
                resp = await client.get("/instruments/NFO", headers=self._headers())
        except httpx.TransportError as exc:
            raise broker_unreachable("zerodha", "GET", "/instruments/NFO", exc) from exc
        if resp.status_code >= 500:
            raise BrokerServerError(
                resp.status_code, f"zerodha GET /instruments/NFO: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise broker_request_error(
                resp.status_code, f"zerodha GET /instruments/NFO: {resp.text[:200]}"
            )

        rows = list(csv.DictReader(io.StringIO(resp.text)))
        _nfo_instruments_cache = rows
        _nfo_instruments_cache_at = now
        return rows

    async def _nse_tokens(self) -> dict[str, str]:
        """tradingsymbol -> instrument_token for NSE cash equities and NSE indices."""
        global _nse_token_cache, _nse_token_cache_at, _nse_equity_symbols
        now = time.monotonic()
        if (
            _nse_token_cache is not None
            and (now - _nse_token_cache_at) < _NSE_INSTRUMENTS_CACHE_TTL_SECONDS
        ):
            return _nse_token_cache
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, transport=self._transport, timeout=self._timeout
            ) as client:
                resp = await client.get("/instruments/NSE", headers=self._headers())
        except httpx.TransportError as exc:
            raise broker_unreachable("zerodha", "GET", "/instruments/NSE", exc) from exc
        if resp.status_code >= 500:
            raise BrokerServerError(
                resp.status_code, f"zerodha GET /instruments/NSE: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise broker_request_error(
                resp.status_code, f"zerodha GET /instruments/NSE: {resp.text[:200]}"
            )
        tokens: dict[str, str] = {}
        equities: set[str] = set()
        for row in csv.DictReader(io.StringIO(resp.text)):
            symbol = (row.get("tradingsymbol") or "").upper()
            token = row.get("instrument_token")
            is_equity = row.get("segment") == "NSE" and row.get("instrument_type") == "EQ"
            if symbol and token and (is_equity or row.get("segment") == "INDICES"):
                tokens.setdefault(symbol, token)
                if is_equity:
                    equities.add(symbol)
        _nse_token_cache = tokens
        _nse_equity_symbols = frozenset(equities)
        _nse_token_cache_at = now
        return tokens

    async def _instrument_token(self, symbol: str) -> str:
        tokens = await self._nse_tokens()
        wanted = symbol.strip().upper()
        # An exchange-qualified name ("NSE:INFY") is accepted; the exchange part is dropped.
        if ":" in wanted:
            wanted = wanted.split(":", 1)[1]
        token = tokens.get(_INDEX_TRADINGSYMBOLS.get(wanted, wanted)) or tokens.get(wanted)
        if token is None:
            raise InstrumentNotFoundError("zerodha", symbol)
        return token

    async def has_instrument(self, symbol: str) -> bool:
        try:
            await self._instrument_token(symbol)
        except InstrumentNotFoundError:
            return False
        return True

    async def search_symbols(self, query: str, limit: int = 10) -> list[SymbolHit]:
        tokens = await self._nse_tokens()
        hits = [SymbolHit(symbol, "equity") for symbol in _nse_equity_symbols]
        hits += [
            SymbolHit(name, "index")
            for name in SEARCHABLE_INDICES
            if _INDEX_TRADINGSYMBOLS.get(name) in tokens
        ]
        return rank_symbol_hits(hits, query, limit)

    async def get_historical_candles(
        self, symbol: str, interval: CandleInterval, start: date, end: date
    ) -> list[Candle]:
        """Candles for `start`..`end` inclusive, oldest first. A range wider than Kite allows per
        request is fetched in spans. Today's daily candle, when asked for, is the partial bar so
        far: callers must not treat it as final before the session closes."""
        if end < start:
            raise ValueError(f"end {end} is before start {start}")
        token = await self._instrument_token(symbol)
        span = timedelta(days=_HISTORY_SPAN_DAYS[interval])
        candles: dict[datetime, Candle] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + span)
            data = await self._request(
                "GET",
                f"/instruments/historical/{token}/{interval}",
                params={
                    "from": f"{cursor.isoformat()} 00:00:00",
                    "to": f"{chunk_end.isoformat()} 23:59:59",
                    "oi": 1,
                },
            )
            for row in data.get("data", {}).get("candles", []):
                candle = parse_candle(row)
                candles[candle.timestamp] = candle
            cursor = chunk_end + timedelta(days=1)
        return [candles[stamp] for stamp in sorted(candles)]

    async def _batch_quote(self, tradingsymbols: list[str]) -> dict[str, dict[str, Any]]:
        """Kite Connect's real `/quote` endpoint accepts multiple `i=`
        params in one call (documented cap: 500 instruments) -- an option
        chain's CE+PE legs across every strike for one expiry is well
        within that, so no chunking is needed for this call site."""
        if not tradingsymbols:
            return {}
        params = [("i", f"NFO:{symbol}") for symbol in tradingsymbols]
        data = await self._request("GET", "/quote", params=params)
        return cast(dict[str, dict[str, Any]], data.get("data", {}))

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
        strikes: dict[float, dict[str, dict[str, Any]]] = {}
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

    async def get_futures_contracts(self, underlying: str) -> list[FuturesContract]:
        """The underlying's monthly futures from the NFO instrument dump (name = the underlying,
        type FUT), soonest expiry first. Lot size and tick size are the dump's own figures."""
        wanted = underlying.strip().upper()
        contracts: list[FuturesContract] = []
        for row in await self._get_nfo_instruments():
            if row.get("instrument_type") != "FUT" or (row.get("name") or "").upper() != wanted:
                continue
            try:
                expiry = date.fromisoformat(row["expiry"])
                lot_size = int(row["lot_size"])
            except (KeyError, ValueError):
                continue
            if lot_size <= 0:
                continue
            contracts.append(
                FuturesContract(
                    symbol=futures_symbol(wanted, expiry),
                    underlying=wanted,
                    expiry=expiry,
                    lot_size=lot_size,
                    tick_size=float(row.get("tick_size") or 0.0),
                )
            )
        return sorted(contracts, key=lambda contract: contract.expiry)

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
