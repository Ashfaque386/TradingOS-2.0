"""Closing, deactivating, listing and enrolling option subscriptions; and the jobs that serve them
(phase 5). An option strategy holds a position per leg, so each control works on every leg: sold
legs are bought back before the bought leg that hedges them is sold, and a sold leg that cannot be
closed keeps its hedge open.
"""

import time
import uuid
from datetime import date, timedelta

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.brokers.base import OptionContract, option_symbol
from src.core.config import get_settings
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data.broker_provider import NoRealDataSourceError
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.engine.paper_trading.depth import (
    DepthSnapshot,
    RealDepthOrderBookProvider,
    depth_key,
    publish_depth,
)
from src.engine.paper_trading.order_book import OrderBookLevel
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.main import app
from src.models.daily_signal import DailySignal
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration import paper_trading_scheduler as scheduler_module
from src.orchestration.paper_options import plan_key
from src.orchestration.paper_subscription_controls import (
    CloseBlockedError,
    PriceUnavailableError,
    SubscriptionStateError,
    close_all_positions,
    close_position,
    deactivate_subscription,
)
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()
EXPIRY = date.today() + timedelta(days=20)
HEDGE = option_symbol("NIFTY", EXPIRY, 24600.0, "CE")
SOLD = option_symbol("NIFTY", EXPIRY, 24500.0, "CE")
OTHER = option_symbol("NIFTY", EXPIRY, 24500.0, "PE")


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [k for s in (HEDGE, SOLD, OTHER, "NIFTY") for k in (depth_key(s), tick_stream_key(s))]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _subscription(db_session_factory, *, strategy="vertical_spread", capital=1_000_000.0):
    async with db_session_factory() as db:
        created = await create_strategy(db, name=f"CTL-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, created, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=capital,
            stop_loss_pct=50.0,
            option_strategy=strategy,
        )


async def _hold(db_session_factory, subscription, symbol, quantity, avg_cost=100.0):
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id,
                symbol=symbol,
                quantity=quantity,
                avg_cost=avg_cost,
                realized_pnl=0.0,
            )
        )
        await db.commit()


async def _spread(db_session_factory):
    """A bear call spread: the hedge bought, the at-the-money call sold."""
    subscription = await _subscription(db_session_factory)
    await _hold(db_session_factory, subscription, HEDGE, 65, 20.0)
    await _hold(db_session_factory, subscription, SOLD, -65, 60.0)
    return subscription


async def _quote(redis, symbol, price, *, book=True, tick=True):
    if book:
        await publish_depth(
            redis,
            symbol,
            DepthSnapshot(
                bids=(OrderBookLevel(price - 1.0, 1_000),),
                asks=(OrderBookLevel(price + 1.0, 1_000),),
                timestamp_ms=_now_ms(),
            ),
            ttl_seconds=60,
        )
    if tick:
        await publish_tick(redis, Tick(symbol, price, _now_ms(), "real"))


def _providers(redis):
    return {
        "order_book_provider": RealDepthOrderBookProvider(redis, max_age_seconds=30),
        "regulatory_provider": _REGULATORY,
    }


async def _quantities(db_session_factory, subscription) -> dict[str, int]:
    async with db_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
                )
            )
            .scalars()
            .all()
        )
    return {row.symbol: row.quantity for row in rows}


# ---- the position is a leg ----


async def test_a_subscription_holds_one_position_per_contract_but_not_two_in_the_same_one(
    db_session_factory,
):
    subscription = await _spread(db_session_factory)

    assert await _quantities(db_session_factory, subscription) == {HEDGE: 65, SOLD: -65}
    async with db_session_factory() as db:
        db.add(
            PaperPosition(subscription_id=subscription.id, symbol=SOLD, quantity=1, avg_cost=1.0)
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_closing_an_option_subscription_without_naming_a_leg_is_refused(
    db_session_factory, redis
):
    subscription = await _spread(db_session_factory)

    async with db_session_factory() as db:
        with pytest.raises(SubscriptionStateError, match="position per leg"):
            await close_position(
                db,
                redis,
                subscription.id,
                actor="t",
                reason="r",
                price=None,
                administrative=False,
                **_providers(redis),
            )


# ---- closing every leg ----


async def test_every_leg_is_closed_the_sold_one_first(db_session_factory, redis):
    subscription = await _spread(db_session_factory)
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0)

    async with db_session_factory() as db:
        outcome = await close_all_positions(
            db, redis, subscription.id, actor="t", reason="done", **_providers(redis)
        )

    assert outcome.all_flat and outcome.failures == []
    assert [(r.fill.symbol, r.fill.side) for r in outcome.closed] == [
        (SOLD, "buy"),
        (HEDGE, "sell"),
    ]
    assert await _quantities(db_session_factory, subscription) == {HEDGE: 0, SOLD: 0}


