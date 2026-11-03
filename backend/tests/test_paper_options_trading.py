"""Option strategies entering, holding, stopping and exiting on real depth (phase 5).

Time is fixed (`NOW`: Monday 19 Oct 2026, 15:30 IST) so the expiry choice is deterministic: with
the default minimum of 3 days the contract is the 22nd; the 20th is too near to enter. Ticks are
stamped with that same clock (a tick's age is judged against it); a book's freshness is judged
against the real clock, as in production, so books are stamped with it.
"""

import time
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.brokers.base import OptionContract, option_symbol
from src.core.config import get_settings
from src.engine.paper_trading.depth import (
    DepthSnapshot,
    RealDepthOrderBookProvider,
    depth_key,
    publish_depth,
)
from src.engine.paper_trading.order_book import OrderBookLevel
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.audit_log import AuditLog
from src.models.daily_signal import DailySignal
from src.models.engine_alert_event import EngineAlertEvent
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.orchestration.paper_options import (
    evaluate_option_subscription,
    load_plan,
    option_quote_symbols,
    plan_key,
)
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)  # 15:30 IST
NEAR, WEEKLY = date(2026, 10, 20), date(2026, 10, 22)
STRIKES = [24400.0, 24450.0, 24500.0, 24550.0, 24600.0, 24650.0]
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


def _contract(expiry, strike, kind, lot=65):
    return OptionContract(
        option_symbol("NIFTY", expiry, strike, kind), "NIFTY", expiry, strike, kind, lot, 5.0
    )


CHAIN = [_contract(e, s, k) for e in (NEAR, WEEKLY) for s in STRIKES for k in ("CE", "PE")]


async def _contracts(underlying):
    assert underlying == "NIFTY"
    return CHAIN


def _sym(strike, kind, expiry=WEEKLY):
    return option_symbol("NIFTY", expiry, strike, kind)


CALL_24500, CALL_24600 = _sym(24500.0, "CE"), _sym(24600.0, "CE")
PUT_24500, PUT_24400 = _sym(24500.0, "PE"), _sym(24400.0, "PE")
ALL_LEGS = [CALL_24500, CALL_24600, PUT_24500, PUT_24400]


def _now_ms(offset_seconds: float = 0.0) -> int:
    return int((NOW.timestamp() + offset_seconds) * 1000)


@pytest_asyncio.fixture
async def redis(redis_client):
    keys = [depth_key(s) for s in ALL_LEGS] + [tick_stream_key(s) for s in ALL_LEGS]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    for name in ("PAPER_FRICTION_OVERRIDES", "PAPER_OPTIONS_MIN_DAYS_TO_EXPIRY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _subscription(
    db_session_factory, *, strategy="long_option", capital=500_000.0, stop=50.0, lots=1
):
    async with db_session_factory() as db:
        created = await create_strategy(db, name=f"OPT-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, created, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=capital,
            stop_loss_pct=stop,
            option_strategy=strategy,
            lots=lots,
        )


async def _signal(db_session_factory, subscription, kind):
    async with db_session_factory() as db:
        db.add(
            DailySignal(
                subscription_id=subscription.id,
                symbol="NIFTY",
                signal_type=kind,
                reference_price=24_510.0,
            )
        )
        await db.commit()


def _book(price, quantity=1_000):
    return DepthSnapshot(
        bids=(OrderBookLevel(price - 1.0, quantity), OrderBookLevel(price - 2.0, quantity)),
        asks=(OrderBookLevel(price + 1.0, quantity), OrderBookLevel(price + 2.0, quantity)),
        timestamp_ms=int(time.time() * 1000),
    )


async def _quote(redis, symbol, price, quantity=1_000, *, tick=True):
    """A leg's live book (best bid price - 1, best ask price + 1) and, by default, a fresh tick
    at `price` for marking it."""
    await publish_depth(redis, symbol, _book(price, quantity), ttl_seconds=60)
    if tick:
        await publish_tick(redis, Tick(symbol, price, _now_ms(), "real"))


