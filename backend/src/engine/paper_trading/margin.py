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


@dataclass(frozen=True, slots=True)
class MarginCheck:
    ok: bool
    required: float  # the approximate margin the position needs
    limit: float  # the most of the account's capital a paper position may use
    reason: str | None = None


def check_futures_margin(
    *,
    price: float,
    quantity: int,
    capital: float,
    margin_pct: float = DEFAULT_FUTURES_MARGIN_PCT,
    max_utilisation_pct: float = 50.0,
) -> MarginCheck:
    """Whether an entry fits the account. A future is bought on margin, so the notional-versus-
    capital position limit (built for cash equity: an index future's notional is many times a
    small account) is the wrong yardstick; the committed margin is the one that counts, and it
    must stay within `max_utilisation_pct` of capital. The margin is an estimate (see the module
    docstring) -- the reason says so."""
    required = approximate_futures_margin(
        price=price, quantity=quantity, margin_pct=margin_pct
    ).approximate_margin
    limit = capital * max_utilisation_pct / 100.0
    if required <= limit:
        return MarginCheck(ok=True, required=required, limit=limit)
    return MarginCheck(
        ok=False,
        required=required,
        limit=limit,
        reason=(
            f"estimated margin Rs {required:,.0f} ({margin_pct:g}% of Rs {price * quantity:,.0f} "
            f"notional; an approximation, not SPAN) is above the Rs {limit:,.0f} "
            f"({max_utilisation_pct:g}% of Rs {capital:,.0f} capital) a paper position may use"
        ),
    )


def check_max_loss_margin(
    *, max_loss: float, capital: float, max_utilisation_pct: float = 50.0
) -> MarginCheck:
    """Whether a defined-risk option position fits the account. Its largest possible loss (the
    premium of a bought option; the width less the credit of a credit spread) is the capital it
    commits, and must be within `max_utilisation_pct` of capital. It is what a broker blocks for a
    defined-risk spread, give or take: the reason says it is the largest possible loss."""
    limit = capital * max_utilisation_pct / 100.0
    if max_loss <= limit:
        return MarginCheck(ok=True, required=max_loss, limit=limit)
    return MarginCheck(
        ok=False,
        required=max_loss,
        limit=limit,
        reason=(
            f"the Rs {max_loss:,.0f} this position could lose at most (its largest possible "
            f"loss) is above the Rs {limit:,.0f} ({max_utilisation_pct:g}% of Rs {capital:,.0f} "
            "capital) a paper position may use"
        ),
    )