async def test_a_sold_leg_that_cannot_be_closed_keeps_its_hedge_open(db_session_factory, redis):
    subscription = await _spread(db_session_factory)
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0, book=False)

    async with db_session_factory() as db:
        outcome = await close_all_positions(
            db, redis, subscription.id, actor="t", reason="done", **_providers(redis)
        )

    assert outcome.closed == [] and not outcome.all_flat
    assert any(SOLD in failure for failure in outcome.failures)
    assert any("hedge" in failure for failure in outcome.failures)
    assert await _quantities(db_session_factory, subscription) == {HEDGE: 65, SOLD: -65}


async def test_a_failed_bought_leg_after_the_sold_one_is_reported_and_the_sold_one_stays_closed(
    db_session_factory, redis
):
    subscription = await _spread(db_session_factory)
    await _quote(redis, SOLD, 60.0)
    await _quote(redis, HEDGE, 20.0, book=False)

    async with db_session_factory() as db:
        outcome = await close_all_positions(
            db, redis, subscription.id, actor="t", reason="done", **_providers(redis)
        )

    assert [r.fill.symbol for r in outcome.closed] == [SOLD] and not outcome.all_flat
    assert any(HEDGE in failure for failure in outcome.failures)
    assert await _quantities(db_session_factory, subscription) == {HEDGE: 65, SOLD: 0}


async def test_with_one_open_position_the_failure_is_the_original_error(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, strategy="long_option")
    await _hold(db_session_factory, subscription, HEDGE, 65)

    async with db_session_factory() as db:
        with pytest.raises(
            PriceUnavailableError
        ):  # no tick ever: the same 422 a single close gives
            await close_all_positions(
                db, redis, subscription.id, actor="t", reason="r", **_providers(redis)
            )


async def test_with_nothing_open_there_is_nothing_to_close(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    async with db_session_factory() as db:
        with pytest.raises(SubscriptionStateError, match="no open position"):
            await close_all_positions(
                db, redis, subscription.id, actor="t", reason="r", **_providers(redis)
            )


# ---- deactivating ----


async def _deactivate(db_session_factory, redis, subscription, decision):
    async with db_session_factory() as db:
        return await deactivate_subscription(
            db,
            redis,
            subscription.id,
            actor="t",
            reason="wrap up",
            on_open_position=decision,
            close_price=None,
            **_providers(redis),
        )


async def test_deactivating_with_open_legs_demands_a_decision_and_names_them(
    db_session_factory, redis
):
    subscription = await _spread(db_session_factory)

    with pytest.raises(SubscriptionStateError) as caught:
        await _deactivate(db_session_factory, redis, subscription, None)

    assert HEDGE in str(caught.value) and SOLD in str(caught.value)
    assert "+65" in str(caught.value) and "-65" in str(caught.value)


async def test_deactivating_with_close_closes_every_leg_first(db_session_factory, redis):
    subscription = await _spread(db_session_factory)
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0)

    result = await _deactivate(db_session_factory, redis, subscription, "close")

    assert result.subscription.is_active is False and result.left_unmanaged_quantity == 0
    assert await _quantities(db_session_factory, subscription) == {HEDGE: 0, SOLD: 0}


async def test_deactivating_with_close_stays_active_if_a_leg_could_not_close(
    db_session_factory, redis
):
    subscription = await _spread(db_session_factory)
    await _quote(redis, SOLD, 60.0)
    await _quote(redis, HEDGE, 20.0, book=False)

    with pytest.raises(SubscriptionStateError, match="partly closed"):
        await _deactivate(db_session_factory, redis, subscription, "close")

    async with db_session_factory() as db:
        assert (await db.get(PaperTradingSubscription, subscription.id)).is_active is True


