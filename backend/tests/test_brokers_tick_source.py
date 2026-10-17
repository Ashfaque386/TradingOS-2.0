"""Broker-quote tick source tests (Build Spec §13): BrokerQuoteTickSource
turns a live BrokerAdapter.get_quote() call into a Tick, and
build_tick_source() picks it over the Phase 7 MockTickSource fallback
exactly when credentials are configured -- proving the swap is real, not
just that each piece works alone.
"""

import httpx
import pytest
from cryptography.fernet import Fernet

from src.brokers import tick_source as tick_source_module
from src.brokers.base import (
    BrokerAuthError,
    BrokerCredentials,
    BrokerQuoteUnavailableError,
    BrokerRequestError,
)
from src.brokers.tick_source import (
    BrokerQuoteTickSource,
    FallbackTickSource,
    _RefreshingTickSource,
    build_tick_source,
    is_broker_configured,
)
from src.brokers.upstox import UpstoxAdapter
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.core.config import get_settings
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    TickSourceUnavailableError,
    UnavailableTickSource,
)
from src.security.secrets_store import SecretsStore, get_secrets_store


def _reset_settings_and_store_caches() -> None:
    get_settings.cache_clear()
    get_secrets_store.cache_clear()


async def test_broker_quote_tick_source_returns_a_tick_from_the_adapters_quote():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"INFY": {"last_price": 1503.25, "depth": {"buy": [], "sell": []}}}},
        )

    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    source = BrokerQuoteTickSource(adapter)

    tick = await source.next_tick("INFY")
    assert tick.symbol == "INFY"
    assert tick.price == 1503.25
    assert tick.timestamp_ms > 0


