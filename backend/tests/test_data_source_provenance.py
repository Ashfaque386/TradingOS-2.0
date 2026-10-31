"""Data provenance: whether a backtest, a paper fill, a live trade or a post-trade review
rested on real market data or on data this system generated.

Nothing recorded that before. Paper trading falls back to a random-walk tick feed when no
broker is configured and walks every fill through a synthetic order book; a backtest takes
the bars its caller posts; and the post-trade review's commentary is folded into an LLM prompt,
where "12 fills, win rate 58%" computed on a random walk reads exactly like a measured result.
"""

import time
import uuid
from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest
from sqlalchemy import select

from src.brokers.base import BrokerCredentials
from src.brokers.tick_source import BrokerQuoteTickSource
from src.brokers.zerodha import ZerodhaKiteAdapter
from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.paper_trading.tick_feed import (
    MockTickSource,
    Tick,
    get_latest_tick,
    publish_tick,
    publish_ticks_once,
    read_new_ticks,
    tick_stream_key,
)
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.data_source import CHECK_SQL, DataSource, combine, provenance_note
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.trade import Trade
from src.orchestration.paper_trading import (
    enroll_in_paper_trading,
    process_tick,
    run_daily_signal_generation,
)
from src.orchestration.post_trade_review import run_post_trade_review
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
_PRICES = FakeDailyPriceProvider(seed_by_symbol={"DEMOSTOCK": 1})
_BUY_AS_OF = pd.Timestamp("2026-09-09")
R, S, U, M, X = (
    DataSource.REAL,
    DataSource.SYNTHETIC,
    DataSource.SUPPLIED,
    DataSource.MIXED,
    DataSource.UNKNOWN,
)


# ---- the vocabulary ------------------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([R], R),
        ([R, R], R),
        ([S, S], S),
        ([U], U),
        ([R, S], M),
        ([S, U], M),
        ([R, S, U], M),
        ([M, R], M),
        ([R, X], X),  # one input nobody can vouch for taints the whole
        ([X, X], X),
        ([M, X], X),
        ([], X),
    ],
)
def test_combining_sources(sources, expected):
    assert combine(sources) == expected
    assert combine(iter(sources)) == expected  # any iterable


def test_the_check_constraint_lists_exactly_the_known_sources():
    for source in DataSource.ALL:
        assert f"'{source}'" in CHECK_SQL
    assert CHECK_SQL.count("'") == 2 * len(DataSource.ALL)


def test_only_real_data_needs_no_caveat():
    assert provenance_note(R) == ""
    for source in (S, U, M, X):
        note = provenance_note(source)
        assert note.startswith(" Data: ") and note.endswith(".")
    assert "synthetic" in provenance_note(S) and "not taken from the market" in provenance_note(S)
    assert "supplied" in provenance_note(U) and "cannot vouch" in provenance_note(U)
    assert "partly synthetic" in provenance_note(M)
    assert "not recorded" in provenance_note(X)
    assert provenance_note("something-new") == provenance_note(X)  # unrecognised = unknown


# ---- where a price comes from ---------------------------------------------------


async def test_the_mock_tick_walk_is_labelled_synthetic():
    assert (await MockTickSource().next_tick("PROV1")).source == S


async def test_a_broker_quote_is_labelled_real():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"NSE:INFY": {"last_price": 1500.0}}})

    adapter = ZerodhaKiteAdapter(
        BrokerCredentials(api_key="k", access_token="t"), transport=httpx.MockTransport(handler)
    )

    assert (await BrokerQuoteTickSource(adapter).next_tick("INFY")).source == R


async def test_the_source_survives_the_redis_stream(redis_client):
    symbol = "PROVSTREAM"
    await redis_client.delete(tick_stream_key(symbol))
    await publish_tick(redis_client, Tick(symbol, 10.0, 1, source=R))
    await publish_tick(redis_client, Tick(symbol, 11.0, 2))  # no source given

    ticks, _ = await read_new_ticks(redis_client, symbol)
    assert [t.source for t in ticks] == [R, X]

    # The latest tick reads its source too (a labelled one: "unknown" would hide a miss).
    await publish_tick(redis_client, Tick(symbol, 12.0, 3, source=S))
    latest = await get_latest_tick(redis_client, symbol)
    assert latest is not None and latest.source == S
    await redis_client.delete(tick_stream_key(symbol))