async def test_with_no_leg_closable_the_deactivation_says_why(db_session_factory, redis):
    subscription = await _spread(db_session_factory)  # no ticks, no books at all

    with pytest.raises(CloseBlockedError):
        await _deactivate(db_session_factory, redis, subscription, "close")


async def test_leaving_the_legs_unmanaged_reports_every_contract_left_open(
    db_session_factory, redis
):
    subscription = await _spread(db_session_factory)

    result = await _deactivate(db_session_factory, redis, subscription, "leave_unmanaged")

    assert result.subscription.is_active is False
    assert result.left_unmanaged_quantity == 130  # 65 bought + 65 sold


# ---- the API ----


class _Provider:
    source_name = "broker_market_data"

    def __init__(self, *, contracts=None, connected=True):
        self.contracts = contracts if contracts is not None else _chain()
        self.connected = connected

    async def symbol_listed(self, symbol: str) -> bool:
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return symbol in {"NIFTY", "RELIANCE"}

    async def daily_bars(self, symbol, start, end):
        return pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": [100.0 + i for i in range(100)],
                "volume": 1000,
                "source": "zerodha",
            },
            index=pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=100),
        )

    async def option_contracts(self, underlying):
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return self.contracts


def _chain():
    return [
        OptionContract(option_symbol("NIFTY", EXPIRY, s, k), "NIFTY", EXPIRY, s, k, 65, 5.0)
        for s in (24400.0, 24500.0, 24600.0)
        for k in ("CE", "PE")
    ]


@pytest.fixture
def api(redis_client, tmp_path, monkeypatch):
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


async def _version_id(client, headers, name="OptApi") -> str:
    created = await client.post(
        "/api/v1/strategies", json={"name": name, "objective": "o"}, headers=headers
    )
    return created.json()["versions"][0]["id"]


async def _enrol(client, headers, version_id, **body):
    payload = {"strategy_version_id": version_id, "symbol": "NIFTY", "instrument_kind": "option"}
    return await client.post(
        "/api/v1/paper-trading/subscriptions", json=payload | body, headers=headers
    )


async def test_an_option_enrolment_stores_the_strategy_and_a_premium_sized_stop(
    client, make_user, api
):
    _use(_Provider())
    headers = await _headers(client, make_user, "opt1@example.com")

    resp = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        option_strategy="vertical_spread",
        spread_width=3,
        lots=2,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["instrument_kind"] == "option" and body["option_strategy"] == "vertical_spread"
    assert (body["spread_width"], body["lots"]) == (3, 2)
    assert body["contract_symbol"] is None and body["symbol"] == "NIFTY"
    assert body["stop_loss_pct"] == 50.0  # 3% of a premium would be noise


async def test_an_explicit_stop_is_kept_and_the_other_kinds_keep_their_default(
    client, make_user, api
):
    _use(_Provider())
    headers = await _headers(client, make_user, "opt2@example.com")

    explicit = await _enrol(client, headers, await _version_id(client, headers), stop_loss_pct=30.0)
    equity = await _enrol(
        client,
        headers,
        await _version_id(client, headers, "OptApiEq"),
        instrument_kind="equity",
        symbol="RELIANCE",
    )

    assert explicit.json()["stop_loss_pct"] == 30.0
    assert equity.json()["stop_loss_pct"] == 3.0  # unchanged for shares


