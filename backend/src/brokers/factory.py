"""Builds a concrete `BrokerAdapter` by broker name (Build Spec §13) --
the one place that knows the mapping from a broker-name string (as stored
in the secrets store and passed over HTTP) to a concrete adapter class,
so every caller (the credential-status API, Shadow Mode checks, the
Phase 7 tick feed's broker-quote poller, Phase 9's LiveExecutionPipeline)
shares one definition of "which brokers exist" instead of hardcoding the
pair themselves.
"""

import structlog

from src.brokers.base import BrokerAdapter, BrokerCredentials
from src.brokers.resilient import ResilientBrokerAdapter
from src.brokers.upstox import UpstoxAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)

KNOWN_BROKERS = ("zerodha", "upstox")


class UnknownBrokerError(ValueError):
    def __init__(self, broker: str):
        self.broker = broker
        super().__init__(f"unknown broker: {broker!r} (known: {', '.join(KNOWN_BROKERS)})")


def build_broker_adapter(
    broker: str, credentials: BrokerCredentials, *, sandbox: bool = False
) -> BrokerAdapter:
    """`sandbox` only affects Upstox (the only broker with one to point
    at) -- passing it for Zerodha is accepted but has no effect, since
    ZerodhaKiteAdapter always talks to the one production endpoint Kite
    Connect offers."""
    if broker == "zerodha":
        return ZerodhaKiteAdapter(credentials)
    if broker == "upstox":
        return UpstoxAdapter(credentials, sandbox=sandbox)
    raise UnknownBrokerError(broker)


def build_configured_adapter(*, sandbox: bool = False) -> ResilientBrokerAdapter | None:
    """Looks up whichever broker has credentials configured in the
    secrets store (first match in `KNOWN_BROKERS` order) and returns a
    circuit-breaker-wrapped adapter for it (Phase 6's `BrokerCircuitBreaker`
    via `ResilientBrokerAdapter`, Phase 8) -- or `None` if no broker is
    configured at all. This is the one shared credential-lookup path
    behind both `src.brokers.tick_source.build_tick_source` (Phase 8,
    quote polling) and `src.orchestration.live_trading` (Phase 9, real
    order submission); a caller placing real orders must always leave
    `sandbox=False` (the default) -- passing `sandbox=True` here is only
    ever correct for Shadow Mode's own dedicated adapter construction in
    src.orchestration.shadow_mode, which builds its adapter directly
    rather than through this function precisely so a dry run can never
    share a code path with real order placement.
    """
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return None

    for broker in KNOWN_BROKERS:
        credentials = store.get_credentials(broker)
        if credentials is None:
            continue
        adapter = build_broker_adapter(broker, credentials, sandbox=sandbox)
        logger.info("brokers.configured_adapter_selected", broker=broker, sandbox=sandbox)
        return ResilientBrokerAdapter(adapter)

    return None