async def test_a_stream_entry_written_before_sources_existed_reads_as_unknown(redis_client):
    symbol = "PROVLEGACY"
    await redis_client.delete(tick_stream_key(symbol))
    await redis_client.xadd(tick_stream_key(symbol), {"price": "5.0", "timestamp_ms": "7"})

    ticks, _ = await read_new_ticks(redis_client, symbol)
    latest = await get_latest_tick(redis_client, symbol)

    assert [t.source for t in ticks] == [X] and latest is not None and latest.source == X
    await redis_client.delete(tick_stream_key(symbol))


async def test_publishing_carries_the_sources_label_through(redis_client):
    await redis_client.delete(tick_stream_key("PROVPUB"))

    await publish_ticks_once(redis_client, MockTickSource(), ["PROVPUB"])

    (tick,), _ = await read_new_ticks(redis_client, "PROVPUB")
    assert tick.source == S
    await redis_client.delete(tick_stream_key("PROVPUB"))


# ---- paper fills ----------------------------------------------------------------


async def _subscription(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="ProvDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="DEMOSTOCK",
            builtin_strategy="sma_crossover",
            sma_window=15,
            initial_capital=100_000.0,
        )


async def _tick_fill(db_session_factory, tick_source: str | None, book=MockOrderBookProvider()):
    subscription = await _subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(db, price_provider=_PRICES, as_of=_BUY_AS_OF)
    kwargs = {} if tick_source is None else {"tick_source": tick_source}
    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=signals[0].reference_price,
            order_book_provider=book,
            regulatory_provider=ReferenceTableRegulatoryDataProvider(),
            **kwargs,
        )
    assert fill is not None
    return fill


@pytest.mark.parametrize(
    ("tick_source", "recorded"),
    [
        (S, S),  # a random-walk tick through a synthetic book: synthetic
        (R, M),  # a real tick through a synthetic book is NOT real: mixed
        (U, M),  # an operator's price through a synthetic book
        (X, X),  # an unlabelled tick: nobody can vouch
        (None, X),  # the caller said nothing at all
    ],
)
async def test_a_paper_fill_records_what_it_rested_on(db_session_factory, tick_source, recorded):
    fill = await _tick_fill(db_session_factory, tick_source)

    assert fill.data_source == recorded
    async with db_session_factory() as db:  # and it is what was persisted, not just returned
        stored = (await db.execute(select(PaperFill.data_source))).scalars().all()
    assert stored == [recorded]


async def test_an_order_book_that_does_not_say_where_its_depth_came_from_makes_the_fill_unknown(
    db_session_factory,
):
    class _AnonymousBook:
        def snapshot(self, symbol, *, mid_price):
            return MockOrderBookProvider().snapshot(symbol, mid_price=mid_price)

    fill = await _tick_fill(db_session_factory, R, book=_AnonymousBook())

    assert fill.data_source == X


def test_the_mock_order_book_declares_itself_synthetic():
    assert MockOrderBookProvider.data_source == S


async def test_the_drain_job_labels_fills_with_their_ticks_source(
    db_session_factory, redis_client, monkeypatch
):
    from src.orchestration import paper_trading_scheduler as scheduler

    await _subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(db, price_provider=_PRICES, as_of=_BUY_AS_OF)
    await redis_client.delete(tick_stream_key("DEMOSTOCK"))
    # A price near the signal's own reference, or the risk gate rejects the order.
    now_ms = int(time.time() * 1000)  # a stale tick would trip the latency guard
    await publish_tick(
        redis_client, Tick("DEMOSTOCK", signals[0].reference_price, now_ms, source=S)
    )
    monkeypatch.setattr(scheduler, "is_market_open_ist", lambda: True)

    await scheduler.run_tick_drain_job(
        db_session_factory,
        redis_client,
        MockOrderBookProvider(),
        ReferenceTableRegulatoryDataProvider(),
    )

    async with db_session_factory() as db:
        fills = (await db.execute(select(PaperFill.data_source))).scalars().all()
    assert fills and set(fills) == {S}  # synthetic tick + synthetic book
    await redis_client.delete(tick_stream_key("DEMOSTOCK"))


