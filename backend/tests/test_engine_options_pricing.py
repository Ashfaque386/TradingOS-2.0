"""`src.engine.options_pricing` tests: a wholly new Black-Scholes pricer
and implied-volatility solver, built to close Zerodha's permanent
missing-IV gap (Kite Connect's API has no options-greeks field at all).
No external reference values are hardcoded here -- the solver is
verified the standard way, by round-tripping a known volatility through
`black_scholes_price` and confirming `implied_volatility` recovers it,
plus every genuinely un-computable input honestly returning `None`,
never a guessed number.
"""

import math
from datetime import date

import pytest

from src.engine.options_pricing import black_scholes_price, implied_volatility, year_fraction

_SPOT = 100.0
_STRIKE = 100.0
_TIME = 0.5
_RATE = 0.07


@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("true_vol", [0.10, 0.15, 0.25, 0.40, 0.75, 1.5])
def test_implied_volatility_recovers_a_known_volatility_round_trip(option_type, true_vol):
    price = black_scholes_price(
        option_type=option_type,
        spot=_SPOT,
        strike=_STRIKE,
        time_to_expiry=_TIME,
        rate=_RATE,
        vol=true_vol,
    )
    solved = implied_volatility(
        option_type=option_type,
        market_price=price,
        spot=_SPOT,
        strike=_STRIKE,
        time_to_expiry=_TIME,
        rate=_RATE,
    )
    assert solved is not None
    assert solved == pytest.approx(true_vol, abs=1e-4)


@pytest.mark.parametrize(
    "strike,true_vol",
    [(80.0, 0.2), (120.0, 0.2), (100.0, 0.05), (100.0, 3.0)],
)
def test_implied_volatility_recovers_across_moneyness_and_extreme_vols(strike, true_vol):
    price = black_scholes_price(
        option_type="CE", spot=_SPOT, strike=strike, time_to_expiry=_TIME, rate=_RATE, vol=true_vol
    )
    solved = implied_volatility(
        option_type="CE",
        market_price=price,
        spot=_SPOT,
        strike=strike,
        time_to_expiry=_TIME,
        rate=_RATE,
    )
    assert solved is not None
    assert solved == pytest.approx(true_vol, abs=1e-3)


def test_black_scholes_call_put_parity_holds():
    """A real, independent correctness check beyond the round-trip:
    call - put == spot - strike * e^(-rT), the textbook put-call parity
    identity, for any volatility."""
    call = black_scholes_price(
        option_type="CE", spot=_SPOT, strike=_STRIKE, time_to_expiry=_TIME, rate=_RATE, vol=0.3
    )
    put = black_scholes_price(
        option_type="PE", spot=_SPOT, strike=_STRIKE, time_to_expiry=_TIME, rate=_RATE, vol=0.3
    )
    expected = _SPOT - _STRIKE * math.exp(-_RATE * _TIME)
    assert (call - put) == pytest.approx(expected, abs=1e-8)


def test_implied_volatility_is_none_for_zero_or_negative_price():
    assert (
        implied_volatility(
            option_type="CE",
            market_price=0.0,
            spot=_SPOT,
            strike=_STRIKE,
            time_to_expiry=_TIME,
            rate=_RATE,
        )
        is None
    )
    assert (
        implied_volatility(
            option_type="CE",
            market_price=-5.0,
            spot=_SPOT,
            strike=_STRIKE,
            time_to_expiry=_TIME,
            rate=_RATE,
        )
        is None
    )


def test_implied_volatility_is_none_for_zero_or_negative_time_to_expiry():
    assert (
        implied_volatility(
            option_type="CE",
            market_price=5.0,
            spot=_SPOT,
            strike=_STRIKE,
            time_to_expiry=0.0,
            rate=_RATE,
        )
        is None
    )
    assert (
        implied_volatility(
            option_type="CE",
            market_price=5.0,
            spot=_SPOT,
            strike=_STRIKE,
            time_to_expiry=-0.1,
            rate=_RATE,
        )
        is None
    )


def test_implied_volatility_is_none_for_a_price_below_intrinsic_value():
    # A deep-ITM call (spot 200, strike 100) priced at 0.5 is below its
    # own intrinsic value (100) -- not consistent with ANY volatility
    # under Black-Scholes, so this must be honestly None, never a
    # nonsensical or clamped answer.
    assert (
        implied_volatility(
            option_type="CE",
            market_price=0.5,
            spot=200.0,
            strike=100.0,
            time_to_expiry=_TIME,
            rate=_RATE,
        )
        is None
    )


def test_implied_volatility_is_none_for_an_unknown_option_type():
    assert (
        implied_volatility(
            option_type="XX",
            market_price=5.0,
            spot=_SPOT,
            strike=_STRIKE,
            time_to_expiry=_TIME,
            rate=_RATE,
        )
        is None
    )


def test_year_fraction_is_zero_on_the_expiry_date_itself():
    day = date(2026, 6, 15)
    assert year_fraction(day, day) == 0.0


def test_year_fraction_is_negative_for_an_already_elapsed_expiry():
    assert year_fraction(date(2026, 1, 1), date(2026, 6, 1)) < 0.0


def test_year_fraction_is_a_real_calendar_day_over_365_count():
    assert year_fraction(date(2027, 1, 1), date(2026, 1, 1)) == pytest.approx(365 / 365, abs=1e-6)
