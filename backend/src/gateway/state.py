"""In-memory holder for the currently-active Agent Gateway config — the
"last-known-good" the app actually runs on between hot-reloads. A reload
that fails validation never touches this; the app keeps running on
whatever was here before (Build Spec §6.2).
"""

import threading

from src.gateway.loader import EffectiveAgentConfig, compute_effective_agents
from src.gateway.schema import TradingOSConfig


class GatewayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config: TradingOSConfig | None = None
        self._effective_agents: dict[str, EffectiveAgentConfig] = {}
        self._version_id: int | None = None

    def set(self, config: TradingOSConfig, version_id: int) -> None:
        effective = compute_effective_agents(config)
        with self._lock:
            self._config = config
            self._effective_agents = effective
            self._version_id = version_id

    def get_config(self) -> TradingOSConfig | None:
        with self._lock:
            return self._config

    def get_version_id(self) -> int | None:
        with self._lock:
            return self._version_id

    def get_effective_agent(self, agent_id: str) -> EffectiveAgentConfig | None:
        with self._lock:
            return self._effective_agents.get(agent_id)

    def get_all_effective_agents(self) -> dict[str, EffectiveAgentConfig]:
        with self._lock:
            return dict(self._effective_agents)


_STATE = GatewayState()


def get_state() -> GatewayState:
    return _STATE
