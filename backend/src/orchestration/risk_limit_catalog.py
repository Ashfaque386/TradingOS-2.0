"""The risk limits that can be staged, with what each one is and what it may be set to.

`risk_limits` stores whatever value was last applied for a name; this says which names exist,
what the value is when nothing has been applied, the range a proposed value must be in, and
whether anything reads the limit. A name outside the catalog cannot be staged, so the console's
list and the engine's reads cannot drift apart.
"""

from dataclasses import dataclass

__all__ = ["MAX_DRAWDOWN_LIMIT", "RISK_LIMIT_CATALOG", "RiskLimitSpec", "limit_spec"]

MAX_DRAWDOWN_LIMIT = "max_drawdown_pct"
WS_LATENCY_LIMIT = "ws_latency_ms"


@dataclass(frozen=True, slots=True)
class RiskLimitSpec:
    name: str
    label: str
    description: str
    unit: str
    default: float
    minimum: float
    maximum: float
    # False: nothing reads this limit yet, so changing it changes nothing. Said, not hidden.
    enforced: bool


RISK_LIMIT_CATALOG: tuple[RiskLimitSpec, ...] = (
    RiskLimitSpec(
        name=MAX_DRAWDOWN_LIMIT,
        label="Max drawdown",
        description=(
            "The fall from the highest equity, in percent, that trips the kill switch. The "
            "monitor reads the applied value on every reading."
        ),
        unit="%",
        default=15.0,
        minimum=0.5,
        maximum=100.0,
        enforced=True,
    ),
    RiskLimitSpec(
        name=WS_LATENCY_LIMIT,
        label="WebSocket latency",
        description="Reserved for the feed-latency guard; nothing reads it yet.",
        unit="ms",
        default=100.0,
        minimum=1.0,
        maximum=60_000.0,
        enforced=False,
    ),
)

_BY_NAME = {spec.name: spec for spec in RISK_LIMIT_CATALOG}


def limit_spec(name: str) -> RiskLimitSpec | None:
    return _BY_NAME.get(name)