async def test_the_long_option_strategy_is_the_default(client, make_user, api):
    _use(_Provider())
    headers = await _headers(client, make_user, "opt3@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.json()["option_strategy"] == "long_option" and resp.json()["spread_width"] == 2


async def test_an_unknown_strategy_or_a_silly_width_is_refused(client, make_user, api):
    _use(_Provider())
    headers = await _headers(client, make_user, "opt4@example.com")
    version_id = await _version_id(client, headers)

    assert (
        await _enrol(client, headers, version_id, option_strategy="straddle")
    ).status_code == 422
    assert (await _enrol(client, headers, version_id, spread_width=0)).status_code == 422
    assert (await _enrol(client, headers, version_id, spread_width=21)).status_code == 422


async def test_with_no_broker_connected_an_option_enrolment_is_refused(client, make_user, api):
    _use(_Provider(connected=False))
    headers = await _headers(client, make_user, "opt5@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.status_code == 422 and "options need a connected broker" in resp.json()["detail"]


async def test_a_provider_with_no_instrument_master_cannot_enrol_options(client, make_user, api):
    class _GeneratedData:
        """The synthetic demo provider: no contracts, no lot sizes."""

    _use(_GeneratedData())
    headers = await _headers(client, make_user, "opt6@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.status_code == 422 and "instrument master" in resp.json()["detail"]


async def test_an_underlying_with_no_expiry_far_enough_out_is_refused_with_the_reason(
    client, make_user, api
):
    near = [
        OptionContract(
            option_symbol("NIFTY", date.today() + timedelta(days=1), 24500.0, "CE"),
            "NIFTY",
            date.today() + timedelta(days=1),
            24500.0,
            "CE",
            65,
            5.0,
        )
    ]
    _use(_Provider(contracts=near))
    headers = await _headers(client, make_user, "opt7@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.status_code == 422
    assert "no listed NIFTY option expires more than 3 days from now" in resp.json()["detail"]


async def test_an_underlying_the_broker_lists_no_options_for_says_so(client, make_user, api):
    _use(_Provider(contracts=[]))
    headers = await _headers(client, make_user, "opt8@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.status_code == 422 and "the broker lists none" in resp.json()["detail"]


async def test_the_minimum_days_setting_reaches_the_enrolment_check(
    client, make_user, api, monkeypatch
):
    tomorrow = date.today() + timedelta(days=1)
    near = [
        OptionContract(
            option_symbol("NIFTY", tomorrow, 24500.0, "CE"),
            "NIFTY",
            tomorrow,
            24500.0,
            "CE",
            65,
            5.0,
        )
    ]
    _use(_Provider(contracts=near))
    monkeypatch.setenv("PAPER_OPTIONS_MIN_DAYS_TO_EXPIRY", "1")
    get_settings.cache_clear()
    headers = await _headers(client, make_user, "opt9@example.com")

    resp = await _enrol(client, headers, await _version_id(client, headers))

    assert resp.status_code == 201


async def test_an_option_strategy_on_a_symbol_and_the_stock_itself_are_separate_subscriptions(
    client, make_user, api
):
    _use(_Provider())
    headers = await _headers(client, make_user, "opt10@example.com")
    version_id = await _version_id(client, headers)

    option = await _enrol(client, headers, version_id)
    stock = await _enrol(client, headers, version_id, instrument_kind="equity")
    again = await _enrol(client, headers, version_id)

    assert (option.status_code, stock.status_code) == (201, 201)
    assert again.status_code == 409


# ---- listing and the position endpoints ----


async def _enrol_and_hold(client, make_user, db_session_factory, email):
    _use(_Provider())
    headers = await _headers(client, make_user, email)
    enrolled = await _enrol(client, headers, await _version_id(client, headers))
    subscription_id = uuid.UUID(enrolled.json()["id"])
    async with db_session_factory() as db:
        for symbol, quantity, cost, realized, costs in (
            (HEDGE, 65, 20.0, 100.0, 30.0),
            (SOLD, -65, 60.0, 200.0, 50.0),
            (OTHER, 0, 0.0, 400.0, 20.0),  # a leg closed earlier: flat, but its results count
        ):
            db.add(
                PaperPosition(
                    subscription_id=subscription_id,
                    symbol=symbol,
                    quantity=quantity,
                    avg_cost=cost,
                    realized_pnl=realized,
                    costs_total=costs,
                )
            )
        await db.commit()
    return headers, str(subscription_id)


async def test_the_list_shows_open_legs_summed_results_and_a_mark_to_market_across_legs(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optlist1@example.com"
    )
    await _quote(redis, HEDGE, 25.0, book=False)  # +5 a unit on the bought leg
    await _quote(redis, SOLD, 50.0, book=False)  # +10 a unit on the sold leg
    await _quote(redis, "NIFTY", 24_510.0, book=False)

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == subscription_id)
    assert row["open_legs"] == 2  # the flat leg is not open
    assert (row["quantity"], row["avg_cost"]) == (0, 0.0)  # meaningless across legs
    assert row["realized_pnl"] == 700.0 and row["costs_total"] == 100.0  # every leg, flat included
    assert row["net_realized_pnl"] == 600.0
    assert row["unrealized_pnl"] == pytest.approx(65 * 5.0 + 65 * 10.0)  # (25-20)*65 + (50-60)*-65
    assert row["last_tick"]["price"] == 24_510.0  # the underlying's tick
    assert row["managed"] is True


async def test_a_leg_that_was_never_priced_makes_the_mark_to_market_unknown_not_zero(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optlist2@example.com"
    )
    await _quote(redis, HEDGE, 25.0, book=False)  # the sold leg has no tick

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == subscription_id)
    assert row["unrealized_pnl"] is None


async def test_an_option_subscription_with_nothing_open_has_a_zero_mark(
    client, make_user, api, db_session_factory
):
    _use(_Provider())
    headers = await _headers(client, make_user, "optlist3@example.com")
    enrolled = await _enrol(client, headers, await _version_id(client, headers))

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == enrolled.json()["id"])
    assert row["open_legs"] == 0 and row["unrealized_pnl"] == 0.0


async def test_open_legs_on_an_inactive_subscription_are_reported_as_unmanaged(
    client, make_user, api, db_session_factory
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optlist4@example.com"
    )
    async with db_session_factory() as db:
        (await db.get(PaperTradingSubscription, uuid.UUID(subscription_id))).is_active = False
        await db.commit()

    listed = await client.get("/api/v1/paper-trading/subscriptions", headers=headers)

    row = next(r for r in listed.json() if r["id"] == subscription_id)
    assert row["managed"] is False and row["open_legs"] == 2


async def test_the_positions_endpoint_lists_every_leg_flat_ones_included(
    client, make_user, api, db_session_factory
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optpos1@example.com"
    )

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/positions", headers=headers
    )

    legs = {p["symbol"]: p for p in resp.json()}
    assert set(legs) == {HEDGE, SOLD, OTHER}
    assert legs[HEDGE]["quantity"] == 65 and legs[SOLD]["quantity"] == -65
    assert legs[OTHER]["quantity"] == 0 and legs[OTHER]["net_realized_pnl"] == 380.0


async def test_the_single_position_endpoint_points_an_option_subscription_to_the_legs(
    client, make_user, api, db_session_factory
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optpos2@example.com"
    )

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=headers
    )

    assert resp.status_code == 409 and "GET .../positions" in resp.json()["detail"]


