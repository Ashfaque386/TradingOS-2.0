"""What the broker actually said about the stored credentials (`feed_health`), and
how it reaches Settings: a token the clock calls valid but the broker is refusing
used to show as "connected"."""

from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet

from src.api.routes.broker_credentials import get_broker_credentials_store
from src.brokers import feed_health
from src.brokers.base import BrokerCredentials
from src.brokers.tick_source import BrokerQuoteTickSource, _RefreshingTickSource
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.core.config import get_settings
from src.core.roles import Role
from src.engine.paper_trading.tick_feed import TickSourceUnavailableError
from src.main import app
from src.security.secrets_store import SecretsStore, get_secrets_store


def test_a_rejection_is_recorded_per_broker_and_can_be_cleared():
    feed_health.record_rejection("zerodha", status_code=403, detail="TokenException")

    rejection = feed_health.get_rejection("zerodha")
    assert rejection is not None
    assert (rejection.status_code, rejection.detail) == (403, "TokenException")
    assert datetime.now(UTC) - rejection.at < timedelta(seconds=5)
    assert feed_health.get_rejection("upstox") is None  # a different broker is unaffected

    feed_health.clear("upstox")
    assert feed_health.get_rejection("zerodha") is not None  # clearing one leaves the other
    feed_health.clear("zerodha")
    assert feed_health.get_rejection("zerodha") is None


def test_clearing_without_a_broker_forgets_every_rejection():
    feed_health.record_rejection("zerodha", status_code=403, detail="x")
    feed_health.record_rejection("upstox", status_code=401, detail="y")

    feed_health.clear()

    assert feed_health.get_rejection("zerodha") is None
    assert feed_health.get_rejection("upstox") is None


def test_a_long_detail_is_cut_short():
    feed_health.record_rejection("zerodha", status_code=403, detail="d" * 500)

    assert len(feed_health.get_rejection("zerodha").detail) == 200


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _source(handler, clock) -> BrokerQuoteTickSource:
    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    return BrokerQuoteTickSource(adapter, retry_after_seconds=600, clock=clock)


async def test_the_tick_source_records_the_rejection_and_clears_it_when_a_probe_succeeds():
    clock = _Clock()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403, json={"status": "error", "message": "token"})
        return httpx.Response(
            200, json={"data": {"NSE:INFY": {"last_price": 1500.0}, "INFY": {"last_price": 1500.0}}}
        )

    source = _source(handler, clock)
    try:
        await source.next_tick("INFY")
    except TickSourceUnavailableError:
        pass
    recorded = feed_health.get_rejection("zerodha")
    assert recorded is not None and recorded.status_code == 403

    clock.now += 601
    tick = await source.next_tick("INFY")  # the probe the broker now accepts

    assert tick.price == 1500.0
    assert feed_health.get_rejection("zerodha") is None


def test_a_rebuilt_tick_source_for_new_credentials_forgets_the_old_rejection(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    get_settings.cache_clear()
    get_secrets_store.cache_clear()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="old"))
        get_secrets_store.cache_clear()
        refreshing = _RefreshingTickSource()
        refreshing()
        feed_health.record_rejection("zerodha", status_code=403, detail="old token")

        refreshing()  # nothing changed: the rejection still stands
        assert feed_health.get_rejection("zerodha") is not None

        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="new"))
        get_secrets_store.cache_clear()
        refreshing()

        assert feed_health.get_rejection("zerodha") is None
    finally:
        get_settings.cache_clear()
        get_secrets_store.cache_clear()


# ---- through the API ----


async def _admin(client, make_user, email: str) -> dict[str, str]:
    await make_user(email, "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _store(tmp_path) -> SecretsStore:
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    app.dependency_overrides[get_broker_credentials_store] = lambda: store
    return store


async def _status(client, headers, broker: str) -> dict:
    rows = (await client.get("/api/v1/broker-credentials", headers=headers)).json()
    return next(r for r in rows if r["broker"] == broker)


async def test_the_status_endpoint_reports_a_rejection_the_clock_cannot_know(
    client, make_user, tmp_path
):
    store = _store(tmp_path)
    try:
        headers = await _admin(client, make_user, "admin-fh1@example.com")
        # A token the clock calls valid (no known expiry), but the broker refuses.
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        before = await _status(client, headers, "zerodha")
        assert before["token_status"] == "valid"
        assert before["feed_rejection_status"] is None and before["feed_rejected_at"] is None

        feed_health.record_rejection("zerodha", status_code=403, detail="Incorrect token")
        after = await _status(client, headers, "zerodha")

        assert after["token_status"] == "valid"  # the clock-derived field is unchanged
        assert after["feed_rejection_status"] == 403
        assert after["feed_rejection_detail"] == "Incorrect token"
        assert after["feed_rejected_at"] is not None
        assert (await _status(client, headers, "upstox"))["feed_rejected_at"] is None
    finally:
        app.dependency_overrides.pop(get_broker_credentials_store, None)


async def test_a_rejection_for_a_broker_with_no_credentials_is_not_shown(
    client, make_user, tmp_path
):
    _store(tmp_path)
    try:
        headers = await _admin(client, make_user, "admin-fh2@example.com")
        feed_health.record_rejection("zerodha", status_code=403, detail="stale")

        status = await _status(client, headers, "zerodha")

        assert status["configured"] is False and status["feed_rejected_at"] is None
    finally:
        app.dependency_overrides.pop(get_broker_credentials_store, None)


async def test_saving_or_deleting_credentials_clears_the_rejection(client, make_user, tmp_path):
    store = _store(tmp_path)
    try:
        headers = await _admin(client, make_user, "admin-fh3@example.com")
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        feed_health.record_rejection("zerodha", status_code=403, detail="x")
        feed_health.record_rejection("upstox", status_code=401, detail="y")

        saved = await client.post(
            "/api/v1/broker-credentials/zerodha",
            json={"api_key": "k2", "access_token": "fresh"},
            headers=headers,
        )
        deleted = await client.delete("/api/v1/broker-credentials/upstox", headers=headers)

        assert saved.status_code == deleted.status_code == 204
        assert feed_health.get_rejection("zerodha") is None
        assert feed_health.get_rejection("upstox") is None
    finally:
        app.dependency_overrides.pop(get_broker_credentials_store, None)
