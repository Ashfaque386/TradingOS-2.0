"""European-option Black-Scholes pricing and implied-volatility solving
-- a wholly new capability, built specifically to close a permanent gap
in Zerodha's live option chain (`src.brokers.zerodha.ZerodhaKiteAdapter.
get_option_chain`): Kite Connect's `/quote` response has no options-
greeks field of any kind, so `call_iv`/`put_iv` are always `None` for
that broker (see docs/phase17-realworld-testing.md Part 2). Upstox, by
contrast, already returns a real broker-reported IV and never needs
this module -- computing one here would only ever be used to fill a gap
a broker's own API genuinely cannot answer, never to override or
second-guess a real value.

Dependency-free by design: a hand-rolled normal CDF via `math.erf`
rather than adding `scipy` for one solver, keeping this build's existing
numeric-dependency footprint (numpy/pandas/scikit-learn) unchanged.
`implied_volatility` solves by bisection, not Newton-Raphson -- the
Black-Scholes price of a call or put is strictly increasing in
volatility (for a positive time to expiry), so bisection over a fixed
bracket is always well-behaved and needs no derivative (vega)
computation or special-casing near-zero vega, at the cost of a few more
iterations than Newton would need -- an irrelevant cost here: this runs
per option-chain request, over at most a few dozen strikes, not in a
hot loop.

**Every genuinely un-computable case returns `None`, never a guessed
number**: a non-positive price/spot/strike, a non-positive time to
expiry (the expiry has already passed, or is today), a market price
that cannot correspond to any volatility in the solver's sane bracket,
or a solve that fails to converge within its iteration budget. The
caller (`src.api.routes.market_data`) is responsible for labeling
whatever it gets back as a *computed* estimate, never as if it were a
real broker-reported figure -- this module has no opinion on that
labeling, it only ever returns a number or `None`.
"""

import math
from datetime import date

_MAX_ITERATIONS = 100
_PRICE_TOLERANCE = 1e-6
_MIN_IV = 0.001  # 0.1% -- the solver's search floor, not a returned default
_MAX_IV = 5.0  # 500% -- Black-Scholes stops being a sane model well before this


def year_fraction(expiry: date, as_of: date) -> float:
    """Calendar-day/365 year fraction -- a standard, simple day-count
    convention, not an exchange-precise trading-day count (NSE F&O
    publishes no single universally-used convention for this, and
    inventing false precision around it would be worse than a simple,
    clearly-documented approximation). Zero or negative when `expiry`
    is on or before `as_of` -- the caller treats that as "no
    forward-looking IV can be computed," never as zero volatility."""
    return (expiry - as_of).days / 365.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(
    *, spot: float, strike: float, time_to_expiry: float, rate: float, vol: float
) -> tuple[float, float]:
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * time_to_expiry) / (
        vol * math.sqrt(time_to_expiry)
    )
    d2 = d1 - vol * math.sqrt(time_to_expiry)
    return d1, d2


def black_scholes_price(
    *,
    option_type: str,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    vol: float,
) -> float:
    """The real Black-Scholes-Merton closed-form European option price.
    `option_type` is `"CE"`/`"PE"`, matching this codebase's own
    `OptionChainEntry`/`Instrument` convention (not the "call"/"put"
    Black-Scholes literature usually spells out) -- callers pass this
    codebase's own option-type strings straight through, no
    translation layer needed."""
    d1, d2 = _d1_d2(spot=spot, strike=strike, time_to_expiry=time_to_expiry, rate=rate, vol=vol)
    discount = math.exp(-rate * time_to_expiry)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
    *,
    option_type: str,
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
) -> float | None:
    """Solves for the volatility that makes `black_scholes_price` match
    `market_price`, or `None` when that genuinely can't be done -- see
    this module's docstring for the full list of un-computable cases."""
    if option_type not in ("CE", "PE"):
        return None
    if market_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry <= 0:
        return None

    def price_at(vol: float) -> float:
        return black_scholes_price(
            option_type=option_type,
            spot=spot,
            strike=strike,
            time_to_expiry=time_to_expiry,
            rate=rate,
            vol=vol,
        )

    low, high = _MIN_IV, _MAX_IV
    diff_low = price_at(low) - market_price
    diff_high = price_at(high) - market_price
    if diff_low > 0 or diff_high < 0:
        # market_price falls outside what any volatility in [_MIN_IV,
        # _MAX_IV] can produce for this spot/strike/time-to-expiry --
        # honestly uncomputable, not clamped to the nearest bound.
        return None

    for _ in range(_MAX_ITERATIONS):
        mid = (low + high) / 2
        diff = price_at(mid) - market_price
        if abs(diff) < _PRICE_TOLERANCE:
            return mid
        if diff > 0:
            high = mid
        else:
            low = mid
    return None  # did not converge within the iteration budget
