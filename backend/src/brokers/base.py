"""The `BrokerAdapter` interface (Build Spec §13): a single operation set
-- place/modify/cancel order, order book, margin, positions, quote, option
chain, expiries -- that every concrete broker implementation (Zerodha Kite
Connect, Upstox) satisfies identically, so every caller (the circuit
breaker wrapper, Shadow Mode, the Phase 7 paper-trading tick feed, and
eventually Phase 9's live order pipeline) is written once against this
Protocol and never against a specific broker's SDK shape.

`broker_name` and `has_sandbox` are plain attributes, not methods -- they
never require a network call to answer, and Shadow Mode
(src.orchestration.shadow_mode) reads `has_sandbox` to decide whether a
dry run may safely make a real call or must stay local-payload-only. See
that module's docstring for why this distinction is never allowed to blur.

`build_order_payload` is deliberately a separate, synchronous, no-network
method (not folded into `place_order`) precisely so Shadow Mode can
construct -- and record -- the exact payload a real order would carry
without ever sending it, for a broker with no sandbox to send it to.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol

from src.engine.risk.circuit_breaker import BrokerServerError

_IST = timezone(timedelta(hours=5, minutes=30))


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class BrokerRequestError(Exception):
    """Raised for a broker HTTP 4xx (bad symbol, invalid params, expired
    auth) -- a caller/request mistake, not a broker outage. Deliberately
    a different exception type than `BrokerServerError`
    (src.engine.risk.circuit_breaker): the circuit breaker's failure
    counter only reacts to `BrokerServerError`, so a 4xx never trips it,
    same as that module's own docstring promises."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(message or f"broker returned {status_code}")


class BrokerAuthError(BrokerRequestError):
    """A broker 401/403: the stored credentials were not accepted (an expired
    daily access token, a revoked key). A `BrokerRequestError` still -- callers
    that catch that keep working -- but a distinct type because, unlike a bad
    symbol or parameter, it fails *every* call until the credentials change,
    so retrying it every few seconds only hammers the broker with a token it
    has already refused."""


class InstrumentNotFoundError(BrokerRequestError):
    """The broker lists no such NSE equity or index. A 404 request mistake, not an outage, so it
    never trips the circuit breaker."""

    def __init__(self, broker: str, symbol: str, message: str | None = None):
        self.broker = broker
        self.symbol = symbol
        super().__init__(
            404,
            message
            or (
                f"{broker} lists no NSE equity or index named {symbol!r} "
                "(use the exchange trading symbol, e.g. INFY)"
            ),
        )


class BrokerQuoteUnavailableError(BrokerRequestError):
    """The broker answered, but with no usable price for this symbol (no entry
    for it, or a last price of 0). Raised instead of returning a quote priced
    0.0: every consumer of `get_quote` takes `last_price` at face value -- the
    paper-trading tick feed would read a 0.0 tick as a total price collapse and
    stop out any open position, and a live order intent would be built with a
    zero price and a zero position value. A `BrokerRequestError` (status 404,
    "not found") so it never trips the circuit breaker, which reacts only to
    outages."""

    def __init__(self, broker: str, symbol: str):
        self.broker = broker
        self.symbol = symbol
        super().__init__(404, f"{broker} returned no usable quote for {symbol}")


def broker_unreachable(broker: str, method: str, path: str, exc: Exception) -> BrokerServerError:
    """The broker could not be reached at all (connect or read timeout, DNS
    failure, connection reset). A `BrokerServerError`, like a 5xx, because it is
    an *outage* and the circuit breaker only counts that family: before this, a
    network failure surfaced as a bare httpx exception, never counted, so an
    unreachable broker would be called on every firing for ever. The message
    names the broker and the call -- a raw `ConnectTimeout` names neither."""
    detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
    return BrokerServerError(503, f"{broker} {method} {path}: unreachable ({detail})"[:300])


_AUTH_REJECTED_STATUSES = frozenset({401, 403})


def broker_request_error(status_code: int, message: str) -> BrokerRequestError:
    """The right exception for a broker 4xx: `BrokerAuthError` for 401/403,
    a plain `BrokerRequestError` for everything else."""
    cls = BrokerAuthError if status_code in _AUTH_REJECTED_STATUSES else BrokerRequestError
    return cls(status_code, message)