def test_build_tick_source_is_unavailable_not_synthetic_when_no_key_configured(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    _reset_settings_and_store_caches()
    try:
        source = build_tick_source()
        assert isinstance(source, UnavailableTickSource)
        assert not isinstance(source, MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_build_tick_source_is_unavailable_not_synthetic_when_no_credentials_stored(
    tmp_path, monkeypatch
):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    _reset_settings_and_store_caches()
    try:
        source = build_tick_source()
        assert isinstance(source, UnavailableTickSource)
    finally:
        _reset_settings_and_store_caches()


async def test_an_unavailable_source_refuses_every_symbol_and_says_why():
    """Paper trading must not run on generated prices for real symbols: the tick job gets a clear,
    unwrapped TickSourceUnavailableError (it is re-raised at once, not counted per symbol)."""
    source = UnavailableTickSource()

    with pytest.raises(TickSourceUnavailableError) as caught:
        await source.next_tick("RELIANCE")

    assert "no broker is connected" in str(caught.value)
    assert "does not run on synthetic data" in str(caught.value)
    assert "PAPER_TRADING_SYNTHETIC" in str(caught.value)  # and how a demo opts in


@pytest.mark.parametrize("have_store", [False, True])
def test_synthetic_mode_is_the_only_way_to_get_the_mock_source(tmp_path, monkeypatch, have_store):
    if have_store:
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    else:
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")
    _reset_settings_and_store_caches()
    try:
        assert isinstance(build_tick_source(), MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_synthetic_mode_does_not_override_a_connected_broker(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")
    _reset_settings_and_store_caches()
    try:
        SecretsStore(tmp_path / "secrets.enc", key).set_credentials(
            "zerodha", BrokerCredentials(api_key="k", access_token="t")
        )
        assert isinstance(build_tick_source(), FallbackTickSource)  # real data still wins
    finally:
        _reset_settings_and_store_caches()


def test_build_tick_source_uses_broker_quotes_when_credentials_configured(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        source = build_tick_source()
        assert isinstance(source, FallbackTickSource)
        assert [s.adapter.broker_name for s in source.sources] == ["zerodha"]
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_false_with_no_encryption_key(monkeypatch):
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    _reset_settings_and_store_caches()
    try:
        assert is_broker_configured() is False
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_false_with_an_empty_store(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(tmp_path / "secrets.enc"))
    _reset_settings_and_store_caches()
    try:
        assert is_broker_configured() is False
    finally:
        _reset_settings_and_store_caches()


def test_is_broker_configured_is_true_once_a_broker_has_real_credentials(tmp_path, monkeypatch):
    """Same real credential lookup `build_tick_source()` uses -- proving
    `is_broker_configured()` agrees with which `TickSource` is actually
    running, not an independently-drifting check."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        assert is_broker_configured() is True
        assert isinstance(build_tick_source(), FallbackTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_reuses_the_same_instance_when_config_is_unchanged(
    tmp_path, monkeypatch
):
    """The real bug this fix closes was two-sided: rebuilding on every
    call would silently reset MockTickSource's per-symbol random walk
    every tick-publish firing. Proves the cached instance survives
    repeated calls when nothing about the broker config changed."""
    monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("PAPER_TRADING_SYNTHETIC", "true")  # the mock only exists in demo mode
    _reset_settings_and_store_caches()
    try:
        refreshing = _RefreshingTickSource()
        first = refreshing()
        second = refreshing()
        assert first is second
        assert isinstance(first, MockTickSource)
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_rebuilds_the_moment_a_broker_is_connected(tmp_path, monkeypatch):
    """The actual Phase 17 Part 4 bug: a broker connected via Settings
    used to only take effect after a backend restart, since
    build_tick_source() was resolved once at process startup. Proves the
    next call after credentials are saved picks it up immediately."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    _reset_settings_and_store_caches()
    try:
        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, UnavailableTickSource)

        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, FallbackTickSource)
        assert [s.adapter.broker_name for s in after.sources] == ["zerodha"]
        assert after is not before
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_rebuilds_when_the_same_brokers_credentials_change(
    tmp_path, monkeypatch
):
    """Re-saving a corrected key/token for an already-configured broker
    must also refresh -- the broker-name-only half of the fingerprint
    wouldn't catch this, since "zerodha" stays "zerodha" either way."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="old-key", access_token="t"))
        get_secrets_store.cache_clear()

        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, FallbackTickSource)

        store.set_credentials("zerodha", BrokerCredentials(api_key="new-key", access_token="t2"))
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, FallbackTickSource)
        assert after is not before
    finally:
        _reset_settings_and_store_caches()


def test_refreshing_tick_source_halts_rather_than_going_synthetic_once_a_broker_is_disconnected(
    tmp_path, monkeypatch
):
    """The symmetric case: disconnecting a broker must also take effect
    on the next call, not just connecting one."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    monkeypatch.delenv("PAPER_TRADING_SYNTHETIC", raising=False)
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert isinstance(before, FallbackTickSource)

        store.delete_credentials("zerodha")
        get_secrets_store.cache_clear()

        after = refreshing()
        assert isinstance(after, UnavailableTickSource)  # disconnected: halted, not synthetic
    finally:
        _reset_settings_and_store_caches()


# ---- A broker that has rejected the credentials is not asked again every 3 seconds ----
# The live stack polled Zerodha every 3 s with an expired daily token and got
# the same 403 back ~1,900 times in an hour and a half: a flood of identical
# tracebacks, and that many calls to a broker that had already said no.


class _Counting:
    """An httpx handler that answers with a fixed status and counts calls."""

    def __init__(self, status: int, *, ok_after: int | None = None) -> None:
        self.status = status
        self.ok_after = ok_after
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.ok_after is not None and self.calls > self.ok_after:
            return httpx.Response(200, json={"data": {"INFY": {"last_price": 1500.0}}})
        return httpx.Response(self.status, json={"status": "error", "message": "nope"})


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.events.append(("warning", event, kw))

    def info(self, event: str, **kw) -> None:
        self.events.append(("info", event, kw))


def _zerodha_source(handler, clock=None, retry=600.0):
    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    return BrokerQuoteTickSource(adapter, retry_after_seconds=retry, clock=clock or _Clock())


class _FixedInstruments:
    """The Upstox instrument directory would download a ~100 MB file; these tests are about the
    auth error, not the symbol lookup."""

    async def resolve(self, symbol: str) -> str:
        return "NSE_EQ|INE009A01021"


@pytest.mark.parametrize("status", [401, 403])
async def test_both_brokers_report_a_rejected_token_as_an_auth_error(status):
    handler = _Counting(status)
    zerodha = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )
    upstox = UpstoxAdapter(
        BrokerCredentials(api_key="k", access_token="t"),
        transport=httpx.MockTransport(handler),
        instruments=_FixedInstruments(),
    )

    for adapter in (zerodha, upstox):
        with pytest.raises(BrokerAuthError) as caught:
            await adapter.get_quote("INFY")
        assert caught.value.status_code == status
        assert isinstance(caught.value, BrokerRequestError)  # existing handlers still match


@pytest.mark.parametrize("status", [400, 404, 429])
async def test_other_client_errors_are_not_auth_errors(status):
    source = _zerodha_source(_Counting(status))

    with pytest.raises(BrokerRequestError) as caught:
        await source.next_tick("INFY")

    assert not isinstance(caught.value, BrokerAuthError)


async def test_after_a_rejection_the_source_stops_calling_the_broker(monkeypatch):
    log = _RecordingLogger()
    monkeypatch.setattr(tick_source_module, "logger", log)
    handler = _Counting(403)
    source = _zerodha_source(handler)

    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")
    assert handler.calls == 1
    for _ in range(50):  # the next 50 firings of the 3-second job
        with pytest.raises(TickSourceUnavailableError) as quiet:
            await source.next_tick("INFY")

    assert handler.calls == 1, "a refused token must not be sent again"
    assert "zerodha rejected the stored credentials (HTTP 403)" in str(quiet.value)
    assert "until the credentials change" in str(quiet.value)
    # Said once, with what to do about it -- not once per firing.
    warnings = [
        e
        for e in log.events
        if e[0] == "warning" and e[1] == "tick_source.broker_credentials_rejected"
    ]
    assert len(warnings) == 1
    assert warnings[0][2]["broker"] == "zerodha" and warnings[0][2]["status"] == 403
    assert "Settings" in warnings[0][2]["action"]


async def test_the_source_probes_again_after_the_retry_interval_and_recovers(monkeypatch):
    log = _RecordingLogger()
    monkeypatch.setattr(tick_source_module, "logger", log)
    clock = _Clock()
    handler = _Counting(403, ok_after=1)  # fixed on the broker's side after the first refusal
    source = _zerodha_source(handler, clock, retry=600.0)

    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")
    clock.now += 599
    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")
    assert handler.calls == 1  # still inside the window

    clock.now += 2
    tick = await source.next_tick("INFY")  # the probe goes through, and succeeds
    assert tick.price == 1500.0 and handler.calls == 2
    assert await source.next_tick("INFY")  # back to normal polling, no suspension left
    assert handler.calls == 3
    # Reported once, when it recovered -- not again on every successful call after.
    recovered = [e for e in log.events if e[1] == "tick_source.broker_credentials_accepted_again"]
    assert len(recovered) == 1


async def test_a_probe_that_is_refused_again_waits_a_full_interval_again():
    clock = _Clock()
    handler = _Counting(403)
    source = _zerodha_source(handler, clock, retry=600.0)

    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")
    clock.now += 601
    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")  # the probe: refused again
    assert handler.calls == 2
    clock.now += 599
    with pytest.raises(TickSourceUnavailableError):
        await source.next_tick("INFY")

    assert handler.calls == 2


@pytest.mark.parametrize("status", [404, 429, 500])
async def test_an_ordinary_failure_does_not_suspend_polling(status):
    handler = _Counting(status)
    source = _zerodha_source(handler)

    for _ in range(3):
        with pytest.raises(Exception) as caught:  # noqa: PT011 - any non-suspension error
            await source.next_tick("INFY")
        assert not isinstance(caught.value, TickSourceUnavailableError)

    assert handler.calls == 3


def test_reconnecting_the_broker_replaces_a_suspended_source(tmp_path, monkeypatch):
    """A fresh login stores a new access token; `get_tick_source` then builds a
    new source, so polling resumes at once rather than after the retry interval."""
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="expired"))
        get_secrets_store.cache_clear()
        refreshing = _RefreshingTickSource()
        stale = refreshing()
        assert isinstance(stale, FallbackTickSource)
        stale.sources[0]._suspended_until = float("inf")  # as if the broker had just refused it

        assert refreshing() is stale  # nothing changed: still the suspended one

        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="fresh"))
        get_secrets_store.cache_clear()
        renewed = refreshing()

        assert renewed is not stale
        assert isinstance(renewed, FallbackTickSource)
        assert renewed.sources[0]._suspended_until is None
    finally:
        _reset_settings_and_store_caches()


