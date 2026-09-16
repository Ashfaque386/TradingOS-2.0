"""Approximate F&O margin model tests (Build Spec §11): every result is
explicitly flagged as not a real SPAN calculation.
"""

import pytest

from src.engine.paper_trading.margin import approximate_futures_margin, approximate_option_margin


def test_long_option_margin_is_full_premium():
    result = approximate_option_margin(is_long=True, premium=50.0, quantity=50)
    assert result.approximate_margin == 2500.0
    assert result.is_span_calculation is False


def test_short_option_margin_is_premium_plus_notional_pct():
    result = approximate_option_margin(
        is_long=False,
        premium=50.0,
        quantity=50,
        underlying_price=20_000,
        short_option_margin_pct=12.0,
    )
    # notional = 20000*50 = 1,000,000; 12% = 120,000; + premium 2,500
    assert result.approximate_margin == pytest.approx(122_500.0)
    assert result.is_span_calculation is False


def test_short_option_requires_underlying_price():
    with pytest.raises(ValueError):
        approximate_option_margin(is_long=False, premium=50.0, quantity=50)


def test_futures_margin_is_a_flat_pct_of_notional():
    result = approximate_futures_margin(price=20_000, quantity=50, margin_pct=15.0)
    assert result.approximate_margin == pytest.approx(150_000.0)
    assert result.is_span_calculation is False


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        approximate_option_margin(is_long=True, premium=-1, quantity=50)
    with pytest.raises(ValueError):
        approximate_futures_margin(price=0, quantity=50)