async def _evaluate(
    db_session_factory, redis, subscription, *, spot=24_510.0, contracts=_contracts
):
    async with db_session_factory() as db:
        return await evaluate_option_subscription(
            db,
            redis,
            subscription.id,
            spot=spot,
            tick_source="real",
            contracts=contracts,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            regulatory_provider=_REGULATORY,
            now=NOW,
        )


async def _legs(db_session_factory, subscription) -> dict[str, PaperPosition]:
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
    return {row.symbol: row for row in rows}


async def _fills(db_session_factory, subscription) -> list[PaperFill]:
    async with db_session_factory() as db:
        return list(
            (
                await db.execute(
                    select(PaperFill)
                    .where(PaperFill.subscription_id == subscription.id)
                    .order_by(PaperFill.created_at, PaperFill.leg_index)
                )
            )
            .scalars()
            .all()
        )


async def _hold(db_session_factory, subscription, symbol, quantity, avg_cost):
    """A leg already held (as an earlier entry would have left it)."""
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol=symbol, quantity=quantity, avg_cost=avg_cost
            )
        )
        await db.commit()


# ---- choosing the legs ----


async def test_a_buy_signal_with_a_flat_book_chooses_the_at_the_money_call_and_asks_for_its_quote(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "planned" and result.fills == ()
    plan = await load_plan(redis, subscription.id)
    assert plan.direction == "bullish" and plan.expiry == "2026-10-22"  # the 20th is too near
    assert [(leg.symbol, leg.side, leg.quantity) for leg in plan.legs] == [(CALL_24500, "buy", 65)]
    async with db_session_factory() as db:
        assert await option_quote_symbols(db, redis) == {CALL_24500}  # now it is quoted


async def test_a_plan_lapses_on_its_own_so_a_stale_choice_is_never_traded(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)

    assert 0 < await redis.pttl(plan_key(subscription.id)) <= 300_000  # the configured 5 minutes


async def test_the_plan_lifetime_is_the_setting(db_session_factory, redis, monkeypatch):
    monkeypatch.setenv("PAPER_OPTIONS_PLAN_TTL_SECONDS", "20")
    get_settings.cache_clear()
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")

    await _evaluate(db_session_factory, redis, subscription)

    assert 0 < await redis.pttl(plan_key(subscription.id)) <= 20_000


async def test_with_no_expiry_far_enough_out_nothing_is_planned(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    near_only = [c for c in CHAIN if c.expiry == NEAR]

    async def contracts(underlying):
        return near_only

    result = await _evaluate(db_session_factory, redis, subscription, contracts=contracts)

    assert result.action == "none" and "no suitable contract" in result.detail
    assert await load_plan(redis, subscription.id) is None


async def test_the_minimum_days_setting_decides_which_expiry_is_chosen(
    db_session_factory, redis, monkeypatch
):
    monkeypatch.setenv("PAPER_OPTIONS_MIN_DAYS_TO_EXPIRY", "1")
    get_settings.cache_clear()
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")

    await _evaluate(db_session_factory, redis, subscription)

    assert (await load_plan(redis, subscription.id)).expiry == "2026-10-20"


async def test_without_a_contract_source_nothing_is_planned(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")

    result = await _evaluate(db_session_factory, redis, subscription, contracts=None)

    assert result.action == "none" and result.detail == "no contract source"


async def test_with_no_signal_a_flat_subscription_does_nothing(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"


async def test_a_plan_for_the_other_direction_is_replaced_when_the_signal_turns(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _signal(db_session_factory, subscription, "SELL")

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "planned"
    plan = await load_plan(redis, subscription.id)
    assert plan.direction == "bearish" and [leg.symbol for leg in plan.legs] == [PUT_24500]


async def test_an_inactive_or_non_option_subscription_is_left_alone(db_session_factory, redis):
    from src.models.paper_trading_subscription import PaperTradingSubscription

    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    async with db_session_factory() as db:
        (await db.get(PaperTradingSubscription, subscription.id)).is_active = False
        await db.commit()

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"
    assert await load_plan(redis, subscription.id) is None

    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="EQ-OPT", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        equity = await enroll_in_paper_trading(db, strategy_version_id=version.id, symbol="NIFTY")
    assert (await _evaluate(db_session_factory, redis, equity)).action == "none"


# ---- entering a long option ----


async def test_the_planned_call_is_bought_once_its_book_arrives(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)  # plan
    assert (await _evaluate(db_session_factory, redis, subscription)).action == "waiting"
    assert await _legs(db_session_factory, subscription) == {}  # no book, nothing bought

    await _quote(redis, CALL_24500, 100.0)
    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "entered"
    [fill] = await _fills(db_session_factory, subscription)
    assert (fill.symbol, fill.side, fill.filled_quantity) == (CALL_24500, "buy", 65)
    assert fill.avg_fill_price == 101.0  # the best ask
    assert fill.data_source == "real" and fill.leg_index == 0
    legs = await _legs(db_session_factory, subscription)
    assert (legs[CALL_24500].quantity, legs[CALL_24500].avg_cost) == (65, 101.0)
    assert await load_plan(redis, subscription.id) is None  # used up


async def test_an_entry_is_charged_the_option_schedule_on_the_premium(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, CALL_24500, 100.0)

    await _evaluate(db_session_factory, redis, subscription)

    [fill] = await _fills(db_session_factory, subscription)
    premium = 65 * 101.0
    costs = fill.costs_breakdown
    assert (costs["segment"], costs["product"]) == ("option", "nrml")
    assert costs["turnover"] == pytest.approx(premium)  # the premium, not the strike value
    assert costs["brokerage"] == 20.0 and costs["stt"] == 0.0  # flat fee, no STT on a buy
    assert costs["exchange_txn_charges"] == pytest.approx(
        premium * (0.0003553 + 0.000000001)
    )  # + IPFT
    assert costs["stamp_duty"] == pytest.approx(premium * 0.00003)
    legs = await _legs(db_session_factory, subscription)
    assert legs[CALL_24500].costs_total == pytest.approx(fill.costs_total)


async def test_the_entry_consumes_the_signal_and_a_second_pass_does_nothing(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, CALL_24500, 100.0)
    await _evaluate(db_session_factory, redis, subscription)

    again = await _evaluate(db_session_factory, redis, subscription)

    assert again.action == "none"  # holding it, signal still BUY, mark healthy
    assert len(await _fills(db_session_factory, subscription)) == 1
    async with db_session_factory() as db:
        assert (await db.execute(select(DailySignal))).scalar_one().consumed is True


async def test_a_premium_beyond_the_allowed_share_of_capital_is_refused(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, capital=100_000.0)  # limit Rs 50,000
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, CALL_24500, 1_000.0)  # 65 x 1,001 = Rs 65,065

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "refused" and "largest possible loss" in result.detail
    assert await _fills(db_session_factory, subscription) == []


async def test_a_bearish_signal_buys_the_put(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, PUT_24500, 90.0)

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "entered"
    assert (await _fills(db_session_factory, subscription))[0].symbol == PUT_24500


async def test_lots_set_the_quantity(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, lots=3)
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, CALL_24500, 100.0)

    await _evaluate(db_session_factory, redis, subscription)

    assert (await _fills(db_session_factory, subscription))[0].filled_quantity == 195


# ---- entering a vertical (credit) spread ----


async def _enter_spread(
    db_session_factory, redis, *, hedge_depth=1_000, capital=500_000.0, kind="BUY"
):
    """A bull put spread (BUY: sell the at-the-money put, buy the one two strikes down) or a bear
    call spread (SELL: sell the at-the-money call, buy the one two strikes up). The hedge quotes
    at 20 (ask 21), the sold leg at 60 (bid 59): a credit of 38 a unit."""
    subscription = await _subscription(
        db_session_factory, strategy="vertical_spread", capital=capital
    )
    await _signal(db_session_factory, subscription, kind)
    await _evaluate(db_session_factory, redis, subscription)
    hedge, sold = (PUT_24400, PUT_24500) if kind == "BUY" else (CALL_24600, CALL_24500)
    await _quote(redis, hedge, 20.0, hedge_depth)
    await _quote(redis, sold, 60.0)
    result = await _evaluate(db_session_factory, redis, subscription)
    return subscription, result, hedge, sold


async def test_a_bull_put_spread_buys_the_lower_put_as_its_hedge_then_sells_the_at_the_money_put(
    db_session_factory, redis
):
    subscription, result, hedge, sold = await _enter_spread(db_session_factory, redis)

    assert result.action == "entered"
    fills = await _fills(db_session_factory, subscription)
    assert [(f.symbol, f.side, f.leg_index) for f in fills] == [
        (hedge, "buy", 0),
        (sold, "sell", 1),
    ]  # the hedge first
    assert (hedge, sold) == (PUT_24400, PUT_24500)
    assert len({f.order_group_id for f in fills}) == 1  # one order
    assert fills[0].avg_fill_price == 21.0 and fills[1].avg_fill_price == 59.0  # ask, bid
    legs = await _legs(db_session_factory, subscription)
    assert (legs[hedge].quantity, legs[sold].quantity) == (65, -65)


async def test_a_bear_call_spread_works_the_other_way_up(db_session_factory, redis):
    subscription, result, hedge, sold = await _enter_spread(db_session_factory, redis, kind="SELL")

    assert result.action == "entered"
    assert (hedge, sold) == (CALL_24600, CALL_24500)
    legs = await _legs(db_session_factory, subscription)
    assert (legs[hedge].quantity, legs[sold].quantity) == (65, -65)


async def test_the_sold_leg_is_charged_stt_and_the_bought_one_is_not(db_session_factory, redis):
    subscription, _, _, _ = await _enter_spread(db_session_factory, redis)

    hedge, sold = await _fills(db_session_factory, subscription)

    assert hedge.costs_breakdown["stt"] == 0.0 and hedge.costs_breakdown["stamp_duty"] > 0
    assert sold.costs_breakdown["stt"] == pytest.approx(65 * 59.0 * 0.0015)
    assert sold.costs_breakdown["stamp_duty"] == 0.0


async def test_a_hedge_that_fills_only_partly_limits_what_is_sold_against_it(
    db_session_factory, redis
):
    subscription, result, hedge, sold = await _enter_spread(
        db_session_factory, redis, hedge_depth=20
    )  # two levels of 20: only 40 of the 65 can be bought

    assert result.action == "entered"
    legs = await _legs(db_session_factory, subscription)
    bought = legs[hedge].quantity
    assert 0 < bought < 65
    assert legs[sold].quantity == -bought  # never more sold than bought: never naked
    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "paper_trading.option_entry_unbalanced")
            )
        ).scalar_one()
    assert entry.details["strategy"] == "vertical_spread"


