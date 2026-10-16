"""Real symbols and real history at enrolment (real-market-data plan, phase 2).

With a broker connected, enrolling a subscription checks the symbol is one the broker lists and
fetches its real price history; with none connected the subscription is still allowed (its state
says why it is not running). `backfill-history` retries the fetch; the 07:30 IST job repairs it
every morning and tells the operator when it cannot.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data import lake
from src.data.broker_provider import NoRealDataSourceError
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.main import app
from src.orchestration import market_data_scheduler as scheduler_module


def _frame(days: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [100.0 + i for i in range(days)],
            "volume": 1000,
            "source": "zerodha",
        },
        index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=days),
    )


class _Provider:
    """A broker-shaped provider: lists some symbols, returns bars, or says no broker is connected."""  # noqa: E501

    source_name = "broker_market_data"

    def __init__(self, listed=("INFY",), *, connected=True, fail=None):
        self.listed = set(listed)
        self.connected = connected
        self.fail = fail
        self.fetched: list[str] = []

    async def symbol_listed(self, symbol: str) -> bool:
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return symbol in self.listed

    async def daily_bars(self, symbol, start, end):
        self.fetched.append(symbol)
        if self.fail is not None:
            raise self.fail
        return _frame()


@pytest.fixture(autouse=True)
def _wiring(redis_client, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path / "lake"))
    from src.core.config import get_settings

    get_settings.cache_clear()
    app.dependency_overrides[get_redis] = lambda: redis_client
    app.dependency_overrides[get_signal_price_provider] = lambda: DataLakePriceProvider(
        root=tmp_path / "lake", real_only=True
    )
    yield
    for dependency in (get_redis, get_signal_price_provider, get_market_data_provider):
        app.dependency_overrides.pop(dependency, None)
    get_settings.cache_clear()


async def _headers(client, make_user, email: str, role: Role = Role.SYSTEM_ADMINISTRATOR) -> dict:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _version_id(client, headers) -> str:
    created = await client.post(
        "/api/v1/strategies", json={"name": "HistDemo", "objective": "o"}, headers=headers
    )
    return created.json()["versions"][0]["id"]


async def _enrol(client, headers, symbol: str, version_id: str | None = None):
    return await client.post(
        "/api/v1/paper-trading/subscriptions",
        json={
            "strategy_version_id": version_id or await _version_id(client, headers),
            "symbol": symbol,
        },
        headers=headers,
    )


# ---- enrolment ----


async def test_enrolling_a_listed_symbol_fetches_its_real_history(client, make_user, tmp_path):
    provider = _Provider(listed=("INFY",))
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    headers = await _headers(client, make_user, "hist-admin@example.com")

    resp = await _enrol(client, headers, "INFY")

    assert resp.status_code == 201
    assert provider.fetched == ["INFY"]
    stored = lake.read_daily_bars(
        tmp_path / "lake", "INFY", date(2000, 1, 1), date(2100, 1, 1), real_only=True
    )
    assert len(stored) == 100  # real history is in the lake, tagged
    assert lake.pinned_source(tmp_path / "lake", "INFY") == "zerodha"


async def test_enrolling_a_symbol_no_broker_lists_is_refused_and_creates_nothing(client, make_user):
    provider = _Provider(listed=("INFY",))
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    headers = await _headers(client, make_user, "unlisted-admin@example.com")

    resp = await _enrol(client, headers, "DEMOSTOCK")

    assert resp.status_code == 422
    assert "no connected broker lists" in resp.json()["detail"]
    assert "real symbols" in resp.json()["detail"]
    assert provider.fetched == []
    listing = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)
    assert listing.json() == []


async def test_with_no_broker_connected_the_enrolment_is_allowed_but_fetches_nothing(
    client, make_user
):
    provider = _Provider(connected=False)
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    headers = await _headers(client, make_user, "nobroker-admin@example.com")

    resp = await _enrol(client, headers, "INFY")

    assert resp.status_code == 201 and provider.fetched == []
    row = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()[0]
    assert row["state"] == "awaiting_history"  # waiting for a broker AND for history


async def test_a_failed_history_fetch_does_not_undo_the_enrolment(client, make_user):
    provider = _Provider(fail=RuntimeError("zerodha: Insufficient permission"))
    app.dependency_overrides[get_market_data_provider] = lambda: provider
    headers = await _headers(client, make_user, "failfetch-admin@example.com")

    resp = await _enrol(client, headers, "INFY")

    assert resp.status_code == 201  # enrolled; the fetch is retried by the morning repair
    row = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()[0]
    assert row["state"] == "awaiting_history"
    assert "history has not been ingested yet" in row["state_reason"]


async def test_after_a_successful_fetch_the_subscription_is_no_longer_awaiting_history(
    client, make_user
):
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider()
    headers = await _headers(client, make_user, "ready-admin@example.com")
    await _enrol(client, headers, "INFY")

    row = (await client.get("/api/v1/paper-trading/subscriptions", headers=headers)).json()[0]

    # History is there; what is left to wait for is a price feed (none is connected in tests).
    assert row["state"] == "halted_no_data"


# ---- backfill-history ----


async def test_backfill_history_reports_what_it_stored(client, make_user):
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider(connected=False)
    headers = await _headers(client, make_user, "bf-admin@example.com")
    sub = (await _enrol(client, headers, "INFY")).json()["id"]
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider()

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub}/backfill-history", headers=headers
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success" and body["symbol"] == "INFY"
    assert body["rows"] == 100 and body["source"] == "zerodha" and body["error"] is None
    assert body["first_date"] and body["last_date"]


async def test_backfill_history_says_why_it_failed(client, make_user):
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider(connected=False)
    headers = await _headers(client, make_user, "bffail-admin@example.com")
    sub = (await _enrol(client, headers, "INFY")).json()["id"]
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider(
        fail=NoRealDataSourceError("no broker is connected")
    )

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub}/backfill-history", headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "failed" and "no broker is connected" in resp.json()["error"]
    assert resp.json()["rows"] == 0


async def test_backfill_history_needs_an_operator_and_a_real_subscription(client, make_user):
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider()
    admin = await _headers(client, make_user, "bfrole-admin@example.com")
    sub = (await _enrol(client, admin, "INFY")).json()["id"]
    auditor = await _headers(
        client, make_user, "bfrole-auditor@example.com", Role.READ_ONLY_AUDITOR
    )

    denied = await client.post(
        f"/api/v1/paper-trading/subscriptions/{sub}/backfill-history", headers=auditor
    )
    missing = await client.post(
        "/api/v1/paper-trading/subscriptions/00000000-0000-0000-0000-000000000000/backfill-history",
        headers=admin,
    )

    assert denied.status_code == 403
    assert missing.status_code == 404


# ---- the morning job ----


@pytest.fixture
def _sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append({"level": level, "title": title, "body": body})
        return []

    monkeypatch.setattr(scheduler_module, "notify", fake_notify)
    return calls


async def _make_active_subscription(client, make_user, email: str, symbol: str = "INFY"):
    app.dependency_overrides[get_market_data_provider] = lambda: _Provider(connected=False)
    headers = await _headers(client, make_user, email)
    await _enrol(client, headers, symbol)
    return headers


async def test_the_morning_job_repairs_history_and_stays_quiet_on_success(
    client, make_user, db_session_factory, tmp_path, _sent, monkeypatch
):
    await _make_active_subscription(client, make_user, "job-admin@example.com")
    monkeypatch.setattr(scheduler_module, "is_nse_trading_day", lambda day: True)

    await scheduler_module.run_history_gap_repair_job(
        db_session_factory, _Provider(), tmp_path / "lake"
    )

    assert _sent == []
    assert lake.pinned_source(tmp_path / "lake", "INFY") == "zerodha"


async def test_the_morning_job_tells_the_operator_which_symbols_could_not_be_fetched(
    client, make_user, db_session_factory, tmp_path, _sent, monkeypatch
):
    await _make_active_subscription(client, make_user, "jobfail-admin@example.com")
    monkeypatch.setattr(scheduler_module, "is_nse_trading_day", lambda day: True)

    await scheduler_module.run_history_gap_repair_job(
        db_session_factory,
        _Provider(fail=NoRealDataSourceError("no broker is connected")),
        tmp_path / "lake",
    )

    assert len(_sent) == 1
    assert _sent[0]["title"] == "Price history could not be fetched"
    assert "INFY" in _sent[0]["body"] and "no broker is connected" in _sent[0]["body"]


async def test_the_morning_job_does_nothing_on_a_market_holiday(
    client, make_user, db_session_factory, tmp_path, _sent, monkeypatch
):
    provider = _Provider()
    monkeypatch.setattr(scheduler_module, "is_nse_trading_day", lambda day: False)
    await _make_active_subscription(client, make_user, "jobhol-admin@example.com")

    await scheduler_module.run_history_gap_repair_job(
        db_session_factory, provider, tmp_path / "lake"
    )

    assert provider.fetched == [] and _sent == []


async def test_the_morning_job_does_nothing_with_no_active_subscription(
    db_session_factory, tmp_path, _sent, monkeypatch
):
    provider = _Provider()
    monkeypatch.setattr(scheduler_module, "is_nse_trading_day", lambda day: True)

    await scheduler_module.run_history_gap_repair_job(
        db_session_factory, provider, tmp_path / "lake"
    )

    assert provider.fetched == [] and _sent == []


async def test_the_scheduler_registers_the_repair_before_the_morning_signals():
    scheduler = scheduler_module.start_market_data_scheduler(
        None,  # type: ignore[arg-type]
        provider=_Provider(),  # type: ignore[arg-type]
        root=Path("unused"),
        backup_root=Path("unused"),
    )
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)

    job = jobs["market_data_history_gap_repair"]
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "7" and fields["minute"] == "30"
    assert fields["day_of_week"] == "mon-fri"
