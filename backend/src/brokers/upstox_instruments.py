"""Turning a trading symbol into the `instrument_key` Upstox wants.

Upstox identifies every instrument by an `instrument_key` such as `NSE_EQ|INE009A01021` (segment
and ISIN) -- never by the bare symbol (`INFY`) the rest of this system uses. The adapter used to
send the symbol and every quote came back `UDAPI1087: One of either symbol or instrument_key is
invalid`; found against a live token, not by the mocked tests, which accept any string.

Upstox publishes its instrument master as a public, gzipped JSON file refreshed around 06:00 IST.
This downloads the NSE one (equities and indices), keeps only the symbol -> key map (a few
thousand entries out of the ~100 MB of JSON), and holds it for `CACHE_TTL_SECONDS`. A symbol that
already looks like a key (`NSE_EQ|...`, anything with a `|`) is passed through untouched, so a
caller that already knows the key is never slowed or broken by the lookup.
"""

import asyncio
import gzip
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
import structlog

from src.brokers.base import (
    BrokerRequestError,
    FuturesContract,
    InstrumentNotFoundError,
    OptionContract,
    broker_request_error,
    broker_unreachable,
    futures_symbol,
    option_symbol,
    parse_futures_symbol,
    parse_option_symbol,
)
from src.engine.risk.circuit_breaker import BrokerServerError

logger = structlog.get_logger(__name__)

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
# The file is regenerated once a day (~06:00 IST); a symbol list does not change within hours.
CACHE_TTL_SECONDS = 6 * 3600
# After a failed refresh, keep serving the old map and try again in a while, not on every call.
RETRY_AFTER_FAILURE_SECONDS = 600.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0

EQUITY_SEGMENT = "NSE_EQ"
EQUITY_TYPE = "EQ"
INDEX_SEGMENT = "NSE_INDEX"
FUTURES_SEGMENT = "NSE_FO"
FUTURES_TYPE = "FUT"
IST = timezone(timedelta(hours=5, minutes=30))


class UpstoxInstrumentNotFoundError(InstrumentNotFoundError):
    """Upstox lists no NSE equity or index under this name. A `BrokerRequestError` (404): a
    request mistake, not an outage, so it never trips the circuit breaker."""

    def __init__(self, symbol: str):
        super().__init__(
            "upstox",
            symbol,
            f"upstox lists no NSE equity or index named {symbol!r} "
            "(use the exchange trading symbol, e.g. INFY, or a full instrument_key)",
        )


class InstrumentResolver(Protocol):
    async def resolve(self, symbol: str) -> str: ...

    async def listed_names(self) -> tuple[list[str], set[str]]: ...

    async def futures_for(self, underlying: str) -> list[FuturesContract]: ...

    async def options_for(self, underlying: str) -> list[OptionContract]: ...


@dataclass(frozen=True, slots=True)
class FutureEntry:
    key: str
    contract: FuturesContract


@dataclass(frozen=True, slots=True)
class OptionEntry:
    key: str
    contract: OptionContract


@dataclass
class SymbolIndex:
    equities: dict[str, str] = field(default_factory=dict)  # TRADING SYMBOL -> instrument_key
    indices: dict[str, str] = field(default_factory=dict)  # NAME or TRADING SYMBOL -> key
    # (UNDERLYING, expiry year, expiry month) -> the monthly future. Upstox names a future
    # `NIFTY FUT 27 OCT 26`; this system names it `futures_symbol(...)`, so lookups go by what
    # both share: the underlying and the month it expires in.
    futures: dict[tuple[str, int, int], FutureEntry] = field(default_factory=dict)
    # (UNDERLYING, expiry date, strike, CE/PE) -> the option contract and its instrument_key.
    options: dict[tuple[str, date, float, str], OptionEntry] = field(default_factory=dict)


