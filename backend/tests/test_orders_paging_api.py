"""The execution ledger and the live lists, paged and filtered by the database: how many rows match
in all (`X-Total-Count`), the filters the Orders page sets, and what a row rests on.

Times are set explicitly: a row's default time is the transaction's start, so rows written
together would tie and their order would mean nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.core.roles import Role
from src.models.live_order_intent import LiveOrderIntent
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.trade import Trade
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


async def _headers(client, make_user) -> dict[str, str]:
    await make_user("orders-reader@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "orders-reader@example.com", "password": "supersecret1"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _fill(subscription_id, symbol, minutes_ago, **over) -> PaperFill:
    values = dict(
        subscription_id=subscription_id,
        symbol=symbol,
        side="buy",
        order_group_id=uuid.uuid4(),
        requested_quantity=10,
        filled_quantity=10,
        avg_fill_price=100.0,
        fully_filled=True,
        data_source="synthetic",
        costs_total=0.0,
        created_at=NOW - timedelta(minutes=minutes_ago),
    )
    values.update(over)
    return PaperFill(**values)


async def _seed(db_session_factory) -> dict[str, uuid.UUID]:
    """Newest first: live INFY failed (1m), live INFY filled (2m), paper RELIANCE (3m), paper TCS
    partial (4m), paper reliance (5m)."""
    async with db_session_factory() as db:
        alpha = await create_strategy(db, name="Alpha", objective="obj")
        beta = await create_strategy(db, name="Beta", objective="obj")
        version = await create_version_with_validation(db, alpha, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add_all(
            [
                _fill(subscription.id, "RELIANCE", 3, costs_total=12.5, avg_fill_price=2500.0),
                _fill(
                    subscription.id,
                    "TCS",
                    4,
                    filled_quantity=4,
                    fully_filled=False,
                    data_source="real",
                ),
                _fill(subscription.id, "reliance", 5, administrative=True),
            ]
        )
        intent = LiveOrderIntent(
            strategy_id=beta.id,
            symbol="INFY",
            side="buy",
            quantity=5,
            intent_type="entry",
            expires_at=NOW + timedelta(minutes=5),
            status="submitted",
        )
        db.add(intent)
        await db.flush()
        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=beta.id,
            symbol="INFY",
            side="buy",
            quantity=5,
            broker_name="zerodha",
            broker_order_id="OID-1",
            status="submitted",
            submitted_at=NOW,
        )
        db.add(order)
        await db.flush()
        db.add_all(
            [
                Trade(
                    order_id=order.id,
                    symbol="INFY",
                    side="buy",
                    quantity=5,
                    price=1490.0,
                    fill_price=1500.0,
                    status="filled",
                    data_source="real",
                    executed_at=NOW - timedelta(minutes=2),
                ),
                Trade(
                    order_id=order.id,
                    symbol="INFY",
                    side="buy",
                    quantity=5,
                    price=1490.0,
                    status="failed",
                    data_source="real",
                    executed_at=NOW - timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()
        return {"alpha": alpha.id, "beta": beta.id, "order": order.id}


async def _get(client, headers, **params):
    return await client.get("/api/v1/orders", params=params, headers=headers)


async def test_the_total_is_every_execution_whatever_the_page(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    first = await _get(client, headers, limit=2)
    last = await _get(client, headers, limit=2, offset=4)

    assert [r["symbol"] for r in first.json()] == ["INFY", "INFY"]
    assert first.headers["x-total-count"] == "5"
    assert len(last.json()) == 1 and last.headers["x-total-count"] == "5"


async def test_paper_and_live_are_merged_newest_first_and_paged_across_each_other(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    pages = [(await _get(client, headers, limit=2, offset=o)).json() for o in (0, 2, 4)]

    rows = [r for page in pages for r in page]
    assert [(r["mode"], r["symbol"], r["status"]) for r in rows] == [
        ("live", "INFY", "failed"),
        ("live", "INFY", "filled"),
        ("paper", "RELIANCE", "filled"),
        ("paper", "TCS", "partial"),
        ("paper", "reliance", "filled"),
    ]


async def test_the_mode_filter_counts_only_that_mode(client, make_user, db_session_factory):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    paper = await _get(client, headers, mode="paper")
    live = await _get(client, headers, mode="live")

    assert paper.headers["x-total-count"] == "3" and {r["mode"] for r in paper.json()} == {"paper"}
    assert live.headers["x-total-count"] == "2" and {r["mode"] for r in live.json()} == {"live"}


async def test_the_status_filter_takes_a_live_status_and_a_paper_status(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    partial = await _get(client, headers, status="partial")
    failed = await _get(client, headers, status="failed")
    filled = await _get(client, headers, status="filled")

    assert [r["symbol"] for r in partial.json()] == ["TCS"]
    assert [r["mode"] for r in failed.json()] == ["live"]
    assert filled.headers["x-total-count"] == "3"  # two paper fills and one live trade


async def test_a_status_nothing_has_is_refused_not_answered_with_nothing(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)

    assert (await _get(client, headers, status="OPEN")).status_code == 422


async def test_the_symbol_filter_is_a_case_insensitive_contains(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    resp = await _get(client, headers, symbol="reLIA")

    assert resp.headers["x-total-count"] == "2"
    assert {r["symbol"] for r in resp.json()} == {"RELIANCE", "reliance"}


async def test_a_wildcard_typed_into_the_symbol_filter_is_looked_for_not_applied(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    await _seed(db_session_factory)

    assert (await _get(client, headers, symbol="%")).json() == []
    assert (await _get(client, headers, symbol="I_FY")).json() == []


async def test_the_strategy_filter_follows_a_paper_fill_to_its_strategy_too(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    ids = await _seed(db_session_factory)

    alpha = await _get(client, headers, strategy_id=str(ids["alpha"]))
    beta = await _get(client, headers, strategy_id=str(ids["beta"]))

    assert alpha.headers["x-total-count"] == "3" and {r["mode"] for r in alpha.json()} == {"paper"}
    assert beta.headers["x-total-count"] == "2" and {r["mode"] for r in beta.json()} == {"live"}
    assert {r["strategy_name"] for r in alpha.json()} == {"Alpha"}
    assert {r["strategy_name"] for r in beta.json()} == {"Beta"}


async def test_each_row_says_what_it_rests_on_and_what_it_cost(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    ids = await _seed(db_session_factory)

    rows = {(r["mode"], r["symbol"], r["status"]): r for r in (await _get(client, headers)).json()}

    live_filled = rows[("live", "INFY", "filled")]
    assert live_filled["price"] == 1500.0  # the broker-confirmed fill price, not the quoted one
    assert live_filled["data_source"] == "real"
    assert live_filled["order_id"] == str(ids["order"])
    assert live_filled["costs"] is None  # a live trade's costs are not recorded
    assert rows[("live", "INFY", "failed")]["price"] == 1490.0  # no fill price yet: the quote

    paper = rows[("paper", "RELIANCE", "filled")]
    assert paper["data_source"] == "synthetic" and paper["costs"] == 12.5
    assert paper["order_id"] is None and paper["administrative"] is False
    assert rows[("paper", "TCS", "partial")]["data_source"] == "real"
    assert rows[("paper", "reliance", "filled")]["administrative"] is True


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
async def test_a_page_size_or_offset_out_of_range_is_refused(
    client, make_user, db_session_factory, params
):
    headers = await _headers(client, make_user)

    assert (await _get(client, headers, **params)).status_code == 422


async def test_no_executions_is_an_empty_page_with_a_zero_total(client, make_user):
    headers = await _headers(client, make_user)

    resp = await _get(client, headers)

    assert resp.json() == [] and resp.headers["x-total-count"] == "0"


# ---- the live lists ----


async def test_a_strategys_live_orders_are_newest_first_and_counted(
    client, make_user, db_session_factory
):
    headers = await _headers(client, make_user)
    ids = await _seed(db_session_factory)
    async with db_session_factory() as db:
        intent_id = (await db.execute(select(LiveOrderIntent.id).limit(1))).scalar_one()
        for minutes_ago, broker_id in ((10, "OID-OLD"), (0, "OID-NEW")):
            db.add(
                Order(
                    live_order_intent_id=intent_id,
                    strategy_id=ids["beta"],
                    symbol="INFY",
                    side="buy",
                    quantity=1,
                    broker_name="zerodha",
                    broker_order_id=broker_id,
                    status="submitted",
                    submitted_at=NOW + timedelta(minutes=5 - minutes_ago),
                )
            )
        await db.commit()
    path = f"/api/v1/live-trading/strategies/{ids['beta']}/orders"

    page = await client.get(path, params={"limit": 2}, headers=headers)
    rest = await client.get(path, params={"limit": 2, "offset": 2}, headers=headers)

    assert page.headers["x-total-count"] == "3"
    assert [o["broker_order_id"] for o in page.json()] == ["OID-NEW", "OID-1"]
    assert [o["broker_order_id"] for o in rest.json()] == ["OID-OLD"]


async def test_the_intents_are_paged_and_counted_per_filter(client, make_user, db_session_factory):
    headers = await _headers(client, make_user)
    ids = await _seed(db_session_factory)
    async with db_session_factory() as db:
        for minutes_ago, state in ((3, "rejected"), (2, "submitted"), (1, "capped")):
            db.add(
                LiveOrderIntent(
                    strategy_id=ids["beta"],
                    symbol="TCS",
                    side="sell",
                    quantity=2,
                    intent_type="exit",
                    expires_at=NOW + timedelta(minutes=5),
                    generated_at=NOW - timedelta(minutes=minutes_ago),
                    status=state,
                )
            )
        await db.commit()
    path = "/api/v1/live-trading/intents"

    page = await client.get(path, params={"limit": 2}, headers=headers)
    capped = await client.get(path, params={"status_filter": "capped"}, headers=headers)

    # the three above and the one the ledger's own seeding made
    assert page.headers["x-total-count"] == "4" and len(page.json()) == 2
    assert capped.headers["x-total-count"] == "1" and capped.json()[0]["status"] == "capped"
