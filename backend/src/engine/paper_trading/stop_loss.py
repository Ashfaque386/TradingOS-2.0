"""Layer 2's universal stop-loss (Build Spec §11): one configurable
percentage threshold applied uniformly to every open paper position,
independent of which strategy or symbol it belongs to -- "universal" as
opposed to a per-strategy custom exit rule. Re-entry (re-opening a
position after a stop-out, while Layer 1's daily BUY signal is still
active) is an orchestration-level decision
(`src.orchestration.paper_trading.process_tick`) since it depends on
persisted `DailySignal` state, not something this pure function can
decide on a tick price alone.
"""

from dataclasses import dataclass

DEFAULT_STOP_LOSS_PCT = 3.0


@dataclass(frozen=True, slots=True)
class StopLossDecision:
    triggered: bool
    reason: str | None = None


def check_universal_stop_loss(
    *,
    position_qty: int,
    avg_cost: float,
    tick_price: float,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
) -> StopLossDecision:
    """A long position stops out when price falls `stop_loss_pct`% below
    average cost; a short position stops out when price rises
    `stop_loss_pct`% above it. A flat position (quantity 0) never
    triggers -- there's nothing to stop out of."""
    if position_qty == 0 or avg_cost <= 0:
        return StopLossDecision(triggered=False)

    if position_qty > 0:
        threshold = avg_cost * (1 - stop_loss_pct / 100.0)
        if tick_price <= threshold:
            return StopLossDecision(
                triggered=True,
                reason=(
                    f"long stop-loss: tick {tick_price} <= threshold {threshold:.4f} "
                    f"({stop_loss_pct}% below avg cost {avg_cost})"
                ),
            )
    else:
        threshold = avg_cost * (1 + stop_loss_pct / 100.0)
        if tick_price >= threshold:
            return StopLossDecision(
                triggered=True,
                reason=(
                    f"short stop-loss: tick {tick_price} >= threshold {threshold:.4f} "
                    f"({stop_loss_pct}% above avg cost {avg_cost})"
                ),
            )

    return StopLossDecision(triggered=False)
