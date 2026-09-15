"""Order-placement code path (Execution Agent / Paper Trading Engine, Build
Spec §7.1). Not implemented yet -- ships in a later phase. This module
exists now so src.agents.heartbeat's structural isolation (Build Spec §17:
"structurally cannot call any order-placement ... code path") has a
concrete, importable function it must never reference. Never import this
module from src.agents.heartbeat or src.agents.scheduler.
"""


def place_order(*args, **kwargs):
    raise NotImplementedError("order placement ships in a later phase")
