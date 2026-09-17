from datetime import date

from src.data.nse_calendar import is_nse_trading_day, previous_nse_trading_day


def test_weekend_is_never_a_trading_day():
    assert is_nse_trading_day(date(2026, 9, 19)) is False  # Saturday
    assert is_nse_trading_day(date(2026, 9, 20)) is False  # Sunday


def test_ordinary_weekday_is_a_trading_day():
    assert is_nse_trading_day(date(2026, 9, 16)) is True  # Wednesday


def test_fixed_date_holiday_is_never_a_trading_day():
    assert is_nse_trading_day(date(2026, 8, 15)) is False  # Independence Day
    assert is_nse_trading_day(date(2026, 1, 26)) is False  # Republic Day


def test_extra_holiday_override_excludes_that_date():
    extra = frozenset({date(2026, 9, 16)})
    assert is_nse_trading_day(date(2026, 9, 16), extra_holidays=extra) is False
    assert is_nse_trading_day(date(2026, 9, 16), extra_holidays=frozenset()) is True


def test_previous_nse_trading_day_skips_weekend():
    # Monday 2026-09-21 -> previous trading day is Friday 2026-09-18
    assert previous_nse_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)


def test_previous_nse_trading_day_skips_fixed_holiday_and_weekend():
    # 2026-08-17 is a Monday; 2026-08-15 (Sat) is Independence Day AND a
    # weekend already, 2026-08-16 is Sunday -- previous trading day is
    # Friday 2026-08-14.
    assert previous_nse_trading_day(date(2026, 8, 17)) == date(2026, 8, 14)