async def test_without_a_book_for_the_hedge_nothing_is_sold(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "BUY")
    await _evaluate(db_session_factory, redis, subscription)
    await _quote(redis, PUT_24500, 60.0)  # the sold leg has a book; its hedge has none

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "waiting"
    assert await _fills(db_session_factory, subscription) == []
    assert await _legs(db_session_factory, subscription) == {}


async def test_a_spread_is_judged_by_its_largest_possible_loss_not_its_cash_flow(
    db_session_factory, redis
):
    """It takes in a credit (65 x 38 = Rs 2,470), but could lose its width (65 x 100 = Rs 6,500)
    less that: Rs 4,030. A 50% limit on Rs 10,000 (Rs 5,000) takes it; on Rs 7,000 (Rs 3,500) it
    does not, though no cash is paid out."""
    _, taken, _, _ = await _enter_spread(db_session_factory, redis, capital=10_000.0)
    assert taken.action == "entered"

    await redis.delete(*[depth_key(s) for s in ALL_LEGS], *[tick_stream_key(s) for s in ALL_LEGS])
    _, refused, _, _ = await _enter_spread(db_session_factory, redis, capital=7_000.0)

    assert refused.action == "refused" and "4,030" in refused.detail


# ---- holding: marks, stop, signal flip ----