@dataclass(frozen=True, slots=True)
class BrokerCredentials:
    """Decrypted broker API credentials, held in memory only as long as a
    call needs them. `__repr__` never includes the secret values -- only
    which fields are present -- so an accidental `logger.info(...,
    creds=credentials)` or an uncaught exception's traceback can't leak a
    key/token. This is the in-memory counterpart to
    src.security.secrets_store's at-rest encryption; both exist because
    Build Spec §20 requires secrets "never logged, never returned in API
    responses except as write-only fields", not just encrypted on disk.

    `redirect_uri`/`token_expires_at`/`token_duration` are OAuth metadata
    (src.api.routes.broker_oauth), not secrets themselves -- safe to echo
    back in a status response, unlike api_secret/access_token. Added on top
    of the original api_key/api_secret/access_token trio (still the only
    fields a hand-pasted-token POST ever sets) so a stored row round-trips
    through SecretsStore.set_credentials's already-existing
    "keep whatever's non-None" behavior without needing a schema migration.
    """

    api_key: str
    api_secret: str | None = None
    access_token: str | None = None
    redirect_uri: str | None = None
    token_expires_at: str | None = None  # ISO 8601, UTC
    # Upstox only ("standard" | "extended"); irrelevant to Zerodha, whose
    # access-token lifetime isn't caller-selectable.
    token_duration: str | None = None
    # Operator-set callback URL (Broker Config), used instead of the one
    # derived from the request origin -- e.g. a tunnel or LAN hostname
    # registered in the broker's developer console. `redirect_uri` above
    # stays "the one last sent to the broker".
    redirect_uri_override: str | None = None

    def __repr__(self) -> str:
        present = ["api_key"]
        if self.api_secret:
            present.append("api_secret")
        if self.access_token:
            present.append("access_token")
        if self.token_expires_at:
            present.append("token_expires_at")
        return f"BrokerCredentials(<redacted: {', '.join(present)}>)"

    @property
    def token_status(self) -> str:
        """Derived, not stored -- "expired" is a function of wall-clock
        time relative to token_expires_at, so persisting it as its own
        field would just go stale. "never-connected": no access_token at
        all (only a hand-pasted api_key/api_secret, or nothing). "valid":
        has a token and (no known expiry, or expiry still in the future).
        "expired": has a token whose known expiry has passed.
        """
        if not self.access_token:
            return "never-connected"
        if self.token_expires_at is None:
            return "valid"
        try:
            expires_at = datetime.fromisoformat(self.token_expires_at)
        except ValueError:
            return "valid"
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return "valid" if datetime.now(UTC) < expires_at else "expired"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    # Broker-specific product code (e.g. Kite's "MIS"/"CNC", Upstox's
    # "D"/"I") passed through as-is rather than modeled here -- this
    # layer standardizes the *operation set*, not every broker's product
    # taxonomy.
    product: str = "MIS"


@dataclass(frozen=True, slots=True)
class BrokerOrderResult:
    broker_order_id: str
    status: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    status: str
    average_price: float | None


@dataclass(frozen=True, slots=True)
class MarginInfo:
    available_margin: float
    used_margin: float
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: int
    average_price: float
    pnl: float


# Symbols per quote request: well under both brokers' documented caps (500), so URLs stay short.
QUOTE_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One level of a broker's market depth: a price and the quantity resting there."""

    price: float
    quantity: int


@dataclass(frozen=True, slots=True)
class BrokerQuote:
    symbol: str
    last_price: float
    bid: float | None
    ask: float | None
    timestamp: datetime
    # The broker's 5-level book, best first (bids descending, asks ascending), and the open
    # interest of a derivative. Empty / None when the broker sent none -- never filled in.
    depth_bids: tuple[DepthLevel, ...] = ()
    depth_asks: tuple[DepthLevel, ...] = ()
    open_interest: float | None = None
    # When the exchange stamped this quote, where the broker says so unambiguously (Kite's
    # `timestamp`, "the exchange timestamp of the quote packet"). `timestamp` above is when we
    # received it. `None` for a broker whose equivalent is not verified: never guessed.
    exchange_time: datetime | None = None


def _exchange_time(row: dict[str, Any], broker: str) -> datetime | None:
    """Kite sends `timestamp` as `YYYY-MM-DD HH:MM:SS` with no zone (NSE time, so IST). Upstox's
    equivalent fields are documented inconsistently (the published example's `timestamp` does not
    match its own `last_trade_time`), so Upstox gets `None` until a real payload settles it."""
    if broker != "zerodha":
        return None
    raw = row.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
    except ValueError:
        return None


