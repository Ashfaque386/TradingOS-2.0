"""Real market data for the ingestion pipelines, from the connected broker (the real-market-data
plan, phase 2).

`MarketDataProvider` had one implementation, `FakeMarketDataProvider`, so the data lake -- and with
it every signal computed from it -- rested on generated prices. This provider takes daily and
intraday candles from the broker's historical endpoints instead.

**Rules it enforces**

- **History is pinned to one source per symbol.** The first broker that lists a symbol becomes its
  source (Zerodha is tried first); every later fetch for that symbol goes to the same broker, and if
  that broker is not connected the fetch fails -- it does not quietly use the other one. Measured
  on a real account, the two brokers' daily candles disagree for some symbols (INFY by ~2% on 183 of
  272 days; NIFTY not at all): two adjustment bases spliced into one series is a fake price jump
  that a moving-average signal would trade on. Rows carry their `source`; see `lake.pinned_source`.
- **Only final bars.** A daily bar for today is withheld until the session is over: a broker's
  daily candle for today is still forming, and no signal may be computed from a partial day.
- **No fabrication.** An unknown symbol is an error, not an empty list; no connected broker is an
  error (`NoRealDataSourceError`), not a fall-back to generated data.
- **Honest gaps.** Brokers publish no corporate-action feed, so `corporate_actions` returns
  nothing -- recorded as such, not invented (their candles are already split/bonus adjusted, which
  was verified across RELIANCE's 2024 bonus).
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import structlog

from src.brokers.base import (
    BrokerAdapter,
    Candle,
    FuturesContract,
    InstrumentNotFoundError,
    OptionContract,
)
from src.brokers.futures_contracts import resolve_contract
from src.data import lake
from src.data.providers import (
    CorporateActionRecord,
    InstrumentRecord,
    MarketPulseSnapshot,
)
from src.engine.paper_trading.market_hours import IST, session_is_complete

logger = structlog.get_logger(__name__)

SOURCE_NAME = "broker_market_data"
INDEX_NAMES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "INDIAVIX"})
DEFAULT_EQUITY_TICK_SIZE = 0.05


class NoRealDataSourceError(Exception):
    """No connected broker can supply real market data, and the pipelines never substitute generated
    data for it."""


class PinnedSourceUnavailableError(NoRealDataSourceError):
    """A symbol's history is pinned to a broker that is not connected right now."""


def daily_frame(candles: Sequence[Candle], source: str) -> pd.DataFrame:
    """Daily candles as the frame the lake stores: a naive date index, OHLCV and the source."""
    frame = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
            lake.SOURCE_COLUMN: [source] * len(candles),
        },
        index=pd.DatetimeIndex([pd.Timestamp(c.timestamp.astimezone(IST).date()) for c in candles]),
    )
    return frame.sort_index()


def minute_frame(candles: Sequence[Candle], source: str) -> pd.DataFrame:
    """Minute candles with naive IST timestamps (the lake's intraday key)."""
    frame = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
            lake.SOURCE_COLUMN: [source] * len(candles),
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp(c.timestamp.astimezone(IST).replace(tzinfo=None)) for c in candles]
        ),
    )
    return frame.sort_index()


