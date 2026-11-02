"""In-memory holder for the currently-active Agent Gateway config — the
"last-known-good" the app actually runs on between hot-reloads. A reload
that fails validation never touches this; the app keeps running on
whatever was here before (Build Spec §6.2).
"""

import hashlib
import threading
import time

from src.gateway.loader import EffectiveAgentConfig, compute_effective_agents
from src.gateway.schema import TradingOSConfig


class GatewayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config: TradingOSConfig | None = None
        self._effective_agents: dict[str, EffectiveAgentConfig] = {}
        self._version_id: int | None = None
        self._self_write: tuple[str, float] | None = None

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

    def note_self_write(self, text: str) -> None:
        """The API (or a service call) is about to write `text` to the config file and apply it
        itself. The file watcher will notice that write a moment later and must not apply the same
        text a second time, which used to leave a duplicate "active" version after every save."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            self._self_write = (digest, time.monotonic())

    def consume_self_write(self, content_hash: str, *, within_seconds: float = 30.0) -> bool:
        """True once, for the hash of a write `note_self_write` announced in the last
        `within_seconds`: the watcher skips that one change. Anything else is a change like any
        other, and it forgets the announcement: a person who edits the file after the API wrote it
        and then puts the API's text back has made a change that must be applied."""
        with self._lock:
            noted = self._self_write
            self._self_write = None
            if noted is None or noted[0] != content_hash:
                return False
            return time.monotonic() - noted[1] <= within_seconds

    def get_all_effective_agents(self) -> dict[str, EffectiveAgentConfig]:
        with self._lock:
            return dict(self._effective_agents)


_STATE = GatewayState()


def get_state() -> GatewayState:
    return _STATE