async def test_a_structure_that_has_lost_half_its_premium_is_closed(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 49.0)  # down 51%

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "exited" and "stop-loss" in result.detail
    [fill] = await _fills(db_session_factory, subscription)
    assert (fill.side, fill.filled_quantity, fill.avg_fill_price) == ("sell", 65, 48.0)
    assert fill.costs_breakdown["stt"] == pytest.approx(65 * 48.0 * 0.0015)  # a sell: STT
    assert (await _legs(db_session_factory, subscription))[CALL_24500].quantity == 0


async def test_a_stop_loss_exit_is_recorded_as_an_engine_alert(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 49.0)

    await _evaluate(db_session_factory, redis, subscription)

    async with db_session_factory() as db:
        [event] = (await db.execute(select(EngineAlertEvent))).scalars().all()
    assert (event.source, event.kind, event.severity) == ("event", "stop_loss", "warning")
    assert event.title == "NIFTY: option stop-loss hit, structure closed"
    assert event.detail == "Closed 1 of 1 legs: stop-loss (50% of the premium)."
    assert event.subscription_id == subscription.id
    assert event.resolved_at == event.opened_at  # over the moment it happened
    assert event.key.startswith("stop_loss:")


async def test_an_exit_for_another_reason_is_not_a_stop_loss_alert(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 100.0)
    await _signal(db_session_factory, subscription, "SELL")  # the signal turns: not a stop

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "exited"

    async with db_session_factory() as db:
        assert (await db.execute(select(EngineAlertEvent))).scalars().all() == []


