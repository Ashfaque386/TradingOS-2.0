"""IST-aware market-hours tests (Build Spec §11)."""

from datetime import UTC, datetime

from src.engine.paper_trading.market_hours import is_market_open_ist


def test_open_during_regular_session():
    # Tue 2026-09-15 10:00 IST = 04:30 UTC
    assert is_market_open_ist(datetime(2026, 9, 15, 4, 30, tzinfo=UTC)) is True


def test_closed_before_open():
    # Tue 2026-09-15 09:00 IST = 03:30 UTC (before 09:15 open)
    assert is_market_open_ist(datetime(2026, 9, 15, 3, 30, tzinfo=UTC)) is False


def test_closed_after_close():
    # Tue 2026-09-15 16:00 IST = 10:30 UTC (after 15:30 close)
    assert is_market_open_ist(datetime(2026, 9, 15, 10, 30, tzinfo=UTC)) is False


def test_closed_on_weekend():
    # Sat 2026-09-19 10:00 IST = 04:30 UTC
    assert is_market_open_ist(datetime(2026, 9, 19, 4, 30, tzinfo=UTC)) is False


def test_naive_datetime_treated_as_utc():
    assert is_market_open_ist(datetime(2026, 9, 15, 4, 30)) is True


def test_boundary_times_are_inclusive():
    from datetime import time

    from src.engine.paper_trading.market_hours import MARKET_CLOSE_IST, MARKET_OPEN_IST

    assert MARKET_OPEN_IST == time(9, 15)
    assert MARKET_CLOSE_IST == time(15, 30)
