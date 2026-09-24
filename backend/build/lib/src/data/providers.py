"""`MarketDataProvider` Protocol (Build Spec §14): the single abstraction
every ingestion pipeline in `src.orchestration.market_data` reads from --
daily OHLCV, intraday minute bars, the instrument master, corporate
actions, and an on-demand Market Pulse snapshot (India VIX, sector-index
and global-index day-change).

`FakeMarketDataProvider` is the deterministic, seeded stand-in used by
every pipeline in this sandbox, which has no real market-data vendor
integration and no egress to one -- the same honest-stub posture as every
other not-yet-integrated external data source in this codebase (Phase 7's
`FakeDailyPriceProvider`, Phase 6's `FakeNiftyBenchmarkProvider`). Unlike
those, this phase's ingestion *pipeline* (scheduling, partitioned storage,
freshness tracking, backup) is fully real -- only the underlying data
*source* remains synthetic pending a real vendor/broker historical-data
integration. The one exception is `src.data.bhavcopy`, which genuinely
attempts a real network fetch against NSE's public Bhavcopy archive before
falling back to this same synthetic generator.
"""

import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

import numpy as np
import pandas as pd

from src.data.nse_calendar import previous_nse_trading_day


def _stable_seed_for(*parts: str) -> int:
    """Deterministic, process-stable seed (crc32, not Python's salted
    `hash()`) -- see Phase 7's `price_data._stable_seed_for` for why."""
    return zlib.crc32("|".join(parts).encode("utf-8")) % 1_000_000


def _last_thursday_of_month(year: int, month: int) -> date:
    first_of_next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    day = first_of_next_month - timedelta(days=1)
    while day.weekday() != 3:  # Monday=0 ... Thursday=3
        day -= timedelta(days=1)
    return day


def _monthly_expiries(today: date, *, count: int = 2) -> list[date]:
    """Real NSE monthly F&O expiry convention: the last Thursday of the
    month, rolled back to the nearest actual trading day
    (`previous_nse_trading_day`) when that Thursday is itself a holiday
    -- the same real rule NSE applies, never an invented date. Returns
    the next `count` such expiries on or after `today`, skipping an
    already-elapsed current-month expiry."""
    expiries: list[date] = []
    year, month = today.year, today.month
    while len(expiries) < count:
        thursday = _last_thursday_of_month(year, month)
        candidate = previous_nse_trading_day(thursday + timedelta(days=1))
        if candidate >= today:
            expiries.append(candidate)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return expiries


def _strike_step(spot: float) -> float:
    """A realistic NSE strike-price increment for the given spot price --
    real exchanges use a coarser step for higher-priced underlyings, not
    one fixed increment for every stock."""
    if spot < 100:
        return 2.5
    if spot < 250:
        return 5.0
    if spot < 1000:
        return 10.0
    if spot < 2500:
        return 20.0
    if spot < 5000:
        return 50.0
    return 100.0


# Real, actually-used NSE lot-size values -- a deterministic pick from
# this set per underlying (NSE fixes one lot size per underlying across
# all its F&O contracts, not a fresh one per strike/expiry).
_REALISTIC_LOT_SIZES = (25, 50, 75, 100, 150, 200, 250, 300, 500, 1000)


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    symbol: str
    exchange: str
    instrument_type: str
    isin: str | None
    lot_size: int
    tick_size: float
    underlying_symbol: str | None = None
    expiry_date: date | None = None
    strike_price: float | None = None
    option_type: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    symbol: str
    action_type: str  # dividend | split | bonus | rights
    ex_date: date
    ratio: float | None
    amount: float | None
    announced_at: datetime


@dataclass(frozen=True, slots=True)
class MarketPulseSnapshot:
    as_of: datetime
    india_vix: float
    sector_indices_change_pct: dict[str, float]
    global_indices_change_pct: dict[str, float]


class MarketDataProvider(Protocol):
    def daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...

    def intraday_minute_bars(self, symbol: str, day: date) -> pd.DataFrame: ...

    def instrument_master(self, symbols: list[str]) -> list[InstrumentRecord]: ...

    def corporate_actions(self, symbol: str, since: date) -> list[CorporateActionRecord]: ...

    def market_pulse(self) -> MarketPulseSnapshot: ...


_SERIES_START = pd.Timestamp("2020-01-01")
_SERIES_END = pd.Timestamp("2035-12-31")

_SECTOR_INDICES = ["NIFTY_BANK", "NIFTY_IT", "NIFTY_AUTO", "NIFTY_PHARMA", "NIFTY_FMCG"]
_GLOBAL_INDICES = ["SP500", "NASDAQ", "DOWJONES", "NIKKEI225", "HANGSENG"]