async def test_a_smaller_loss_is_held(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 60.0)  # down 40%

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"
    assert await _fills(db_session_factory, subscription) == []


async def test_the_stop_is_the_subscriptions_own_percentage(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, stop=20.0)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 79.0)  # down 21%

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "exited"


async def test_a_credit_spread_is_stopped_on_the_value_of_both_legs_together(
    db_session_factory, redis
):
    """A bear call spread opened for a credit of 40 a unit (sold 60, bought 20) over a width of
    100: it can lose 60 a unit at most (Rs 3,900). A 50% stop is a loss of Rs 1,950."""
    subscription = await _subscription(db_session_factory, strategy="vertical_spread", stop=50.0)
    await _signal(db_session_factory, subscription, "SELL")  # bearish, like what is held
    await _hold(db_session_factory, subscription, CALL_24600, 65, 20.0)  # the hedge
    await _hold(db_session_factory, subscription, CALL_24500, -65, 60.0)  # the sold call
    await _quote(redis, CALL_24600, 25.0)
    await _quote(redis, CALL_24500, 90.0)  # a loss of 65 x (30 - 5) = Rs 1,625: held

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"

    await _quote(redis, CALL_24500, 100.0)  # a loss of 65 x (40 - 5) = Rs 2,275: stopped
    assert (await _evaluate(db_session_factory, redis, subscription)).action == "exited"