async def test_the_positions_of_an_unknown_subscription_are_404(client, make_user, api):
    headers = await _headers(client, make_user, "optpos3@example.com")

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{uuid.uuid4()}/positions", headers=headers
    )

    assert resp.status_code == 404


async def test_the_position_endpoint_is_unchanged_for_an_equity_subscription(
    client, make_user, api
):
    _use(_Provider())
    headers = await _headers(client, make_user, "optpos4@example.com")
    enrolled = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="equity",
        symbol="RELIANCE",
    )

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{enrolled.json()['id']}/position", headers=headers
    )

    assert resp.status_code == 404 and resp.json()["detail"] == "No position yet"


# ---- the close route ----


async def test_the_close_route_closes_every_leg_and_lists_each_fill(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optclose1@example.com"
    )
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/close-position",
        json={"reason": "taking it off"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["closed"] is True and body["remaining_quantity"] == 0 and body["failures"] == []
    assert [(f["symbol"], f["side"]) for f in body["legs"]] == [(SOLD, "buy"), (HEDGE, "sell")]
    assert body["fill"]["symbol"] == SOLD  # the first leg's fill is the headline


async def test_the_close_route_reports_a_leg_that_would_not_close(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optclose2@example.com"
    )
    await _quote(redis, SOLD, 60.0)
    await _quote(redis, HEDGE, 20.0, book=False)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/close-position",
        json={"reason": "taking it off"},
        headers=headers,
    )

    body = resp.json()
    assert resp.status_code == 200
    assert body["closed"] is False and any(HEDGE in f for f in body["failures"])
    assert [f["symbol"] for f in body["legs"]] == [SOLD]


async def test_the_close_route_is_a_409_when_no_leg_could_close(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optclose3@example.com"
    )
    await _quote(redis, SOLD, 60.0, book=False)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/close-position",
        json={"reason": "taking it off"},
        headers=headers,
    )

    assert resp.status_code == 409 and SOLD in resp.json()["detail"]


