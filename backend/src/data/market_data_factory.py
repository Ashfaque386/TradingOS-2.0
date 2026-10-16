"""Which `MarketDataProvider` the app runs on: the connected broker, or -- only when
`PAPER_TRADING_SYNTHETIC` is on (demos and the automated tests) -- the generated-data provider."""

from pathlib import Path

import structlog

from src.brokers.base import BrokerAdapter
from src.brokers.breaker_registry import get_broker_circuit_breaker
from src.brokers.factory import KNOWN_BROKERS, build_broker_adapter
from src.brokers.resilient import ResilientBrokerAdapter
from src.core.config import get_settings
from src.data.broker_provider import BrokerMarketDataProvider
from src.data.providers import AnyMarketDataProvider, FakeMarketDataProvider
from src.security.secrets_store import SecretsStoreError, get_secrets_store

logger = structlog.get_logger(__name__)


def connected_adapters() -> list[BrokerAdapter]:
    """The brokers that can serve market data right now, in preference order (`KNOWN_BROKERS`:
    Zerodha first): credentials stored, an access token present and not expired. Each sits behind
    its circuit breaker, like every other broker call."""
    try:
        store = get_secrets_store()
    except SecretsStoreError:
        return []
    adapters: list[BrokerAdapter] = []
    for broker in KNOWN_BROKERS:
        credentials = store.get_credentials(broker)
        if credentials is None or not credentials.access_token:
            continue
        if credentials.token_status == "expired":  # nosec B105: a status label, not a secret
            continue
        adapters.append(
            ResilientBrokerAdapter(
                build_broker_adapter(broker, credentials),
                breaker=get_broker_circuit_breaker(broker),
            )
        )
    return adapters


def get_market_data_provider() -> AnyMarketDataProvider:
    """FastAPI dependency and the scheduler's provider. Cheap to build: the broker provider
    re-reads the connected brokers on every call, so a broker connected later is picked up."""
    settings = get_settings()
    if settings.paper_trading_synthetic:
        return FakeMarketDataProvider()
    return BrokerMarketDataProvider(connected_adapters, root=Path(settings.data_lake_path))
