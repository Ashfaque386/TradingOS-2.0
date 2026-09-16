"""Builds a concrete `BrokerAdapter` by broker name (Build Spec §13) --
the one place that knows the mapping from a broker-name string (as stored
in the secrets store and passed over HTTP) to a concrete adapter class,
so every caller (the credential-status API, Shadow Mode checks, the
Phase 7 tick feed's broker-quote poller) shares one definition of "which
brokers exist" instead of hardcoding the pair themselves.
"""

from src.brokers.base import BrokerAdapter, BrokerCredentials
from src.brokers.upstox import UpstoxAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter

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