async def test_the_tick_endpoint_labels_an_operators_price_as_supplied(
    client, make_user, db_session_factory, monkeypatch
):
    from src.core.roles import Role

    await make_user("prov-admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "prov-admin@example.com", "password": "supersecret1"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    subscription = await _subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(db, price_provider=_PRICES, as_of=_BUY_AS_OF)
    # Through the real (synthetic) book, "supplied" and "real" would both come out "mixed" and
    # the label could not be told apart; a book that declares itself supplied can.
    from src.api.routes import paper_trading as paper_route

    class _SuppliedBook:
        data_source = U

        def snapshot(self, symbol, *, mid_price):
            return MockOrderBookProvider().snapshot(symbol, mid_price=mid_price)

    monkeypatch.setattr(paper_route, "_ORDER_BOOK_PROVIDER", _SuppliedBook())

    resp = await client.post(
        f"/api/v1/paper-trading/subscriptions/{subscription.id}/tick",
        json={"tick_price": signals[0].reference_price},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["data_source"] == U  # an operator's price, labelled as such


# ---- backtests ------------------------------------------------------------------


async def test_a_backtest_posted_by_a_caller_is_labelled_supplied(client, make_user):
    from src.core.roles import Role

    await make_user("prov-bt@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "prov-bt@example.com", "password": "supersecret1"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    strategy = (
        await client.post(
            "/api/v1/strategies", json={"name": "ProvBt", "objective": "o"}, headers=headers
        )
    ).json()
    versions = await client.get(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    version_id = versions.json()["versions"][0]["id"] if "versions" in versions.json() else None
    if version_id is None:
        pytest.skip("strategy detail does not expose versions in this build")
    idx = pd.bdate_range("2026-01-01", periods=60)
    bars = [
        {
            "date": d.date().isoformat(),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100 + i,
            "volume": 1000,
        }
        for i, d in enumerate(idx)
    ]

    resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_version_id": version_id,
            "symbol": "PROVBT",
            "bars": bars,
            "as_of": idx[-1].date().isoformat(),
        },
        headers=headers,
    )

    assert resp.status_code == 201
    assert resp.json()["data_source"] == U
    listed = await client.get("/api/v1/backtests", headers=headers)
    assert [r["data_source"] for r in listed.json()] == [U]


# ---- live trades ----------------------------------------------------------------


async def test_a_live_trade_is_labelled_real(db_session_factory):
    from src.orchestration.live_trading import (
        generate_live_order_intent,
        run_live_daily_signal_generation,
    )
    from tests.test_orchestration_live_trading import (
        _BUY_AS_OF as LIVE_AS_OF,
    )
    from tests.test_orchestration_live_trading import (
        _PRICE_PROVIDER as LIVE_PRICES,
    )
    from tests.test_orchestration_live_trading import (
        _REGULATORY_PROVIDER as LIVE_REG,
    )
    from tests.test_orchestration_live_trading import (
        _enroll_and_enable_autonomy,
        _make_live_eligible_strategy,
        _mock_adapter,
        _quote_and_order_handler,
    )

    strategy_id = await _make_live_eligible_strategy(db_session_factory)
    subscription = await _enroll_and_enable_autonomy(db_session_factory, strategy_id)
    async with db_session_factory() as db:
        await run_live_daily_signal_generation(db, price_provider=LIVE_PRICES, as_of=LIVE_AS_OF)

    async with db_session_factory() as db:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription.id,
            tick_price=106.0,
            adapter=_mock_adapter(_quote_and_order_handler(106.5)),
            regulatory_provider=LIVE_REG,
        )

    assert intent is not None and intent.status == "submitted"
    async with db_session_factory() as db:
        trades = (await db.execute(select(Trade))).scalars().all()
    assert [t.data_source for t in trades] == [R]


# ---- the post-trade review ------------------------------------------------------


