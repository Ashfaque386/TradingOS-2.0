"""Risk-limit mutation code path (dual-control stage/confirm, Build Spec
§8.5). Not implemented yet -- ships in a later phase. This module exists
now so src.agents.heartbeat's structural isolation (Build Spec §17:
"structurally cannot call any ... risk-limit-mutation code path") has a
concrete, importable function it must never reference. Never import this
module from src.agents.heartbeat or src.agents.scheduler.
"""

from typing import Any, NoReturn


def mutate_risk_limit(*args: Any, **kwargs: Any) -> NoReturn:
    raise NotImplementedError("risk-limit mutation ships in a later phase")
