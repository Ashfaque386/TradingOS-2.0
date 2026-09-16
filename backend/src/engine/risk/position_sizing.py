"""Volatility-Adjusted Position Sizing (Build Spec §8): a fixed
risk-per-trade fraction of account equity, divided by a stop distance
derived from the instrument's own ATR -- a more volatile instrument gets a
smaller share count for the same dollar-risk budget, never a flat share
count regardless of volatility.

quantity = floor((equity * risk_per_trade_pct / 100) / (atr * atr_multiplier))

`atr_multiplier` is this module's own modeling choice for how many ATRs
away the assumed stop sits (a wider stop needs a smaller size for the same
risk budget), the same "our own documented choice, not a regulatory
figure" posture as Phase 5's slippage model.
"""

from dataclasses import dataclass

DEFAULT_RISK_PER_TRADE_PCT = 1.0
DEFAULT_ATR_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    quantity: int
    risk_amount: float | None
    stop_distance: float | None
    reason: str | None = None


def size_position(
    *,
    account_equity: float,
    price: float,
    atr: float,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
) -> PositionSizeResult:
    if account_equity <= 0:
        return PositionSizeResult(
            quantity=0,
            risk_amount=None,
            stop_distance=None,
            reason="account_equity must be positive",
        )
    if price <= 0:
        return PositionSizeResult(
            quantity=0, risk_amount=None, stop_distance=None, reason="price must be positive"
        )
    if atr <= 0:
        return PositionSizeResult(
            quantity=0, risk_amount=None, stop_distance=None, reason="atr must be positive"
        )

    risk_amount = account_equity * (risk_per_trade_pct / 100.0)
    stop_distance = atr * atr_multiplier
    quantity = int(risk_amount // stop_distance)
    return PositionSizeResult(
        quantity=quantity, risk_amount=risk_amount, stop_distance=stop_distance
    )