async def test_with_a_stale_or_missing_mark_there_is_no_stop(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    # a book but no tick at all: the leg cannot be marked
    await publish_depth(redis, CALL_24500, _book(10.0), ttl_seconds=60)

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"

    await publish_tick(redis, Tick(CALL_24500, 10.0, _now_ms(-600), "real"))  # ten minutes old
    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"


async def test_a_mark_just_inside_the_age_limit_is_used(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await publish_depth(redis, CALL_24500, _book(10.0), ttl_seconds=60)
    await publish_tick(redis, Tick(CALL_24500, 10.0, _now_ms(-55), "real"))

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "exited"


async def test_a_signal_that_turns_closes_the_structure_and_the_next_pass_enters_the_other(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 100.0)
    await _signal(db_session_factory, subscription, "SELL")

    first = await _evaluate(db_session_factory, redis, subscription)

    assert first.action == "exited" and "other way" in first.detail
    assert (await _legs(db_session_factory, subscription))[CALL_24500].quantity == 0
    second = await _evaluate(db_session_factory, redis, subscription)  # flat, SELL standing
    assert second.action == "planned"
    assert (await load_plan(redis, subscription.id)).legs[0].symbol == PUT_24500


async def test_a_structure_matching_the_signal_is_held(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await _hold(db_session_factory, subscription, CALL_24500, 65, 100.0)
    await _quote(redis, CALL_24500, 100.0)

    assert (await _evaluate(db_session_factory, redis, subscription)).action == "none"


# ---- closing: the hedge goes last ----


async def _hold_bear_call_spread(db_session_factory, subscription):
    await _hold(db_session_factory, subscription, CALL_24600, 65, 20.0)  # the hedge
    await _hold(db_session_factory, subscription, CALL_24500, -65, 60.0)  # the sold call


async def test_a_spread_closes_by_buying_back_the_sold_leg_before_selling_the_hedge(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "BUY")  # the opposite of the held bear spread
    await _hold_bear_call_spread(db_session_factory, subscription)
    await _quote(redis, CALL_24600, 20.0)
    await _quote(redis, CALL_24500, 60.0)

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "exited"
    fills = await _fills(db_session_factory, subscription)
    assert [(f.symbol, f.side) for f in fills] == [(CALL_24500, "buy"), (CALL_24600, "sell")]
    legs = await _legs(db_session_factory, subscription)
    assert legs[CALL_24500].quantity == 0 and legs[CALL_24600].quantity == 0


async def test_if_the_sold_leg_cannot_be_closed_its_hedge_is_not_sold_from_under_it(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "BUY")
    await _hold_bear_call_spread(db_session_factory, subscription)
    await _quote(redis, CALL_24600, 20.0)
    await publish_tick(redis, Tick(CALL_24500, 60.0, _now_ms(), "real"))  # the sold leg: no book

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "exit_blocked" and CALL_24500 in result.detail
    assert await _fills(db_session_factory, subscription) == []
    legs = await _legs(db_session_factory, subscription)
    assert (legs[CALL_24600].quantity, legs[CALL_24500].quantity) == (65, -65)


async def test_a_sold_leg_left_without_its_hedge_is_closed_at_once(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "SELL")
    await _hold(db_session_factory, subscription, CALL_24500, -65, 60.0)  # the hedge is gone
    await _quote(redis, CALL_24500, 60.0)

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "exited" and "without its hedge" in result.detail
    assert (await _legs(db_session_factory, subscription))[CALL_24500].quantity == 0


# ---- what the tick job quotes ----


async def test_held_legs_and_planned_legs_are_what_the_tick_job_must_quote(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _hold_bear_call_spread(db_session_factory, subscription)
    await _hold(db_session_factory, subscription, PUT_24500, 0, 0.0)  # a flat leg is not quoted
    await redis.set(
        plan_key(subscription.id),
        '{"direction": "bullish", "strategy": "vertical_spread", "expiry": "2026-10-22", '
        f'"legs": [{{"symbol": "{PUT_24400}", "side": "buy", "quantity": 65, "strike": 24400.0, '
        '"option_type": "PE"}]}',
        px=60_000,
    )

    async with db_session_factory() as db:
        symbols = await option_quote_symbols(db, redis)

    assert symbols == {CALL_24500, CALL_24600, PUT_24400}
    await redis.delete(plan_key(subscription.id))


async def test_with_no_option_subscriptions_there_is_nothing_extra_to_quote(
    db_session_factory, redis
):
    async with db_session_factory() as db:
        assert await option_quote_symbols(db, redis) == set()


async def test_a_sold_leg_only_partly_bought_back_keeps_its_hedge_open(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "BUY")  # the opposite of the held bear spread
    await _hold_bear_call_spread(db_session_factory, subscription)
    await publish_depth(
        redis,
        CALL_24500,
        DepthSnapshot(
            bids=(), asks=(OrderBookLevel(61.0, 40),), timestamp_ms=int(time.time() * 1000)
        ),
        ttl_seconds=60,
    )  # only 40 of the 65 can be bought back
    await publish_tick(redis, Tick(CALL_24500, 60.0, _now_ms(), "real"))
    await _quote(redis, CALL_24600, 20.0)

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "exited"
    fills = await _fills(db_session_factory, subscription)
    assert [(f.symbol, f.side, f.filled_quantity) for f in fills] == [(CALL_24500, "buy", 40)]
    legs = await _legs(db_session_factory, subscription)
    assert legs[CALL_24500].quantity == -25  # still short 25 ...
    assert legs[CALL_24600].quantity == 65  # ... and its hedge is untouched


async def test_a_structure_with_one_leg_unpriced_is_not_stopped_on_a_guess(
    db_session_factory, redis
):
    """The sold call alone has jumped to 100 (a heavy loss if the hedge stayed at 20), but the
    hedge has no fresh mark: the structure cannot be valued, so nothing is stopped on it."""
    subscription = await _subscription(db_session_factory, strategy="vertical_spread", stop=50.0)
    await _signal(db_session_factory, subscription, "SELL")  # bearish, like what is held
    await _hold_bear_call_spread(db_session_factory, subscription)
    await _quote(redis, CALL_24500, 100.0)  # the sold leg is marked ...
    await publish_depth(redis, CALL_24600, _book(20.0), ttl_seconds=60)  # ... the hedge is not

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "none"
    assert await _fills(db_session_factory, subscription) == []


async def test_the_naked_options_scan_runs_on_an_entry_so_a_wrongly_hedged_sold_leg_is_refused(
    db_session_factory, redis
):
    """The strategies never plan this, but what is traded is what is in the stored plan: a sold
    put whose 'hedge' is a put at a HIGHER strike (the wrong side: not further out of the money)
    is still naked to the scan, and the gate must stop it. Both legs have books and the premium
    fits, so only the scan stands in the way."""
    wrong_hedge = _sym(24600.0, "PE")
    subscription = await _subscription(db_session_factory, strategy="vertical_spread")
    await _signal(db_session_factory, subscription, "BUY")
    await redis.set(
        plan_key(subscription.id),
        '{"direction": "bullish", "strategy": "vertical_spread", "expiry": "2026-10-22", '
        f'"legs": [{{"symbol": "{wrong_hedge}", "side": "buy", "quantity": 65, '
        '"strike": 24600.0, "option_type": "PE"}, '
        f'{{"symbol": "{PUT_24500}", "side": "sell", "quantity": 65, "strike": 24500.0, '
        '"option_type": "PE"}]}',
        px=60_000,
    )
    await _quote(redis, wrong_hedge, 150.0)
    await _quote(redis, PUT_24500, 60.0)

    result = await _evaluate(db_session_factory, redis, subscription)

    assert result.action == "refused" and "naked" in result.detail
    assert await _fills(db_session_factory, subscription) == []
    assert await _legs(db_session_factory, subscription) == {}
    await redis.delete(
        plan_key(subscription.id), depth_key(wrong_hedge), tick_stream_key(wrong_hedge)
    )