def _depth_levels(raw: Any) -> tuple[DepthLevel, ...]:
    """Levels with a real price and a positive quantity, in the order sent. Both brokers send
    empty padding levels (price 0, quantity 0) outside market hours; those are dropped."""
    levels: list[DepthLevel] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        price = float(entry.get("price") or 0.0)
        quantity = int(entry.get("quantity") or 0)
        if price > 0 and quantity > 0:
            levels.append(DepthLevel(price=price, quantity=quantity))
    return tuple(levels)


def quote_from_row(symbol: str, row: dict[str, Any], *, broker: str) -> BrokerQuote:
    """A quote from one row of Kite's or Upstox's quote response (they share the shape: a
    `last_price`, a `depth` of `buy`/`sell` levels and an `oi`). Raises
    `BrokerQuoteUnavailableError` for a row with no usable price."""
    last_price = float(row.get("last_price") or 0.0)
    if last_price <= 0:
        raise BrokerQuoteUnavailableError(broker, symbol)
    depth = row.get("depth") or {}
    bids = _depth_levels(depth.get("buy"))
    asks = _depth_levels(depth.get("sell"))
    open_interest = row.get("oi")
    return BrokerQuote(
        symbol=symbol,
        last_price=last_price,
        bid=bids[0].price if bids else None,
        ask=asks[0].price if asks else None,
        timestamp=datetime.now(UTC),
        depth_bids=bids,
        depth_asks=asks,
        open_interest=float(open_interest) if open_interest else None,
        exchange_time=_exchange_time(row, broker),
    )


CandleInterval = Literal["day", "minute"]


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar from a broker's historical-data endpoint. `timestamp` is timezone-aware (IST,
    as the brokers send it); a daily candle is stamped at that day's midnight."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int | None = None


def parse_candle(row: list[Any]) -> Candle:
    """[timestamp, open, high, low, close, volume(, open interest)] -- the shape both Zerodha Kite
    and Upstox v2 use. Raises ValueError on a malformed row rather than inventing a bar."""
    if len(row) < 6:
        raise ValueError(f"a candle needs at least 6 fields, got {len(row)}: {row!r}")
    stamp = datetime.fromisoformat(str(row[0]))
    if stamp.tzinfo is None:
        raise ValueError(f"candle timestamp has no timezone: {row[0]!r}")
    open_interest = row[6] if len(row) > 6 and row[6] not in (None, "") else None
    return Candle(
        timestamp=stamp,
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=int(row[5] or 0),
        open_interest=int(open_interest) if open_interest is not None else None,
    )


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_FUTURES_SYMBOL = re.compile(r"^(?:NFO:)?(?P<underlying>.+?)(?P<year>\d{2})(?P<month>[A-Z]{3})FUT$")


def futures_symbol(underlying: str, expiry: date) -> str:
    """The one name this system gives a monthly future, whichever broker lists it:
    `NFO:NIFTY26OCTFUT` (Kite's own `NFO:` + tradingsymbol form). A quote or a tick stream is
    asked for by it; each adapter maps it to its own instrument (Kite uses it as is, Upstox looks
    the underlying and expiry month up in its instrument file)."""
    return f"NFO:{underlying.upper()}{expiry:%y}{_MONTHS[expiry.month - 1]}FUT"


def parse_futures_symbol(symbol: str) -> tuple[str, int, int] | None:
    """`NFO:NIFTY26OCTFUT` -> (`NIFTY`, 2026, 10); `None` for anything that is not a monthly
    future's name (an equity, an index, an option)."""
    match = _FUTURES_SYMBOL.match(symbol.strip().upper())
    if match is None or match["month"] not in _MONTHS:
        return None
    return match["underlying"], 2000 + int(match["year"]), _MONTHS.index(match["month"]) + 1


@dataclass(frozen=True, slots=True)
class FuturesContract:
    """One monthly future as a broker's instrument master lists it. `lot_size` is the broker's
    figure today -- exchanges revise it, so it is read each time and never kept as a constant."""

    symbol: str  # `futures_symbol(...)`
    underlying: str
    expiry: date
    lot_size: int
    tick_size: float


_OPTION_SYMBOL = re.compile(
    r"^NFO:(?P<underlying>[^|]+)\|(?P<expiry>\d{6})\|(?P<strike>\d+(?:\.\d+)?)\|(?P<kind>CE|PE)$"
)