async def test_the_deactivate_route_with_close_closes_the_legs(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optdeact1@example.com"
    )
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0)

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/deactivate",
        json={"reason": "wrap up", "on_open_position": "close"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription"]["is_active"] is False
    assert resp.json()["left_unmanaged_quantity"] == 0


# ---- the jobs ----


async def test_the_tick_job_quotes_the_underlying_the_held_legs_and_the_planned_ones(
    db_session_factory, redis, monkeypatch
):
    subscription = await _spread(db_session_factory)
    await redis.set(
        plan_key(subscription.id),
        '{"direction": "bearish", "strategy": "vertical_spread", "expiry": "2030-01-01", '
        f'"legs": [{{"symbol": "{OTHER}", "side": "buy", "quantity": 65, "strike": 24500.0, '
        '"option_type": "PE"}]}',
        px=60_000,
    )
    asked: list[str] = []

    class _Source:
        async def next_tick(self, symbol: str) -> Tick:
            asked.append(symbol)
            return Tick(symbol=symbol, price=100.0, timestamp_ms=_now_ms())

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await scheduler_module.run_tick_publish_job(db_session_factory, redis, _Source)

    assert sorted(asked) == sorted({"NIFTY", HEDGE, SOLD, OTHER})
    await redis.delete(plan_key(subscription.id))


async def test_the_drain_job_runs_the_option_logic_on_the_underlyings_ticks(
    db_session_factory, redis, monkeypatch
):
    subscription = await _subscription(db_session_factory, strategy="long_option")
    async with db_session_factory() as db:
        db.add(
            DailySignal(
                subscription_id=subscription.id,
                symbol="NIFTY",
                signal_type="BUY",
                reference_price=24_500.0,
            )
        )
        await db.commit()
    await publish_tick(redis, Tick("NIFTY", 24_510.0, _now_ms(), "real"))
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    async def contracts(underlying):
        return _chain()

    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis,
        RealDepthOrderBookProvider(redis, max_age_seconds=30),
        _REGULATORY,
        contracts,
    )

    assert await redis.get(plan_key(subscription.id)) is not None  # the legs were chosen
    async with db_session_factory() as db:
        stored = await db.get(PaperTradingSubscription, subscription.id)
    newest = (await redis.xrevrange(tick_stream_key("NIFTY"), count=1))[0][0]
    assert stored.tick_cursor == newest  # the cursor is on the underlying's stream
    await redis.delete(plan_key(subscription.id))


async def test_the_expiry_sweep_job_waits_for_the_market(db_session_factory, redis, monkeypatch):
    from datetime import datetime

    from src.engine.paper_trading.market_hours import IST

    subscription = await _subscription(db_session_factory)
    today = datetime.now(IST).date()
    due = option_symbol("NIFTY", today, 24500.0, "CE")
    await _hold(db_session_factory, subscription, due, 65)
    await _quote(redis, due, 80.0)

    async def run():
        await scheduler_module.run_expiry_sweep_job(
            db_session_factory,
            redis,
            RealDepthOrderBookProvider(redis, max_age_seconds=30),
            _REGULATORY,
        )

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    await run()
    assert await _quantities(db_session_factory, subscription) == {due: 65}  # shut: untouched

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    await run()
    assert await _quantities(db_session_factory, subscription) == {due: 0}  # open: squared off
    await redis.delete(depth_key(due), tick_stream_key(due))


async def test_closing_option_legs_is_not_held_to_the_cash_equity_position_limit(
    db_session_factory, redis
):
    """Buying back 65 of a call at 60 is Rs 3,900 of premium -- more than 10% of a Rs 20,000
    account, the limit that suits shares. A premium is not a share position: the close goes
    through."""
    subscription = await _subscription(db_session_factory, capital=20_000.0)
    await _hold(db_session_factory, subscription, HEDGE, 65, 20.0)
    await _hold(db_session_factory, subscription, SOLD, -65, 60.0)
    await _quote(redis, HEDGE, 20.0)
    await _quote(redis, SOLD, 60.0)

    async with db_session_factory() as db:
        outcome = await close_all_positions(
            db, redis, subscription.id, actor="t", reason="done", **_providers(redis)
        )

    assert outcome.all_flat and outcome.failures == []


