"""Max Drawdown Kill Switch (Build Spec §8): the single most important
safety primitive in this build -- every threshold and behavior here is
load-bearing.

**Latching, never self-clearing**: once tripped, the switch stays tripped
regardless of what the equity curve does afterward (even a full recovery
above the prior peak does NOT clear it) -- only an explicit human reset
(src.orchestration.kill_switch.reset_kill_switch, always recording who and
when) clears it. This module's `evaluate_drawdown` is pure and stateless;
the *latching* itself is a persistence-layer concern
(src.orchestration.kill_switch) precisely so "never self-clears" is a
property of the stored state, not of any in-process object that could be
recreated fresh on the next request.

**Two separate instances, live and paper**: this module never hard-codes
"the" kill switch -- every function takes an explicit `KillSwitchMode`.
There is no shared mutable state anywhere in this module (or its
persistence layer) that could let tripping one mode affect the other; the
two modes are kept apart by construction (an explicit, required parameter
plus, in the persistence layer, a distinct DB row per mode), not by
convention.
"""

from dataclasses import dataclass
from typing import Literal

KillSwitchMode = Literal["live", "paper"]

DEFAULT_MAX_DRAWDOWN_PCT = 15.0


@dataclass(frozen=True, slots=True)
class DrawdownEvaluation:
    """Never fabricates a drawdown figure: `drawdown_pct` is `None` when the
    peak equity given is non-positive (nothing meaningful to divide by),
    not a fabricated `0`."""

    drawdown_pct: float | None
    should_trip: bool
    threshold_pct: float


def evaluate_drawdown(
    *,
    current_equity: float,
    peak_equity: float,
    threshold_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
) -> DrawdownEvaluation:
    if peak_equity <= 0:
        return DrawdownEvaluation(drawdown_pct=None, should_trip=False, threshold_pct=threshold_pct)

    drawdown_pct = max(0.0, (peak_equity - current_equity) / peak_equity * 100.0)
    return DrawdownEvaluation(
        drawdown_pct=drawdown_pct,
        should_trip=drawdown_pct >= threshold_pct,
        threshold_pct=threshold_pct,
    )


class KillSwitchTrippedError(Exception):
    """Raised by the order-intent risk gate (src.orchestration.risk_gate)
    when the kill switch for the requested mode is latched tripped. This is
    the mechanism that blocks new order intents from being *created* at
    all, not merely rejected at submission time."""

    def __init__(self, mode: KillSwitchMode, reason: str | None):
        self.mode = mode
        self.reason = reason
        super().__init__(f"{mode} kill switch is tripped: {reason or 'no reason recorded'}")