@dataclass(frozen=True, slots=True)
class FakeMarketDataProvider:
    daily_mean: float = 0.0005
    daily_vol: float = 0.012
    start_price: float = 100.0
    _daily_series_cache: dict[str, pd.DataFrame] = field(
        default_factory=dict, init=False, repr=False
    )

    def _full_daily_series(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._daily_series_cache:
            seed = _stable_seed_for("daily", symbol)
            rng = np.random.default_rng(seed)
            idx = pd.bdate_range(start=_SERIES_START, end=_SERIES_END)
            returns = rng.normal(self.daily_mean, self.daily_vol, len(idx))
            close = self.start_price * np.cumprod(1 + returns)
            self._daily_series_cache[symbol] = pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": rng.integers(10_000, 50_000, len(idx)),
                },
                index=idx,
            )
        return self._daily_series_cache[symbol]

    def daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        series = self._full_daily_series(symbol)
        return series.loc[pd.Timestamp(start) : pd.Timestamp(end)]

    def intraday_minute_bars(self, symbol: str, day: date) -> pd.DataFrame:
        seed = _stable_seed_for("intraday", symbol, day.isoformat())
        rng = np.random.default_rng(seed)
        idx = pd.date_range(
            start=datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=15),
            end=datetime.combine(day, datetime.min.time()) + timedelta(hours=15, minutes=30),
            freq="1min",
        )
        daily_row = self._full_daily_series(symbol).loc[: pd.Timestamp(day)].tail(1)
        base_price = float(daily_row["close"].iloc[0]) if not daily_row.empty else self.start_price
        returns = rng.normal(0.0, 0.0008, len(idx))
        close = base_price * np.cumprod(1 + returns)
        return pd.DataFrame(
            {
                "open": close * 0.9995,
                "high": close * 1.0015,
                "low": close * 0.9985,
                "close": close,
                "volume": rng.integers(100, 2_000, len(idx)),
            },
            index=idx,
        )

    def instrument_master(self, symbols: list[str]) -> list[InstrumentRecord]:
        """Real NSE F&O instrument-master shape (previously a documented
        gap -- see docs/phase17-realworld-testing.md Part 2 -- the schema
        supported it since Phase 10, but nothing populated it): each
        equity also gets a monthly futures contract plus a 5-strike
        option chain (CE/PE) per upcoming monthly expiry, strikes
        centered on this symbol's own latest synthetic close (the same
        real anchor `intraday_minute_bars` already uses for its own base
        price) rather than an arbitrary fixed price. `isin` stays `None`
        for F&O rows -- NSE genuinely never assigns one to a derivative
        contract, only to the underlying security -- never a fabricated
        one."""
        records = []
        today = date.today()
        expiries = _monthly_expiries(today)
        for symbol in symbols:
            seed = _stable_seed_for("instrument", symbol)
            rng = np.random.default_rng(seed)
            records.append(
                InstrumentRecord(
                    symbol=symbol,
                    exchange="NSE",
                    instrument_type="equity",
                    isin=f"INE{seed % 1_000_000:06d}",
                    lot_size=1,
                    tick_size=0.05,
                    is_active=True,
                )
            )

            daily_row = self._full_daily_series(symbol).loc[: pd.Timestamp(today)].tail(1)
            spot = float(daily_row["close"].iloc[0]) if not daily_row.empty else self.start_price
            step = _strike_step(spot)
            atm_strike = round(spot / step) * step
            lot_size = int(rng.choice(_REALISTIC_LOT_SIZES))

            for expiry in expiries:
                month_code = expiry.strftime("%y%b").upper()
                records.append(
                    InstrumentRecord(
                        symbol=f"{symbol}{month_code}FUT",
                        exchange="NFO",
                        instrument_type="future",
                        isin=None,
                        lot_size=lot_size,
                        tick_size=0.05,
                        underlying_symbol=symbol,
                        expiry_date=expiry,
                    )
                )
                for offset in (-2, -1, 0, 1, 2):
                    strike = round(atm_strike + offset * step, 2)
                    if strike <= 0:
                        continue
                    for option_type in ("CE", "PE"):
                        records.append(
                            InstrumentRecord(
                                symbol=f"{symbol}{month_code}{strike:g}{option_type}",
                                exchange="NFO",
                                instrument_type="option",
                                isin=None,
                                lot_size=lot_size,
                                tick_size=0.05,
                                underlying_symbol=symbol,
                                expiry_date=expiry,
                                strike_price=strike,
                                option_type=option_type,
                            )
                        )
        return records

    def corporate_actions(self, symbol: str, since: date) -> list[CorporateActionRecord]:
        seed = _stable_seed_for("corp_action", symbol, since.isoformat())
        rng = np.random.default_rng(seed)
        if rng.random() >= 0.3:
            return []
        # A single deterministic dividend roughly a quarter after `since`,
        # only if that date has actually arrived.
        ex_date = since + timedelta(days=int(rng.integers(30, 120)))
        if ex_date > date.today():
            return []
        return [
            CorporateActionRecord(
                symbol=symbol,
                action_type="dividend",
                ex_date=ex_date,
                ratio=None,
                amount=round(float(rng.uniform(1.0, 15.0)), 2),
                announced_at=datetime.combine(ex_date - timedelta(days=7), datetime.min.time()),
            )
        ]

    def market_pulse(self) -> MarketPulseSnapshot:
        now = datetime.now()
        today = now.date()
        yesterday = today - timedelta(days=1)

        def _day_change_pct(symbol: str) -> float:
            series = self._full_daily_series(symbol)
            window = series.loc[: pd.Timestamp(today)].tail(2)
            if len(window) < 2:
                return 0.0
            prev_close, last_close = window["close"].iloc[0], window["close"].iloc[1]
            return round(((last_close - prev_close) / prev_close) * 100, 3)

        vix_series = self._full_daily_series("INDIAVIX")
        vix_row = vix_series.loc[: pd.Timestamp(today)].tail(1)
        india_vix = float(vix_row["close"].iloc[0]) if not vix_row.empty else 15.0
        _ = yesterday

        return MarketPulseSnapshot(
            as_of=now,
            india_vix=round(india_vix % 40 + 10, 2),
            sector_indices_change_pct={s: _day_change_pct(s) for s in _SECTOR_INDICES},
            global_indices_change_pct={s: _day_change_pct(s) for s in _GLOBAL_INDICES},
        )
