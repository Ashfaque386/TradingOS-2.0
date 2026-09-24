"""Order-Intent Risk Gate (Build Spec §8): the single choke point every
order-intent-creating code path in this codebase must go through before an
intent is allowed to exist. Concrete live/paper trading engines (Phase 7)
and broker adapters (Phase 8) don't exist yet -- this module is what they
will call once they do; this phase proves the gate itself works by
exercising it directly.

Check order is deliberate and never reordered: the kill switch is checked
FIRST, before anything else runs -- once tripped, no compliance check, no
naked-options scan, no correlation check, nothing downstream even
executes. That is what "blocks new order intents, not just submission"
means in practice: `create_order_intent` raises before an `OrderIntent`
object is ever constructed, for either kill-switch mode independently
(src.orchestration.kill_switch keeps live and paper state in separate
rows).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from src.engine.options_grounding import GroundedLeg, naked_options_scan
from src.engine.risk.compliance import (
    ComplianceCheckResult,
    RegulatoryDataProvider,
    run_compliance_checks,
)
from src.engine.risk.correlation_constraint import (
    DEFAULT_CORRELATION_THRESHOLD,
    CorrelationCheckResult,
    NiftyBenchmarkProvider,
    evaluate_correlation_constraint,
)
from src.engine.risk.kill_switch import KillSwitchMode
from src.orchestration.kill_switch import assert_not_tripped

__all__ = ["OrderIntent", "OrderIntentRejectedError", "create_order_intent"]


class OrderIntentRejectedError(Exception):
    """Raised when one or more non-kill-switch checks reject the intent --
    the kill switch itself raises `KillSwitchTrippedError` separately
    (src.engine.risk.kill_switch), checked before anything that could
    raise this."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True, slots=True)
class OrderIntent:
    mode: KillSwitchMode
    symbol: str
    side: str
    quantity: int
    price: float
    created_at: datetime
    compliance: ComplianceCheckResult
    correlation: CorrelationCheckResult | None


async def create_order_intent(
    db: AsyncSession,
    *,
    mode: KillSwitchMode,
    symbol: str,
    side: str,
    quantity: int,
    proposed_price: float,
    reference_price: float,
    proposed_position_value: float,
    portfolio_value: float,
    regulatory_provider: RegulatoryDataProvider,
    option_legs: list[GroundedLeg] | None = None,
    recent_returns: pd.Series | None = None,
    benchmark_provider: NiftyBenchmarkProvider | None = None,
    correlation_threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> OrderIntent:
    # 1. Kill switch -- checked first, ahead of everything below. Raises
    #    KillSwitchTrippedError and returns without evaluating anything
    #    else once the switch for this mode is tripped.
    await assert_not_tripped(db, mode)

    reasons: list[str] = []

    # 2. Compliance: SEBI-style position limits + NSE-style circuit band.
    compliance = run_compliance_checks(
        symbol=symbol,
        proposed_position_value=proposed_position_value,
        portfolio_value=portfolio_value,
        proposed_price=proposed_price,
        reference_price=reference_price,
        provider=regulatory_provider,
    )
    reasons.extend(compliance.reasons)

    # 3. Naked-options scan (Phase 4's src.engine.options_grounding) --
    #    wired into this per-tick gate in addition to the pre-deployment
    #    check in src.orchestration.strategies, per Build Spec §8's
    #    requirement for both.
    if option_legs:
        scan = naked_options_scan(option_legs)
        reasons.extend(scan.reasons)

    # 4. Correlation constraint -- the same evaluate_correlation_constraint
    #    used pre-deployment (src.orchestration.strategies.
    #    promote_to_paper_trading), wired here too so it is never only a
    #    pre-deployment check (the explicit gap this phase closes).
    correlation: CorrelationCheckResult | None = None
    if recent_returns is not None and benchmark_provider is not None:
        correlation = evaluate_correlation_constraint(
            recent_returns, benchmark_provider.daily_returns(), threshold=correlation_threshold
        )
        if correlation.breached:
            reasons.append(
                f"{symbol}: recent returns correlation {correlation.correlation:.4f} with "
                f"Nifty 50 exceeds constraint {correlation_threshold:.4f}"
            )

    if reasons:
        raise OrderIntentRejectedError(reasons)

    return OrderIntent(
        mode=mode,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=proposed_price,
        created_at=datetime.now(UTC),
        compliance=compliance,
        correlation=correlation,
    )
