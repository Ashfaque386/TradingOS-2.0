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

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


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
    raw: dict


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
    raw: dict


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: int
    average_price: float
    pnl: float


@dataclass(frozen=True, slots=True)
class BrokerQuote:
    symbol: str
    last_price: float
    bid: float | None
    ask: float | None
    timestamp: datetime


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
    broker_name: str
    has_sandbox: bool

    def build_order_payload(self, order: OrderRequest) -> dict: ...
    async def place_order(self, order: OrderRequest) -> BrokerOrderResult: ...
    async def modify_order(
        self, broker_order_id: str, *, price: float | None = None, quantity: int | None = None
    ) -> BrokerOrderResult: ...
    async def cancel_order(self, broker_order_id: str) -> BrokerOrderResult: ...
    async def get_order_book(self) -> list[BrokerOrder]: ...
    async def get_margin(self) -> MarginInfo: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_quote(self, symbol: str) -> BrokerQuote: ...
    async def get_option_chain(self, underlying: str, expiry: str) -> list[OptionChainEntry]: ...
    async def get_expiries(self, underlying: str) -> list[str]: ...