async def test_the_expiry_sweep_job_uses_the_configured_squareoff_window(
    db_session_factory, redis, monkeypatch
):
    from datetime import datetime

    from src.engine.paper_trading.market_hours import IST

    subscription = await _subscription(db_session_factory)
    in_three_days = option_symbol(
        "NIFTY", datetime.now(IST).date() + timedelta(days=3), 24500.0, "CE"
    )
    await _hold(db_session_factory, subscription, in_three_days, 65)
    await _quote(redis, in_three_days, 80.0)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    async def run():
        await scheduler_module.run_expiry_sweep_job(
            db_session_factory,
            redis,
            RealDepthOrderBookProvider(redis, max_age_seconds=30),
            _REGULATORY,
        )

    await run()  # the default: one day before expiry, so three days out is left alone
    assert await _quantities(db_session_factory, subscription) == {in_three_days: 65}

    monkeypatch.setenv("PAPER_OPTIONS_SQUAREOFF_DAYS", "3")
    get_settings.cache_clear()
    await run()
    assert await _quantities(db_session_factory, subscription) == {in_three_days: 0}
    await redis.delete(depth_key(in_three_days), tick_stream_key(in_three_days))


# ---- marks on the positions endpoint (the paper-trading desk reads them) ----


async def test_an_open_leg_carries_its_mark_its_age_and_its_unrealized_pnl(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optmark1@example.com"
    )
    await _quote(redis, HEDGE, 25.0, book=False)  # bought 65 at 20
    await _quote(redis, SOLD, 50.0, book=False)  # sold 65 at 60

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/positions", headers=headers
    )

    legs = {p["symbol"]: p for p in resp.json()}
    assert legs[HEDGE]["mark_price"] == 25.0
    assert legs[HEDGE]["unrealized_pnl"] == pytest.approx(65 * (25.0 - 20.0))
    assert legs[SOLD]["mark_price"] == 50.0
    assert legs[SOLD]["unrealized_pnl"] == pytest.approx(-65 * (50.0 - 60.0))  # short: a gain
    assert 0 <= legs[HEDGE]["mark_age_seconds"] < 30


async def test_a_flat_leg_has_no_mark_even_if_the_contract_still_ticks(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optmark2@example.com"
    )
    await _quote(redis, OTHER, 90.0, book=False)  # a leg closed earlier, still quoted

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/positions", headers=headers
    )

    flat = next(p for p in resp.json() if p["symbol"] == OTHER)
    assert flat["quantity"] == 0
    assert flat["mark_price"] is None and flat["unrealized_pnl"] is None


async def test_an_open_leg_never_priced_is_unknown_not_zero(
    client, make_user, api, db_session_factory, redis
):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optmark3@example.com"
    )
    await _quote(redis, HEDGE, 25.0, book=False)  # the sold leg has never had a tick

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/positions", headers=headers
    )

    sold = next(p for p in resp.json() if p["symbol"] == SOLD)
    assert sold["quantity"] == -65
    assert sold["mark_price"] is None and sold["mark_age_seconds"] is None
    assert sold["unrealized_pnl"] is None


async def test_a_mark_that_is_old_says_how_old(client, make_user, api, db_session_factory, redis):
    headers, subscription_id = await _enrol_and_hold(
        client, make_user, db_session_factory, "optmark4@example.com"
    )
    await publish_tick(redis, Tick(HEDGE, 25.0, _now_ms() - 600_000, "real"))  # ten minutes ago

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/positions", headers=headers
    )

    hedge = next(p for p in resp.json() if p["symbol"] == HEDGE)
    assert hedge["mark_age_seconds"] >= 590 and hedge["mark_price"] == 25.0


async def test_the_other_position_endpoints_carry_no_mark(
    client, make_user, api, db_session_factory, redis
):
    _use(_Provider())
    headers = await _headers(client, make_user, "optmark5@example.com")
    enrolled = await _enrol(
        client,
        headers,
        await _version_id(client, headers),
        instrument_kind="equity",
        symbol="RELIANCE",
    )
    subscription_id = uuid.UUID(enrolled.json()["id"])
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription_id, symbol="RELIANCE", quantity=10, avg_cost=1500.0
            )
        )
        await db.commit()

    single = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription_id}/position", headers=headers
    )

    assert single.status_code == 200
    assert single.json()["mark_price"] is None  # only the legs listing prices a position
