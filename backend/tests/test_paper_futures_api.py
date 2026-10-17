"""Enrolling and listing futures subscriptions through the API (phase 4).

A futures enrolment needs the connected broker: its instrument master is the only source of the
contract and its lot size, so with none connected it is refused rather than guessed. One lot must
fit the account (an estimate of the margin, labelled as such), or the operator is told how much
capital it needs.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from src.brokers.base import FuturesContract, futures_symbol
from src.brokers.futures_contracts import NoFuturesContractError
from src.core.config import get_settings
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data.broker_provider import NoRealDataSourceError
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.engine.paper_trading.tick_feed import tick_stream_key
from src.main import app

CONTRACT = FuturesContract(
    futures_symbol("NIFTY", date(2026, 11, 23)), "NIFTY", date(2026, 11, 23), 65, 10.0
)


class _Provider:
    """A broker-shaped provider that lists NIFTY and its futures, or fails like a real one."""

    source_name = "broker_market_data"

    def __init__(self, *, contract=CONTRACT, price=24_000.0, connected=True, error=None):
        self.contract = contract
        self.price = price
        self.connected = connected
        self.error = error
        self.asked: list[tuple[str, int]] = []

    async def symbol_listed(self, symbol: str) -> bool:
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return symbol in {"NIFTY", "RELIANCE"}

    async def daily_bars(self, symbol, start, end):
        days = 100
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

    async def futures_contract(self, underlying, *, today, roll_days):
        self.asked.append((underlying, roll_days))
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        if self.error is not None:
            raise self.error
        return self.contract

    async def indicative_price(self, symbol):
        return self.price


@pytest.fixture(autouse=True)
def _wiring(redis_client, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_LAKE_PATH", str(tmp_path / "lake"))
    get_settings.cache_clear()
    app.dependency_overrides[get_redis] = lambda: redis_client
    app.dependency_overrides[get_signal_price_provider] = lambda: DataLakePriceProvider(
        root=tmp_path / "lake", real_only=True
    )
    yield
    for dependency in (get_redis, get_signal_price_provider, get_market_data_provider):
        app.dependency_overrides.pop(dependency, None)
    get_settings.cache_clear()


def _use(provider) -> None:
    app.dependency_overrides[get_market_data_provider] = lambda: provider


async def _headers(client, make_user, email: str) -> dict:
    await make_user(email, "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _version_id(client, headers, name="FutApi") -> str:
    created = await client.post(
        "/api/v1/strategies", json={"name": name, "objective": "o"}, headers=headers
    )
    return created.json()["versions"][0]["id"]


async def _enrol(client, headers, version_id, **body):
    payload = {"strategy_version_id": version_id, "symbol": "NIFTY"} | body
    return await client.post("/api/v1/paper-trading/subscriptions", json=payload, headers=headers)


# ---- enrolment ----


async def test_a_futures_enrolment_picks_the_contract_and_lot_size_from_the_broker(
    client, make_user
):
    provider = _Provider()
    _use(provider)
    headers = await _headers(client, make_user, "fut1@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        lots=2,
        initial_capital=1_000_000,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["instrument_kind"] == "future" and body["symbol"] == "NIFTY"
    assert body["contract_symbol"] == "NFO:NIFTY26NOVFUT"
    assert (body["contract_expiry"], body["lot_size"], body["lots"]) == ("2026-11-23", 65, 2)
    assert provider.asked == [("NIFTY", get_settings().paper_futures_roll_days)]


async def test_an_equity_enrolment_is_unchanged_and_asks_no_contract(client, make_user):
    provider = _Provider()
    _use(provider)
    headers = await _headers(client, make_user, "fut2@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers), symbol="RELIANCE")

    body = resp.json()
    assert resp.status_code == 201
    assert body["instrument_kind"] == "equity" and body["contract_symbol"] is None
    assert provider.asked == []


async def test_one_lot_that_does_not_fit_the_account_is_refused_with_what_it_needs(
    client, make_user
):
    _use(_Provider(price=24_000.0))
    headers = await _headers(client, make_user, "fut3@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        initial_capital=100_000,  # a lot needs ~Rs 2.34 lakh of margin: 50% of capital is Rs 50,000
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "1 lot(s) of NFO:NIFTY26NOVFUT (65 each)" in detail
    assert "234,000" in detail and "not SPAN" in detail and "initial_capital" in detail


async def test_the_fit_check_counts_every_lot(client, make_user):
    _use(_Provider(price=24_000.0))
    headers = await _headers(client, make_user, "fut4@example.com")
    version_id = await _version_id(client, headers)

    one = await _enrol(
        client, headers, version_id, instrument_kind="future", lots=1, initial_capital=500_000
    )
    two = await _enrol(
        client,
        headers,
        await _version_id(client, headers, "FutApi2"),
        instrument_kind="future",
        lots=2,
        initial_capital=500_000,
    )

    assert one.status_code == 201  # Rs 2.34 lakh <= Rs 2.5 lakh
    assert two.status_code == 422  # Rs 4.68 lakh > Rs 2.5 lakh


async def test_without_a_quote_the_fit_check_is_skipped_not_failed(client, make_user):
    _use(_Provider(price=None))
    headers = await _headers(client, make_user, "fut5@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        initial_capital=100_000,
    )

    assert resp.status_code == 201  # the entry itself is still refused later if it does not fit


async def test_with_no_broker_connected_a_futures_enrolment_is_refused(client, make_user):
    _use(_Provider(connected=False))
    headers = await _headers(client, make_user, "fut6@example.com")

    resp = await _enrol(
        client, headers, await _version_id(client, headers), instrument_kind="future"
    )

    assert resp.status_code == 422
    assert "futures need a connected broker" in resp.json()["detail"]


async def test_a_provider_with_no_instrument_master_cannot_enrol_a_future(client, make_user):
    class _GeneratedData:
        """The synthetic demo provider: no contracts, no lot sizes."""

    _use(_GeneratedData())
    headers = await _headers(client, make_user, "fut7@example.com")

    resp = await _enrol(
        client, headers, await _version_id(client, headers), instrument_kind="future"
    )

    assert resp.status_code == 422 and "instrument master" in resp.json()["detail"]


async def test_an_underlying_with_no_listed_future_is_refused_with_the_reason(client, make_user):
    _use(_Provider(error=NoFuturesContractError("no futures contract for NIFTY (zerodha: none)")))
    headers = await _headers(client, make_user, "fut8@example.com")

    resp = await _enrol(
        client, headers, await _version_id(client, headers), instrument_kind="future"
    )

    assert resp.status_code == 422 and "no futures contract for NIFTY" in resp.json()["detail"]


async def test_an_unlisted_underlying_is_refused_before_any_contract_is_looked_up(
    client, make_user
):
    provider = _Provider()
    _use(provider)
    headers = await _headers(client, make_user, "fut9@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        symbol="NOSUCH",
        instrument_kind="future",
    )

    assert resp.status_code == 422 and "no connected broker lists" in resp.json()["detail"]
    assert provider.asked == []


@pytest.mark.parametrize("lots", [0, -1, 1001])
async def test_lots_must_be_a_sensible_whole_number(client, make_user, lots):
    _use(_Provider())
    headers = await _headers(client, make_user, f"fut10-{lots}@example.com")

    resp = await _enrol(
        client, headers, await _version_id(client, headers), instrument_kind="future", lots=lots
    )

    assert resp.status_code == 422


async def test_the_stock_and_its_future_are_separate_subscriptions_but_two_futures_are_not(
    client, make_user
):
    _use(_Provider())
    headers = await _headers(client, make_user, "fut11@example.com")
    version_id = await _version_id(client, headers)

    stock = await _enrol(client, headers, version_id, symbol="RELIANCE")
    future = await _enrol(
        client,
        headers,
        version_id,
        symbol="RELIANCE",
        instrument_kind="future",
        initial_capital=2_000_000,
    )
    again = await _enrol(
        client,
        headers,
        version_id,
        symbol="RELIANCE",
        instrument_kind="future",
        initial_capital=2_000_000,
    )

    assert (stock.status_code, future.status_code) == (201, 201)
    assert again.status_code == 409


# ---- listing ----


async def test_the_list_shows_the_contract_lots_and_which_symbol_is_ticking(
    client, make_user, redis_client
):
    _use(_Provider())
    headers = await _headers(client, make_user, "fut12@example.com")
    enrolled = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        lots=3,
        initial_capital=2_000_000,
    )
    await redis_client.delete(tick_stream_key("NFO:NIFTY26NOVFUT"))

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == enrolled.json()["id"])
    assert row["instrument_kind"] == "future"
    assert (row["contract_symbol"], row["lot_size"], row["lots"]) == ("NFO:NIFTY26NOVFUT", 65, 3)
    assert row["contract_expiry"] == "2026-11-23"


async def test_a_contract_that_expired_without_a_roll_is_shown_as_halted_with_why(
    client, make_user, db_session_factory
):
    from src.models.paper_trading_subscription import PaperTradingSubscription

    yesterday = date.today() - timedelta(days=1)
    expired = FuturesContract(futures_symbol("NIFTY", yesterday), "NIFTY", yesterday, 65, 10.0)
    _use(_Provider(contract=expired))
    headers = await _headers(client, make_user, "fut13@example.com")
    enrolled = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        initial_capital=2_000_000,
    )
    assert enrolled.status_code == 201, enrolled.text

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == enrolled.json()["id"])
    assert row["state"] == "halted_no_data"
    assert "expired on" in row["state_reason"] and "has not been rolled" in row["state_reason"]

    async with db_session_factory() as db:  # an inactive one is just inactive
        (
            await db.get(PaperTradingSubscription, __import__("uuid").UUID(row["id"]))
        ).is_active = False
        await db.commit()
    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)
    assert next(r for r in listed.json() if r["id"] == row["id"])["state"] == "inactive"


async def test_the_contract_is_chosen_with_the_configured_roll_window(
    client, make_user, monkeypatch
):
    monkeypatch.setenv("PAPER_FUTURES_ROLL_DAYS", "7")
    get_settings.cache_clear()
    provider = _Provider()
    _use(provider)
    headers = await _headers(client, make_user, "fut14@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="future",
        initial_capital=1_000_000,
    )

    assert resp.status_code == 201
    assert provider.asked == [("NIFTY", 7)]