def _strike_text(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else repr(float(strike))


def option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    """The one name this system gives an option contract, whichever broker lists it:
    `NFO:NIFTY|261027|24500|CE` (underlying, expiry as YYMMDD, strike, CE or PE). Brokers name
    options differently, and Kite names a weekly differently from a monthly, so neither's own
    symbol is used: each adapter maps this name to its own instrument through its instrument
    master (by underlying, expiry date, strike and type), which is exact for every expiry."""
    kind = option_type.upper()
    if kind not in ("CE", "PE"):
        raise ValueError(f"an option is CE or PE, not {option_type!r}")
    return f"NFO:{underlying.upper()}|{expiry:%y%m%d}|{_strike_text(strike)}|{kind}"


def parse_option_symbol(symbol: str) -> tuple[str, date, float, str] | None:
    """`NFO:NIFTY|261027|24500|CE` -> (`NIFTY`, 2026-10-27, 24500.0, `CE`); `None` for anything
    that is not an option contract's name (an equity, a future, an index)."""
    match = _OPTION_SYMBOL.match(symbol.strip().upper())
    if match is None:
        return None
    stamp = match["expiry"]
    try:
        expiry = date(2000 + int(stamp[:2]), int(stamp[2:4]), int(stamp[4:]))
    except ValueError:
        return None
    return match["underlying"], expiry, float(match["strike"]), match["kind"]


@dataclass(frozen=True, slots=True)
class OptionContract:
    """One option as a broker's instrument master lists it (lot size read each time, as for
    futures: exchanges revise it)."""

    symbol: str  # `option_symbol(...)`
    underlying: str
    expiry: date
    strike: float
    option_type: str  # "CE" | "PE"
    lot_size: int
    tick_size: float


@dataclass(frozen=True, slots=True)
class OptionChainEntry:
    """`call_oi`/`put_oi`/`call_iv`/`put_iv` are genuinely optional --
    `None` when a broker's response doesn't carry them for a given row,
    never a fabricated `0.0`, the same never-fabricate-a-metric posture
    as every other optional numeric field in this codebase (Follow-up D,
    Phase 16 wiring audit)."""

    strike: float
    call_symbol: str | None
    put_symbol: str | None
    call_ltp: float | None
    put_ltp: float | None
    call_oi: float | None = None
    put_oi: float | None = None
    call_iv: float | None = None
    put_iv: float | None = None


class BrokerAdapter(Protocol):
    # Declared as read-only properties (not plain attributes) so a wrapper
    # like ResilientBrokerAdapter -- which forwards them as @property,
    # never reassignable -- still satisfies this Protocol structurally;
    # nothing anywhere in this codebase ever assigns to either after
    # construction, so requiring settability was stricter than what's
    # actually used, not an intentional part of the interface.
    @property
    def broker_name(self) -> str: ...
    @property
    def has_sandbox(self) -> bool: ...

    def build_order_payload(self, order: OrderRequest) -> dict[str, Any]: ...
    async def place_order(self, order: OrderRequest) -> BrokerOrderResult: ...
    async def modify_order(
        self, broker_order_id: str, *, price: float | None = None, quantity: int | None = None
    ) -> BrokerOrderResult: ...
    async def cancel_order(self, broker_order_id: str) -> BrokerOrderResult: ...
    async def get_order_book(self) -> list[BrokerOrder]: ...
    async def get_margin(self) -> MarginInfo: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_quote(self, symbol: str) -> BrokerQuote: ...
    async def get_quotes(self, symbols: Sequence[str]) -> dict[str, BrokerQuote]:
        """Quotes for many symbols in as few calls as the broker allows. A symbol the broker has
        no usable price for is left out of the result (not an error, not a zero quote)."""
        ...

    async def get_option_chain(self, underlying: str, expiry: str) -> list[OptionChainEntry]: ...
    async def get_expiries(self, underlying: str) -> list[str]: ...
    async def get_option_contracts(self, underlying: str) -> list[OptionContract]:
        """The underlying's listed option contracts (every expiry and strike), unsorted; empty
        when it has none."""
        ...

    async def get_futures_contracts(self, underlying: str) -> list[FuturesContract]:
        """The underlying's listed monthly futures, soonest expiry first (none if it has none)."""
        ...

    async def get_historical_candles(
        self, symbol: str, interval: CandleInterval, start: date, end: date
    ) -> list[Candle]: ...
    async def has_instrument(self, symbol: str) -> bool: ...