async def test_a_symbol_with_no_quote_gives_no_tick_and_does_not_suspend_polling():
    """The broker answered fine but has nothing for this symbol: that used to come
    back as a 0.0 price and be published as a real tick. It is a per-symbol
    failure now -- and not a reason to stop asking about the other symbols."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["i"])
        return httpx.Response(200, json={"data": {}})

    source = _zerodha_source(handler)

    for _ in range(3):
        with pytest.raises(BrokerQuoteUnavailableError):
            await source.next_tick("NOSUCHSYMBOL")

    assert calls == ["NSE:NOSUCHSYMBOL"] * 3  # every firing really asked; nothing suspended


def test_connecting_a_second_broker_rebuilds_the_source_so_it_can_be_the_fallback(
    tmp_path, monkeypatch
):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()
        refreshing = _RefreshingTickSource()
        before = refreshing()
        assert [s.adapter.broker_name for s in before.sources] == ["zerodha"]

        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()
        after = refreshing()

        assert after is not before
        assert [s.adapter.broker_name for s in after.sources] == ["zerodha", "upstox"]  # in order
    finally:
        _reset_settings_and_store_caches()


def test_a_broker_without_a_live_token_is_not_part_of_the_source(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    store_path = tmp_path / "secrets.enc"
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(store_path))
    _reset_settings_and_store_caches()
    try:
        store = SecretsStore(store_path, key)
        store.set_credentials("zerodha", BrokerCredentials(api_key="k"))  # keys, no token
        store.set_credentials("upstox", BrokerCredentials(api_key="k", access_token="t"))
        get_secrets_store.cache_clear()

        source = build_tick_source()

        assert [s.adapter.broker_name for s in source.sources] == ["upstox"]
    finally:
        _reset_settings_and_store_caches()