class BrokerMarketDataProvider:
    source_name = SOURCE_NAME

    def __init__(
        self,
        adapters: Callable[[], Sequence[BrokerAdapter]],
        *,
        root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`adapters` is called on every use (credentials can change between calls) and returns the
        connected brokers in preference order."""
        self._adapters = adapters
        self._root = root
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---- which broker ----

    def _connected(self) -> Sequence[BrokerAdapter]:
        adapters = self._adapters()
        if not adapters:
            raise NoRealDataSourceError("no broker is connected")
        return adapters

    async def history_adapter(self, symbol: str) -> BrokerAdapter:
        """The broker this symbol's history comes from: the one it is pinned to, else the first
        connected broker that lists it."""
        adapters = self._connected()
        pinned = lake.pinned_source(self._root, symbol)
        if pinned is not None:
            for adapter in adapters:
                if adapter.broker_name == pinned:
                    return adapter
            raise PinnedSourceUnavailableError(
                f"{symbol}'s history is pinned to {pinned}, which is not connected; reconnect it "
                "(or purge the symbol's history to start again from another broker)"
            )
        for adapter in adapters:
            if await adapter.has_instrument(symbol):
                return adapter
        raise InstrumentNotFoundError(
            "+".join(a.broker_name for a in adapters),
            symbol,
            f"no connected broker lists an NSE equity or index named {symbol!r}",
        )

    async def symbol_listed(self, symbol: str) -> bool:
        """Whether any connected broker lists `symbol`. Raises `NoRealDataSourceError` when none is
        connected (the answer is then unknown, not no)."""
        for adapter in self._connected():
            if await adapter.has_instrument(symbol):
                return True
        return False

    # ---- futures ----

    async def futures_contract(
        self, underlying: str, *, today: date, roll_days: int
    ) -> FuturesContract:
        """The monthly future a subscription on `underlying` should trade today (see
        `futures_contracts.select_contract`). Raises `NoRealDataSourceError` with no broker
        connected, `NoFuturesContractError` when the connected ones list nothing usable."""
        return await resolve_contract(
            self._connected(), underlying, today=today, roll_days=roll_days
        )

    async def option_contracts(self, underlying: str) -> list[OptionContract]:
        """The underlying's listed option contracts from the first connected broker that has any.
        Raises `NoRealDataSourceError` with no broker connected; empty if none lists any."""
        for adapter in self._connected():
            try:
                contracts = await adapter.get_option_contracts(underlying)
            except Exception as exc:  # noqa: BLE001 - one broker failing must not stop the next
                logger.warning(
                    "market_data.option_contracts_unavailable",
                    broker=adapter.broker_name,
                    underlying=underlying,
                    error=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
                continue
            if contracts:
                return contracts
        return []

    async def indicative_price(self, symbol: str) -> float | None:
        """A last price for `symbol` from the first connected broker that has one -- for a
        what-would-this-cost check at enrolment, never for a trade. `None` if nobody quotes it."""
        for adapter in self._connected():
            try:
                return (await adapter.get_quote(symbol)).last_price
            except Exception as exc:  # noqa: BLE001 - an indicative price is best effort
                logger.info(
                    "market_data.indicative_price_unavailable",
                    broker=adapter.broker_name,
                    symbol=symbol,
                    error=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
        return None

    # ---- the MarketDataProvider surface (async) ----

    async def daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        adapter = await self.history_adapter(symbol)
        candles = await adapter.get_historical_candles(symbol, "day", start, end)
        frame = daily_frame(candles, adapter.broker_name)
        today = self._clock().astimezone(IST).date()
        if not frame.empty and not session_is_complete(self._clock()):
            # A partial day is not a daily bar.
            frame = frame.loc[frame.index != pd.Timestamp(today)]
        return frame

    async def intraday_minute_bars(self, symbol: str, day: date) -> pd.DataFrame:
        adapter = await self.history_adapter(symbol)
        candles = await adapter.get_historical_candles(symbol, "minute", day, day)
        return minute_frame(candles, adapter.broker_name)

    async def instrument_master(self, symbols: list[str]) -> list[InstrumentRecord]:
        """Equities and indices the connected brokers list. A symbol nobody lists is left out, not
        invented. (Derivatives come with the F&O phase; brokers' lot sizes are read there.)"""
        records: list[InstrumentRecord] = []
        for symbol in symbols:
            if not await self.symbol_listed(symbol):
                logger.warning("market_data.instrument_not_listed", symbol=symbol)
                continue
            is_index = symbol.upper() in INDEX_NAMES or symbol.upper().startswith("NIFTY ")
            records.append(
                InstrumentRecord(
                    symbol=symbol,
                    exchange="NSE",
                    instrument_type="index" if is_index else "equity",
                    isin=None,
                    lot_size=1,
                    tick_size=0.0 if is_index else DEFAULT_EQUITY_TICK_SIZE,
                )
            )
        return records

    async def corporate_actions(self, symbol: str, since: date) -> list[CorporateActionRecord]:
        return []  # brokers publish no corporate-action feed; their candles are already adjusted

    async def market_pulse(self) -> MarketPulseSnapshot:
        raise NoRealDataSourceError(
            "the market pulse (India VIX, sector and global indices) is not sourced from broker "
            "data yet"
        )
