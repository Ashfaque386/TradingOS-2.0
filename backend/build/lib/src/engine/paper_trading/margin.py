"""Approximate F&O Margin Model (Build Spec §11).

**This is explicitly NOT a real SPAN calculation.** NSE's actual SPAN
(Standard Portfolio Analysis of Risk) margin is a scenario-based
methodology run by the exchange's clearing corporation over an entire
portfolio, incorporating implied volatility surfaces, cross-margining
benefits between correlated positions, and exchange-published risk
parameter files that change daily. Reproducing it is out of scope for a
paper-trading engine's own margin *estimate* -- what follows is a coarse,
percentage-of-notional approximation, documented as such everywhere it's
used, never presented to a caller as an authoritative margin figure.

- A long option's margin is the full premium paid (no leverage on the
  buy side, same as a real broker).
- A short option's margin approximates SPAN + exposure margin as a flat
  percentage of the underlying notional, plus the premium received
  (the premium is credited to the account, not a cost, but is included in
  the required blocked margin the same way most brokers over-collateralize
  it).
- A future/equity F&O position's margin approximates SPAN + exposure
  margin as a flat percentage of notional.

All percentages are this module's own configurable defaults, not
regulatory figures -- the same "our own documented modeling choice"
posture as Phase 5's slippage model.
"""

from dataclasses import dataclass

DEFAULT_FUTURES_MARGIN_PCT = 15.0
DEFAULT_SHORT_OPTION_MARGIN_PCT = 12.0


@dataclass(frozen=True, slots=True)
class MarginEstimate:
    approximate_margin: float
    is_span_calculation: bool = False  # always False -- see module docstring


def approximate_option_margin(
    *,
    is_long: bool,
    premium: float,
    quantity: int,
    underlying_price: float | None = None,
    short_option_margin_pct: float = DEFAULT_SHORT_OPTION_MARGIN_PCT,
) -> MarginEstimate:
    if premium < 0 or quantity <= 0:
        raise ValueError("premium must be non-negative and quantity must be positive")

    if is_long:
        return MarginEstimate(approximate_margin=premium * quantity)

    if underlying_price is None or underlying_price <= 0:
        raise ValueError("underlying_price is required (and must be positive) for a short option")

    notional = underlying_price * quantity
    approximate = premium * quantity + notional * (short_option_margin_pct / 100.0)
    return MarginEstimate(approximate_margin=approximate)


def approximate_futures_margin(
    *,
    price: float,
    quantity: int,
    margin_pct: float = DEFAULT_FUTURES_MARGIN_PCT,
) -> MarginEstimate:
    if price <= 0 or quantity <= 0:
        raise ValueError("price and quantity must be positive")

    notional = price * quantity
    return MarginEstimate(approximate_margin=notional * (margin_pct / 100.0))
