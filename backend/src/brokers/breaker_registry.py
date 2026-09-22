"""Per-broker `BrokerCircuitBreaker` singletons (Phase 16 audit follow-up
A): `src.brokers.factory.build_configured_adapter` used to construct a
fresh `ResilientBrokerAdapter(adapter)` -- and therefore a fresh,
zero-state `BrokerCircuitBreaker()` -- on every single call, including
once per HTTP request via `src.api.routes.live_trading.get_live_broker_adapter`.
Three consecutive 5xx responses spread across different callers (the
paper-trading tick source, the live-trading scheduler, one-off API-route
adapters) could each independently see only 1 or 2 failures and never
actually trip -- the breaker's whole purpose (stop hammering a broker
that's already down) silently didn't work across the app's real call
pattern, and there was no live state anywhere to query even if it had.

`get_broker_circuit_breaker(broker)` lazily creates and caches exactly
one `BrokerCircuitBreaker` per broker name for the lifetime of the
process -- a plain module-level dict, not a DB row: unlike Phase 6's
Kill Switch (which must survive a restart and be correct across
multiple worker processes), circuit-breaker state losing itself on
restart is fine -- a fresh process legitimately starts from "assume the
broker is healthy" -- and this app runs as a single uvicorn process (no
`--workers N`), so a module-level singleton genuinely is the one shared
instance every caller sees, matching this codebase's existing
in-process-singleton precedent (src.gateway's `GatewayState`,
src.agents.llm_router's per-provider `ProviderHealth`).
"""

from src.engine.risk.circuit_breaker import BrokerCircuitBreaker

_BREAKERS: dict[str, BrokerCircuitBreaker] = {}


def get_broker_circuit_breaker(broker: str) -> BrokerCircuitBreaker:
    if broker not in _BREAKERS:
        _BREAKERS[broker] = BrokerCircuitBreaker()
    return _BREAKERS[broker]


def get_all_broker_circuit_breakers() -> dict[str, BrokerCircuitBreaker]:
    """A read-only snapshot of the dict for the status endpoint -- does
    not create an entry for a broker that has never had an adapter built
    for it (a breaker's `CLOSED`/zero-failures starting state is exactly
    what an uncreated entry would report anyway, so the status endpoint
    treats "not in this dict" and "present, closed, zero failures" the
    same rather than eagerly creating one at read time)."""
    return dict(_BREAKERS)
