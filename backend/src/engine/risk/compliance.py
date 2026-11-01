"""Compliance Checker (Build Spec §8): a house single-position limit and a
circuit-filter band, both deterministic and independent of any
LLM output -- nothing here ever asks a model anything; every verdict is
arithmetic against a regulatory data source.

**Two different kinds of limit, said as such.** The position limit is a concentration rule of this
system (no single position above a share of the portfolio); it is not an exchange or SEBI limit, and
its reason says so. The circuit band is the exchange's: when the quote that priced the order carried
the exchange's own lower and upper circuit limits for the day (`ExchangeCircuitLimits`), those are
what is checked. Only without them does the check fall back to the reference table's percentage
band against a reference price, and its reason says that it did.

**Swappable regulatory data source**: `RegulatoryDataProvider` is a
`Protocol` (the same swap-later posture as
`src.engine.backtest.freshness.DataLakeFreshnessCheck` from Phase 5) --
`ReferenceTableRegulatoryDataProvider` below is a maintained, deterministic
reference table standing in for a real regulatory data feed (the genuine
NSE/SEBI feed is a Phase 10 data-ingestion concern). Any later real
provider just implements the same two methods; no call site or schema
changes when it's swapped in.

A malformed *input* (a non-positive portfolio or reference price) raises
`ValueError` -- that is a caller bug, not a business-rule outcome. An
actual regulatory breach is never an exception; it is a `blocked=True`
result with a human-readable reason, the same shape callers combine with
the naked-options scan and correlation constraint in
`src.orchestration.risk_gate`.
"""

from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_POSITION_LIMIT_PCT = 10.0
DEFAULT_CIRCUIT_BAND_PCT = 20.0


@dataclass(frozen=True, slots=True)
class ExchangeCircuitLimits:
    """The exchange's price band for the day, as the broker's quote carried it: no order is accepted
    at or beyond either end."""

    lower: float
    upper: float


class RegulatoryDataProvider(Protocol):
    def position_limit_pct(self, symbol: str) -> float: ...
    def circuit_filter_band_pct(self, symbol: str) -> float: ...


@dataclass(frozen=True, slots=True)
class ReferenceTableRegulatoryDataProvider:
    """Illustrative, not exhaustive -- a symbol absent from either table
    falls back to its `default_*_pct`."""

    position_limits_pct: dict[str, float] = field(default_factory=dict)
    circuit_bands_pct: dict[str, float] = field(
        default_factory=lambda: {"NIFTY": 10.0, "BANKNIFTY": 10.0}
    )
    default_position_limit_pct: float = DEFAULT_POSITION_LIMIT_PCT
    default_circuit_band_pct: float = DEFAULT_CIRCUIT_BAND_PCT

    def position_limit_pct(self, symbol: str) -> float:
        return self.position_limits_pct.get(symbol.upper(), self.default_position_limit_pct)

    def circuit_filter_band_pct(self, symbol: str) -> float:
        return self.circuit_bands_pct.get(symbol.upper(), self.default_circuit_band_pct)


@dataclass(frozen=True, slots=True)
class ComplianceCheckResult:
    blocked: bool
    reasons: list[str] = field(default_factory=list)


def check_position_limit(
    *,
    symbol: str,
    proposed_position_value: float,
    portfolio_value: float,
    provider: RegulatoryDataProvider,
) -> ComplianceCheckResult:
    """House single-position concentration limit: a proposed position
    may not exceed `provider.position_limit_pct(symbol)` percent of total
    portfolio value."""
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be positive")

    limit_pct = provider.position_limit_pct(symbol)
    proposed_pct = (proposed_position_value / portfolio_value) * 100.0
    if proposed_pct > limit_pct:
        return ComplianceCheckResult(
            blocked=True,
            reasons=[
                f"{symbol}: proposed position {proposed_pct:.2f}% of portfolio exceeds "
                f"the house single-position limit {limit_pct:.2f}% (a concentration rule of this "
                "system, not an exchange or SEBI limit)"
            ],
        )
    return ComplianceCheckResult(blocked=False)


def check_circuit_filter_band(
    *,
    symbol: str,
    proposed_price: float,
    reference_price: float,
    provider: RegulatoryDataProvider,
    exchange_limits: ExchangeCircuitLimits | None = None,
) -> ComplianceCheckResult:
    """Circuit filter: if the proposed price is already at or beyond the band, the exchange itself
    would accept no order there -- reject the order intent rather than let it be attempted.

    With `exchange_limits` (the exchange's own lower and upper limits for the day, from the quote)
    the price is checked against those. Without them the fallback is the reference table's band
    (`provider`) measured from `reference_price` (the previous close), and the reason says that the
    quote carried no exchange limits."""
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")

    if exchange_limits is not None:
        if proposed_price >= exchange_limits.upper:
            return ComplianceCheckResult(
                blocked=True,
                reasons=[
                    f"{symbol}: proposed price {proposed_price:.2f} is at/above the exchange's "
                    f"upper circuit limit {exchange_limits.upper:.2f} (from the broker's quote)"
                ],
            )
        if proposed_price <= exchange_limits.lower:
            return ComplianceCheckResult(
                blocked=True,
                reasons=[
                    f"{symbol}: proposed price {proposed_price:.2f} is at/below the exchange's "
                    f"lower circuit limit {exchange_limits.lower:.2f} (from the broker's quote)"
                ],
            )
        return ComplianceCheckResult(blocked=False)

    band_pct = provider.circuit_filter_band_pct(symbol)
    move_pct = abs(proposed_price - reference_price) / reference_price * 100.0
    if move_pct >= band_pct:
        return ComplianceCheckResult(
            blocked=True,
            reasons=[
                f"{symbol}: proposed price move {move_pct:.2f}% from the reference price is "
                f"at/beyond the reference table's circuit band {band_pct:.2f}% (the quote carried "
                "no exchange circuit limits)"
            ],
        )
    return ComplianceCheckResult(blocked=False)


def run_compliance_checks(
    *,
    symbol: str,
    proposed_position_value: float,
    portfolio_value: float,
    proposed_price: float,
    reference_price: float,
    provider: RegulatoryDataProvider,
    exchange_limits: ExchangeCircuitLimits | None = None,
    check_circuit_band: bool = True,
) -> ComplianceCheckResult:
    """Combines both checks into one verdict -- the shape
    `src.orchestration.risk_gate` composes with the naked-options scan and
    correlation constraint.

    `check_circuit_band=False` leaves the circuit band out, for one case only: an order that closes
    a position because its stop-loss was hit. A price that gapped past the band is exactly when the
    stop matters, and refusing the exit would leave the position unprotected; the position limit
    still applies."""
    results = [
        check_position_limit(
            symbol=symbol,
            proposed_position_value=proposed_position_value,
            portfolio_value=portfolio_value,
            provider=provider,
        ),
    ]
    if check_circuit_band:
        results.append(
            check_circuit_filter_band(
                symbol=symbol,
                proposed_price=proposed_price,
                reference_price=reference_price,
                provider=provider,
                exchange_limits=exchange_limits,
            )
        )
    reasons = [reason for result in results for reason in result.reasons]
    return ComplianceCheckResult(blocked=bool(reasons), reasons=reasons)