def _option_entry(
    row: dict[str, Any], key: str
) -> tuple[tuple[str, date, float, str], OptionEntry] | None:
    underlying = str(row.get("underlying_symbol") or "").upper()
    expiry_ms = row.get("expiry")
    kind = row.get("instrument_type")
    strike = row.get("strike_price")
    if not underlying or kind not in ("CE", "PE") or not isinstance(expiry_ms, int | float):
        return None
    if not isinstance(strike, int | float):
        return None
    expiry = datetime.fromtimestamp(expiry_ms / 1000, IST).date()
    lot_size = int(row.get("lot_size") or 0)
    if lot_size <= 0:
        return None
    contract = OptionContract(
        symbol=option_symbol(underlying, expiry, float(strike), str(kind)),
        underlying=underlying,
        expiry=expiry,
        strike=float(strike),
        option_type=str(kind),
        lot_size=lot_size,
        tick_size=float(row.get("tick_size") or 0.0),
    )
    return (underlying, expiry, float(strike), str(kind)), OptionEntry(key=key, contract=contract)


def _future_entry(row: dict[str, Any], key: str) -> tuple[tuple[str, int, int], FutureEntry] | None:
    underlying = str(row.get("underlying_symbol") or "").upper()
    expiry_ms = row.get("expiry")
    if not underlying or not isinstance(expiry_ms, int | float):
        return None
    # The file stamps an expiry at 23:59:59 IST of the expiry day, in epoch milliseconds.
    expiry = datetime.fromtimestamp(expiry_ms / 1000, IST).date()
    lot_size = int(row.get("lot_size") or 0)
    if lot_size <= 0:
        return None
    contract = FuturesContract(
        symbol=futures_symbol(underlying, expiry),
        underlying=underlying,
        expiry=expiry,
        lot_size=lot_size,
        tick_size=float(row.get("tick_size") or 0.0),
    )
    return (underlying, expiry.year, expiry.month), FutureEntry(key=key, contract=contract)


def build_symbol_index(rows: list[dict[str, Any]]) -> SymbolIndex:
    """Cash equities (`NSE_EQ` / type `EQ` -- not the BE/SM/... series, which trade differently) by
    trading symbol, and indices by name and trading symbol. Upper-cased: symbols are compared
    case-insensitively."""
    index = SymbolIndex()
    for row in rows:
        key = row.get("instrument_key")
        if not key:
            continue
        segment = row.get("segment")
        if segment == EQUITY_SEGMENT and row.get("instrument_type") == EQUITY_TYPE:
            symbol = row.get("trading_symbol")
            if symbol:
                index.equities.setdefault(str(symbol).upper(), key)
        elif segment == INDEX_SEGMENT:
            for name in (row.get("name"), row.get("trading_symbol")):
                if name:
                    index.indices.setdefault(str(name).upper(), key)
        elif segment == FUTURES_SEGMENT and row.get("instrument_type") == FUTURES_TYPE:
            if (entry := _future_entry(row, key)) is not None:
                index.futures.setdefault(entry[0], entry[1])
        elif segment == FUTURES_SEGMENT and row.get("instrument_type") in ("CE", "PE"):
            if (option := _option_entry(row, key)) is not None:
                index.options.setdefault(option[0], option[1])
    return index


def parse_instrument_file(content: bytes) -> SymbolIndex:
    """`NSE.json.gz` bytes (or the same JSON already decompressed) -> the symbol map."""
    raw = content if content[:1] in (b"[", b"{") else gzip.decompress(content)
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("the Upstox instrument file is not a JSON list")
    return build_symbol_index(rows)


