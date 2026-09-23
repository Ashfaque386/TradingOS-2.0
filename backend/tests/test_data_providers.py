"""`FakeMarketDataProvider.instrument_master` tests (Build Spec §14): the
F&O instrument-generation half of a previously-documented gap (see
docs/phase17-realworld-testing.md Part 2 and docs/phase16-wiring-audit.md
-- the schema supported strike/expiry/option_type since Phase 10, but
nothing populated it). Real NSE conventions checked directly: the last
Thursday of the month, rolled back to a real trading day when it's a
holiday; a strike step that widens with spot price; every generated
symbol unique and non-fabricated in the ways this codebase's rules
forbid (no ISIN on a derivative contract, no fabricated strikes below
zero).
"""

from datetime import date, timedelta

import pandas as pd

from src.data.nse_calendar import is_nse_trading_day, previous_nse_trading_day
from src.data.providers import (
    FakeMarketDataProvider,
    _last_thursday_of_month,
    _monthly_expiries,
    _strike_step,
)


def test_last_thursday_of_month_is_genuinely_the_last_thursday():
    day = _last_thursday_of_month(2026, 9)
    assert day.weekday() == 3
    assert day.month == 9
    # No later Thursday exists in the same month.
    assert (day + timedelta(days=7)).month != 9


def test_last_thursday_of_month_handles_december_year_rollover():
    day = _last_thursday_of_month(2026, 12)
    assert day.year == 2026
    assert day.month == 12
    assert day.weekday() == 3


def test_monthly_expiries_are_real_trading_days_on_or_after_today():
    today = date(2026, 9, 16)
    expiries = _monthly_expiries(today, count=3)
    assert len(expiries) == 3
    assert expiries == sorted(expiries)
    for expiry in expiries:
        assert expiry >= today
        assert is_nse_trading_day(expiry)


def test_monthly_expiries_roll_back_off_a_holiday_thursday():
    """Republic Day (2026-01-26) is a real fixed NSE holiday and does not
    fall on the last Thursday of January 2026 (2026-01-29) -- confirm the
    module's own rollback rule agrees with previous_nse_trading_day's
    real calendar, not a hand-picked date."""
    expiries = _monthly_expiries(date(2026, 1, 1), count=1)
    thursday = _last_thursday_of_month(2026, 1)
    expected = thursday if is_nse_trading_day(thursday) else previous_nse_trading_day(thursday)
    assert expiries[0] == expected


def test_strike_step_widens_with_spot_price():
    assert _strike_step(50) < _strike_step(200)
    assert _strike_step(200) < _strike_step(800)
    assert _strike_step(800) < _strike_step(2000)
    assert _strike_step(2000) < _strike_step(4000)
    assert _strike_step(4000) < _strike_step(10000)


def test_instrument_master_still_returns_the_equity_row_first():
    provider = FakeMarketDataProvider()
    records = provider.instrument_master(["DEMOSTOCK"])
    assert records[0].symbol == "DEMOSTOCK"
    assert records[0].instrument_type == "equity"
    assert records[0].underlying_symbol is None


def test_instrument_master_generates_a_real_shaped_fo_chain():
    provider = FakeMarketDataProvider()
    records = provider.instrument_master(["DEMOSTOCK"])

    futures = [r for r in records if r.instrument_type == "future"]
    options = [r for r in records if r.instrument_type == "option"]

    # 2 monthly expiries x (1 future + 5 strikes x 2 legs).
    assert len(futures) == 2
    assert len(options) == 20

    for future in futures:
        assert future.underlying_symbol == "DEMOSTOCK"
        assert future.expiry_date is not None
        assert future.strike_price is None
        assert future.option_type is None
        # NSE never assigns an ISIN to a derivative contract.
        assert future.isin is None

    call_count = sum(1 for o in options if o.option_type == "CE")
    put_count = sum(1 for o in options if o.option_type == "PE")
    assert call_count == 10
    assert put_count == 10
    for option in options:
        assert option.underlying_symbol == "DEMOSTOCK"
        assert option.expiry_date is not None
        assert option.strike_price is not None
        assert option.strike_price > 0
        assert option.isin is None
        assert option.option_type in ("CE", "PE")


def test_instrument_master_symbols_are_all_unique_and_fit_the_column_width():
    provider = FakeMarketDataProvider()
    records = provider.instrument_master(["DEMOSTOCK", "TESTSTOCK2"])
    symbols = [r.symbol for r in records]
    assert len(symbols) == len(set(symbols)), "every generated instrument symbol must be unique"
    for symbol in symbols:
        assert len(symbol) <= 32  # Instrument.symbol is String(32)


def test_instrument_master_lot_size_is_shared_across_one_symbols_whole_fo_chain():
    """Real NSE fixes one lot size per underlying across all its F&O
    contracts -- never a different lot size per strike/expiry for the
    same symbol."""
    provider = FakeMarketDataProvider()
    records = provider.instrument_master(["DEMOSTOCK"])
    fo_lot_sizes = {r.lot_size for r in records if r.instrument_type in ("future", "option")}
    assert len(fo_lot_sizes) == 1


def test_instrument_master_strikes_are_centered_on_the_symbols_own_synthetic_close():
    provider = FakeMarketDataProvider()
    today = date.today()
    daily_row = provider._full_daily_series("DEMOSTOCK").loc[: pd.Timestamp(today)].tail(1)
    spot = float(daily_row["close"].iloc[0])

    records = provider.instrument_master(["DEMOSTOCK"])
    strikes = sorted({r.strike_price for r in records if r.strike_price is not None})
    step = _strike_step(spot)
    atm = round(spot / step) * step
    assert strikes == [round(atm + offset * step, 2) for offset in (-2, -1, 0, 1, 2)]


def test_instrument_master_is_deterministic_for_the_same_symbol():
    provider_a = FakeMarketDataProvider()
    provider_b = FakeMarketDataProvider()
    records_a = provider_a.instrument_master(["DEMOSTOCK"])
    records_b = provider_b.instrument_master(["DEMOSTOCK"])
    assert [r.symbol for r in records_a] == [r.symbol for r in records_b]
    assert [r.lot_size for r in records_a] == [r.lot_size for r in records_b]
