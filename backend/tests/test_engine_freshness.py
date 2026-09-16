from datetime import date

from src.engine.backtest.freshness import (
    FakeDataLakeFreshness,
    check_data_freshness,
    previous_trading_day,
)


def test_previous_trading_day_skips_weekend():
    # Monday 2024-01-08 -> previous trading day is Friday 2024-01-05.
    assert previous_trading_day(date(2024, 1, 8)) == date(2024, 1, 5)


def test_previous_trading_day_regular_weekday():
    assert previous_trading_day(date(2024, 1, 4)) == date(2024, 1, 3)


def test_freshness_gate_refuses_when_prior_day_missing():
    data_lake = FakeDataLakeFreshness(available_dates={"NIFTY": frozenset()})
    result = check_data_freshness(symbol="NIFTY", as_of=date(2024, 1, 4), data_lake=data_lake)

    assert result.fresh is False
    assert result.required_date == date(2024, 1, 3)
    assert "NIFTY" in result.reason
    assert "2024-01-03" in result.reason


def test_freshness_gate_passes_when_prior_day_present():
    data_lake = FakeDataLakeFreshness(available_dates={"NIFTY": frozenset({date(2024, 1, 3)})})
    result = check_data_freshness(symbol="NIFTY", as_of=date(2024, 1, 4), data_lake=data_lake)

    assert result.fresh is True
    assert result.reason is None


def test_freshness_gate_is_symbol_specific():
    data_lake = FakeDataLakeFreshness(available_dates={"NIFTY": frozenset({date(2024, 1, 3)})})
    result = check_data_freshness(symbol="BANKNIFTY", as_of=date(2024, 1, 4), data_lake=data_lake)

    assert result.fresh is False