class UpstoxInstruments:
    """Resolves symbols to instrument keys, downloading and caching Upstox's instrument file.

    One shared instance serves every adapter (`get_instruments()`): adapters are rebuilt whenever
    credentials change, the symbol list is not theirs to re-download each time."""

    def __init__(
        self,
        *,
        url: str = INSTRUMENTS_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._transport = transport
        self._timeout = timeout
        self._ttl = ttl_seconds
        self._clock = clock
        self._index: SymbolIndex | None = None
        self._valid_until = 0.0
        self._lock = asyncio.Lock()

    async def resolve(self, symbol: str) -> str:
        cleaned = symbol.strip()
        if "|" in cleaned and parse_option_symbol(cleaned) is None:
            return cleaned  # already an instrument_key (an option's shared name has `|` too)
        index = await self._current_index()
        wanted = cleaned.upper()
        if (parsed := parse_futures_symbol(wanted)) is not None:
            entry = index.futures.get(parsed)
            if entry is None:
                raise UpstoxInstrumentNotFoundError(symbol)
            return entry.key
        if (parsed_option := parse_option_symbol(wanted)) is not None:
            option = index.options.get(parsed_option)
            if option is None:
                raise UpstoxInstrumentNotFoundError(symbol)
            return option.key
        key = index.equities.get(wanted) or index.indices.get(wanted)
        if key is None:
            raise UpstoxInstrumentNotFoundError(symbol)
        return key

    async def listed_names(self) -> tuple[list[str], set[str]]:
        """(cash equity trading symbols, index names) in the current instrument file."""
        index = await self._current_index()
        return list(index.equities), set(index.indices)

    async def options_for(self, underlying: str) -> list[OptionContract]:
        """The underlying's listed option contracts, every expiry and strike."""
        index = await self._current_index()
        wanted = underlying.strip().upper()
        return [e.contract for (name, _x, _s, _k), e in index.options.items() if name == wanted]

    async def futures_for(self, underlying: str) -> list[FuturesContract]:
        """The underlying's listed monthly futures, soonest expiry first."""
        index = await self._current_index()
        wanted = underlying.strip().upper()
        found = [e.contract for (name, _y, _m), e in index.futures.items() if name == wanted]
        return sorted(found, key=lambda contract: contract.expiry)

    async def _current_index(self) -> SymbolIndex:
        if self._index is not None and self._clock() < self._valid_until:
            return self._index
        async with self._lock:
            if self._index is not None and self._clock() < self._valid_until:
                return self._index  # another caller refreshed it while this one waited
            try:
                self._index = await self._download()
                self._valid_until = self._clock() + self._ttl
            except (BrokerServerError, BrokerRequestError) as exc:
                if self._index is None:
                    raise  # nothing to fall back to: say why the symbol cannot be resolved
                logger.warning(
                    "upstox_instruments.refresh_failed_keeping_old_list",
                    error=str(exc),
                    retry_in_seconds=RETRY_AFTER_FAILURE_SECONDS,
                )
                self._valid_until = self._clock() + RETRY_AFTER_FAILURE_SECONDS
            return self._index

    async def _download(self) -> SymbolIndex:
        path = self._url.rsplit("/", 1)[-1]
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                resp = await client.get(self._url)
        except httpx.TransportError as exc:
            raise broker_unreachable("upstox", "GET", path, exc) from exc
        if resp.status_code >= 500:
            raise BrokerServerError(resp.status_code, f"upstox GET {path}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise broker_request_error(resp.status_code, f"upstox GET {path}: {resp.text[:200]}")
        try:
            index = await asyncio.to_thread(parse_instrument_file, resp.content)  # ~100 MB of JSON
        except (ValueError, OSError, EOFError) as exc:  # not JSON, not gzip, truncated
            raise BrokerServerError(502, f"upstox GET {path}: unreadable file ({exc})") from exc
        logger.info(
            "upstox_instruments.loaded",
            equities=len(index.equities),
            indices=len(index.indices),
            futures=len(index.futures),
            options=len(index.options),
        )
        return index


_default: UpstoxInstruments | None = None


def get_instruments() -> UpstoxInstruments:
    """The process-wide directory the adapters share."""
    global _default
    if _default is None:
        _default = UpstoxInstruments()
    return _default