async def _paper_strategy_with_fills(db_session_factory, sources: list[str]):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"Rev{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        for i, source in enumerate(sources):
            db.add(
                PaperFill(
                    subscription_id=subscription.id,
                    symbol="RELIANCE",
                    side="sell",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=10,
                    filled_quantity=10,
                    avg_fill_price=2500.0,
                    fully_filled=True,
                    realized_pnl=100.0 if i % 2 == 0 else -50.0,
                    data_source=source,
                )
            )
        await db.commit()
        return strategy.id


async def _paper_finding(db_session_factory, strategy_id):
    async with db_session_factory() as db:
        findings = await run_post_trade_review(db, as_of=datetime.now(IST).date())
    return next(f for f in findings if f.strategy_id == strategy_id and f.mode == "paper")


@pytest.mark.parametrize(
    ("fill_sources", "recorded", "caveat"),
    [
        ([S, S], S, "synthetic -- generated by this system"),
        ([M, M], M, "partly synthetic"),
        ([R, S], M, "partly synthetic"),
        ([U], U, "supplied by an operator"),
        ([X, R], X, "provenance was not recorded"),
        ([R, R], R, None),
    ],
)
async def test_the_review_says_what_its_figures_rest_on(
    db_session_factory, fill_sources, recorded, caveat
):
    strategy_id = await _paper_strategy_with_fills(db_session_factory, fill_sources)

    finding = await _paper_finding(db_session_factory, strategy_id)

    assert finding.data_source == recorded
    if caveat is None:
        assert "Data:" not in finding.commentary  # real data needs no caveat
    else:
        assert f"Data: {caveat}" in finding.commentary
    # The figures themselves are still there, caveat or not.
    assert f"{len(fill_sources)} paper fill(s) today" in finding.commentary


async def test_a_reviews_caveat_reaches_the_llm_prompt_that_folds_it_in(db_session_factory):
    """The finding's commentary is what `review_suggestion` puts in front of a model."""
    from src.orchestration.post_trade_review import latest_finding_for_strategy

    strategy_id = await _paper_strategy_with_fills(db_session_factory, [S, S])
    await _paper_finding(db_session_factory, strategy_id)

    async with db_session_factory() as db:
        latest = await latest_finding_for_strategy(db, strategy_id)

    assert latest is not None and "Data: synthetic" in latest.commentary


async def test_a_live_review_is_labelled_by_its_trades(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="LiveRevProv", objective="o")
        db.add(
            LivePosition(
                strategy_id=strategy.id,
                symbol="TCS",
                quantity=10,
                avg_cost=3400.0,
                realized_pnl=5.0,
            )
        )
        intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol="TCS",
            side="buy",
            quantity=10,
            intent_type="entry",
            expires_at=datetime.now(UTC),
            status="submitted",
        )
        db.add(intent)
        await db.flush()
        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=strategy.id,
            symbol="TCS",
            side="buy",
            quantity=10,
            broker_name="zerodha",
            broker_order_id="X1",
            status="submitted",
        )
        db.add(order)
        await db.flush()
        db.add(
            Trade(
                order_id=order.id,
                symbol="TCS",
                side="buy",
                quantity=10,
                price=3400.0,
                status="filled",
                data_source=R,
            )
        )
        await db.commit()
        strategy_id = strategy.id

    async with db_session_factory() as db:
        findings = await run_post_trade_review(db, as_of=datetime.now(IST).date())

    live = next(f for f in findings if f.strategy_id == strategy_id and f.mode == "live")
    assert live.data_source == R and "Data:" not in live.commentary


async def test_existing_rows_without_a_source_read_as_unknown(db_session_factory):
    """Rows written before provenance existed carry the column's default."""
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="Legacy", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="RELIANCE",
                side="buy",
                order_group_id=uuid.uuid4(),
                requested_quantity=1,
                filled_quantity=1,
                avg_fill_price=1.0,
                fully_filled=True,
                realized_pnl=0.0,
            )
        )
        await db.commit()

    async with db_session_factory() as db:
        assert (await db.execute(select(PaperFill.data_source))).scalars().all() == [X]
