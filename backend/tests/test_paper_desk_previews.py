"""The desk's read-only previews and its one new write (phase 6 follow-up): searching listed
symbols, what enrolling would do, what closing would do, and changing a subscription's settings.

A preview must say what the real action would do, so each test pins an answer to the same rule the
engine uses (the sizing function, the margin check, the depth walk) and worked out by hand.
"""

import time
import uuid
from datetime import date, timedelta

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select

from src.brokers.base import (
    BrokerCredentials,
    FuturesContract,
    OptionContract,
    SymbolHit,
    futures_symbol,
    option_symbol,
    rank_symbol_hits,
)
from src.brokers.upstox import UpstoxAdapter
from src.core.config import get_settings
from src.core.redis_client import get_redis
from src.core.roles import Role
from src.data.broker_provider import BrokerMarketDataProvider, NoRealDataSourceError
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
from src.main import app
from src.models.audit_log import AuditLog
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_desk_previews import preview_close, preview_enrolment
from src.orchestration.paper_subscription_controls import (
    SubscriptionStateError,
    plan_settings_change,
    update_settings,
)
from src.orchestration.paper_trading import enroll_in_paper_trading, equity_entry_quantity
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
TODAY = date.today()
EXPIRY = TODAY + timedelta(days=20)
FUT_EXPIRY = TODAY + timedelta(days=25)
FUT = futures_symbol("NIFTY", FUT_EXPIRY)
PUT_HIGH = option_symbol("NIFTY", EXPIRY, 24500.0, "PE")
PUT_LOW = option_symbol("NIFTY", EXPIRY, 24400.0, "PE")
CALL_ATM = option_symbol("NIFTY", EXPIRY, 24500.0, "CE")
CALL_HIGH = option_symbol("NIFTY", EXPIRY, 24600.0, "CE")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---- ranking ----


def _hits(*names, kind="equity"):
    return [SymbolHit(name, kind) for name in names]


def test_a_search_ranks_exact_then_prefix_then_contains_and_caps_the_list():
    hits = _hits("RELIANCE", "RELIGARE", "NRELIANCE", "RELI", "TCS")

    ranked = [h.symbol for h in rank_symbol_hits(hits, "reli", 10)]

    # exact, then prefixes (alphabetical at equal length), then names that merely contain it
    assert ranked == ["RELI", "RELIANCE", "RELIGARE", "NRELIANCE"]
    assert [h.symbol for h in rank_symbol_hits(hits, "reli", 2)] == ["RELI", "RELIANCE"]


def test_a_name_that_starts_with_the_query_beats_a_shorter_one_that_only_contains_it():
    hits = _hits("XREL", "RELIANCE")  # XREL is shorter, but RELIANCE starts with it

    assert [h.symbol for h in rank_symbol_hits(hits, "rel", 5)] == ["RELIANCE", "XREL"]


def test_an_index_ranks_ahead_of_an_equity_of_the_same_match_length_and_name_length():
    hits = _hits("NIFTYA") + _hits(
        "NIFTYB", kind="index"
    )  # equal rank and length; only kind differs

    assert [h.symbol for h in rank_symbol_hits(hits, "nifty", 5)] == ["NIFTYB", "NIFTYA"]


def test_an_index_ranks_ahead_of_an_equity_with_the_same_match_and_an_empty_query_finds_nothing():
    hits = _hits("NIFTYBEES") + _hits("NIFTY", kind="index")

    assert [h.symbol for h in rank_symbol_hits(hits, "nifty", 5)] == ["NIFTY", "NIFTYBEES"]
    assert rank_symbol_hits(hits, "   ", 5) == []
    assert rank_symbol_hits(hits, "nifty", 0) == []


# ---- the brokers' own lists ----


async def test_zerodha_searches_its_equities_and_the_indices_it_lists_not_the_other_series():
    from src.brokers import zerodha as zerodha_module
    from tests.test_brokers_historical import _Kite, _kite

    zerodha_module._nse_token_cache = None  # another test may have cached a different list
    adapter = _kite(_Kite())

    infy = await adapter.search_symbols("infy", 10)
    nifty = await adapter.search_symbols("nifty", 10)

    assert [(h.symbol, h.kind) for h in infy] == [("INFY", "equity"), ("INFYBEES", "equity")]
    assert ("NIFTY", "index") in [(h.symbol, h.kind) for h in nifty]
    assert "NIFTY 50" not in [h.symbol for h in nifty]  # the broker's own spelling is not offered
    assert await adapter.search_symbols("onlybe", 10) == []  # trades in another series only
    zerodha_module._nse_token_cache = None


class _Names:
    async def resolve(self, symbol: str) -> str:
        return symbol

    async def listed_names(self):
        return ["INFY", "TCS", "INFOSYS"], {"NIFTY", "NIFTY 50", "BANKNIFTY"}


async def test_upstox_searches_its_equities_and_the_supported_indices_only():
    adapter = UpstoxAdapter(BrokerCredentials(api_key="k", access_token="t"), instruments=_Names())

    hits = await adapter.search_symbols("inf", 10)
    indices = await adapter.search_symbols("nifty", 10)

    assert [h.symbol for h in hits] == ["INFY", "INFOSYS"]
    assert [h.symbol for h in indices] == ["NIFTY", "BANKNIFTY"]  # not "NIFTY 50"


class _SearchAdapter:
    def __init__(self, name, hits, error=None):
        self.broker_name = name
        self.hits = hits
        self.error = error

    async def search_symbols(self, query, limit=10):
        if self.error:
            raise self.error
        return self.hits


async def test_the_provider_merges_brokers_once_per_name_and_skips_a_broker_that_fails(tmp_path):
    zerodha = _SearchAdapter("zerodha", _hits("INFY", "INFOSYS"))
    upstox = _SearchAdapter("upstox", _hits("INFY", "INFRATEL"))
    broken = _SearchAdapter("other", [], error=RuntimeError("down"))
    provider = BrokerMarketDataProvider(lambda: [broken, zerodha, upstox], root=tmp_path)

    hits = await provider.search_symbols("inf", 10)

    assert [h.symbol for h in hits] == ["INFY", "INFOSYS", "INFRATEL"]


async def test_with_no_broker_connected_a_search_says_so_instead_of_returning_nothing(tmp_path):
    provider = BrokerMarketDataProvider(lambda: [], root=tmp_path)

    with pytest.raises(NoRealDataSourceError):
        await provider.search_symbols("inf", 10)


# ---- what enrolling would do ----


class _Provider:
    """A broker-shaped provider: lists NIFTY and RELIANCE, one future, an option chain, prices."""

    source_name = "broker_market_data"

    def __init__(self, *, connected=True, prices=None, contracts=None, future=None):
        self.connected = connected
        self.prices = prices if prices is not None else {}
        self.contracts = contracts if contracts is not None else _chain()
        self.future = future
        self.searched: list[str] = []

    async def symbol_listed(self, symbol):
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return symbol in {"NIFTY", "RELIANCE"}

    async def indicative_price(self, symbol):
        return self.prices.get(symbol)

    async def futures_contract(self, underlying, *, today, roll_days):
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return self.future or FuturesContract(FUT, underlying, FUT_EXPIRY, 50, 0.05)

    async def option_contracts(self, underlying):
        if not self.connected:
            raise NoRealDataSourceError("no broker is connected")
        return self.contracts

    async def search_symbols(self, query, limit=10):
        self.searched.append(query)
        return _hits("RELIANCE") if query.upper().startswith("REL") else []

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


def _chain():
    return [
        OptionContract(option_symbol("NIFTY", EXPIRY, s, k), "NIFTY", EXPIRY, s, k, 50, 0.05)
        for s in (24300.0, 24400.0, 24500.0, 24600.0, 24700.0)
        for k in ("CE", "PE")
    ]


async def _preview(db_session_factory, provider, **kwargs):
    args = {
        "symbol": "RELIANCE",
        "instrument_kind": "equity",
        "initial_capital": 100_000.0,
        "position_size_pct": 5.0,
        "lots": 1,
        "option_strategy": "long_option",
        "spread_width": 1,
    } | kwargs
    async with db_session_factory() as db:
        return await preview_enrolment(db, provider, today=TODAY, **args)


def test_the_equity_entry_is_whole_shares_of_the_size_share_of_capital():
    assert equity_entry_quantity(100_000.0, 5.0, 2_874.0) == 1  # Rs 5,000 buys one at 2,874
    assert equity_entry_quantity(100_000.0, 5.0, 5_001.0) == 0
    assert equity_entry_quantity(1_000_000.0, 10.0, 2_500.0) == 40


async def test_an_equity_preview_sizes_the_entry_with_the_engines_own_rule(db_session_factory):
    preview = await _preview(
        db_session_factory, _Provider(prices={"RELIANCE": 1_250.0}), position_size_pct=5.0
    )

    assert preview.listing == "listed" and preview.problems == []
    assert preview.equity == {"entry_capital": 5_000.0, "shares": 4, "notional": 5_000.0}


async def test_a_share_dearer_than_one_entry_is_a_warning_not_a_refusal(db_session_factory):
    preview = await _preview(db_session_factory, _Provider(prices={"RELIANCE": 9_000.0}))

    assert preview.problems == []
    assert preview.equity["shares"] == 0
    assert any("never buy" in w for w in preview.warnings)


async def test_an_unlisted_symbol_is_a_problem_and_nothing_else_is_worked_out(db_session_factory):
    preview = await _preview(db_session_factory, _Provider(), symbol="NOPE")

    assert preview.listing == "not_listed"
    assert "no connected broker lists" in preview.problems[0]
    assert preview.equity is None


async def test_with_no_broker_the_symbol_cannot_be_checked_but_may_still_be_added(
    db_session_factory,
):
    preview = await _preview(db_session_factory, _Provider(connected=False, prices={}))

    assert preview.listing == "unknown" and preview.problems == []
    assert any("No broker is connected" in w for w in preview.warnings)


async def test_a_future_preview_gives_the_contract_lot_size_and_whether_the_margin_fits(
    db_session_factory,
):
    # 2 lots x 50 = 100 units at 24,000 = Rs 24,00,000 notional; 15% margin = Rs 3,60,000.
    provider = _Provider(prices={FUT: 24_000.0})
    fits = await _preview(
        db_session_factory,
        provider,
        symbol="NIFTY",
        instrument_kind="future",
        lots=2,
        initial_capital=1_000_000.0,
    )

    assert fits.problems == []
    assert fits.future["contract"] == FUT and fits.future["lot_size"] == 50
    assert fits.future["quantity"] == 100 and fits.future["notional"] == 2_400_000.0
    assert fits.future["margin"] == pytest.approx(360_000.0)
    assert fits.future["margin_limit"] == pytest.approx(500_000.0)  # 50% of capital
    assert fits.future["fits"] is True
    assert fits.future["rolls_on"] == (FUT_EXPIRY - timedelta(days=3)).isoformat()

    too_big = await _preview(
        db_session_factory,
        provider,
        symbol="NIFTY",
        instrument_kind="future",
        lots=2,
        initial_capital=500_000.0,
    )
    assert too_big.future["fits"] is False
    assert "do not fit this account" in too_big.problems[0]


async def test_a_future_preview_without_a_price_says_the_margin_cannot_be_estimated(
    db_session_factory,
):
    preview = await _preview(
        db_session_factory, _Provider(), symbol="NIFTY", instrument_kind="future"
    )

    assert preview.problems == [] and preview.future["margin"] is None
    assert any("margin can't be estimated" in w for w in preview.warnings)


async def test_an_option_preview_shows_each_directions_legs_and_largest_loss(db_session_factory):
    prices = {"NIFTY": 24_510.0, PUT_HIGH: 150.0, PUT_LOW: 100.0, CALL_ATM: 160.0, CALL_HIGH: 105.0}
    preview = await _preview(
        db_session_factory,
        _Provider(prices=prices),
        symbol="NIFTY",
        instrument_kind="option",
        option_strategy="vertical_spread",
        spread_width=1,
        initial_capital=1_000_000.0,
    )

    buy = next(s for s in preview.scenarios if s.signal == "BUY")
    assert buy.view == "bullish"
    # Bull put spread: buy the lower put, sell the at-the-money put, 50 units each.
    assert [(leg.side, leg.symbol, leg.quantity) for leg in buy.legs] == [
        ("buy", PUT_LOW, 50),
        ("sell", PUT_HIGH, 50),
    ]
    # Width 100 x 50 = 5,000, less the credit (150 - 100) x 50 = 2,500.
    assert buy.max_loss == pytest.approx(2_500.0) and buy.fits is True
    sell = next(s for s in preview.scenarios if s.signal == "SELL")
    assert sell.view == "bearish" and [leg.option_type for leg in sell.legs] == ["CE", "CE"]
    assert preview.option["expiry"] == EXPIRY.isoformat() and preview.option["lot_size"] == 50


async def test_a_long_option_can_lose_what_it_costs_and_that_is_checked_against_the_account(
    db_session_factory,
):
    prices = {"NIFTY": 24_500.0, CALL_ATM: 160.0, PUT_HIGH: 150.0}
    preview = await _preview(
        db_session_factory,
        _Provider(prices=prices),
        symbol="NIFTY",
        instrument_kind="option",
        option_strategy="long_option",
        lots=2,
        initial_capital=20_000.0,
    )

    buy = next(s for s in preview.scenarios if s.signal == "BUY")
    assert buy.max_loss == pytest.approx(16_000.0)  # 2 lots x 50 x Rs 160
    assert buy.fits is False  # limit is 50% of Rs 20,000
    assert any("Neither direction fits" in w for w in preview.warnings)


async def test_an_option_leg_with_no_price_leaves_the_loss_unknown_not_zero(db_session_factory):
    preview = await _preview(
        db_session_factory,
        _Provider(prices={"NIFTY": 24_500.0}),
        symbol="NIFTY",
        instrument_kind="option",
    )

    assert all(s.max_loss is None and s.fits is None for s in preview.scenarios)
    assert "can't be worked out" in preview.scenarios[0].note


async def test_options_with_no_expiry_far_enough_out_are_refused(db_session_factory):
    near = [
        OptionContract(
            option_symbol("NIFTY", TODAY + timedelta(days=1), 24500.0, "CE"),
            "NIFTY",
            TODAY + timedelta(days=1),
            24500.0,
            "CE",
            50,
            0.05,
        )
    ]
    preview = await _preview(
        db_session_factory, _Provider(contracts=near), symbol="NIFTY", instrument_kind="option"
    )

    assert "expires more than" in preview.problems[0]


async def test_a_duplicate_active_subscription_is_a_problem_when_the_strategy_is_known(
    db_session_factory,
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    async with db_session_factory() as db:
        preview = await preview_enrolment(
            db,
            _Provider(prices={"RELIANCE": 100.0}),
            symbol="RELIANCE",
            instrument_kind="equity",
            initial_capital=100_000.0,
            position_size_pct=5.0,
            lots=1,
            option_strategy="long_option",
            spread_width=2,
            strategy_version_id=subscription.strategy_version_id,
            today=TODAY,
        )

    assert "already exists" in preview.problems[0]


# ---- what closing would do ----


async def _sub(db_session_factory, *, symbol="NIFTY", kind="option", **fields):
    async with db_session_factory() as db:
        created = await create_strategy(db, name=f"DESK-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, created, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol=symbol,
            initial_capital=fields.pop("capital", 1_000_000.0),
            stop_loss_pct=fields.pop("stop_loss_pct", 50.0 if kind == "option" else 3.0),
            option_strategy="vertical_spread" if kind == "option" else None,
            **fields,
        )


async def _hold(db_session_factory, subscription, symbol, quantity, avg_cost):
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol=symbol, quantity=quantity, avg_cost=avg_cost
            )
        )
        await db.commit()


async def _book(redis, symbol, price, *, bid_qty=1_000, ask_qty=1_000, age_s=0.0, tick=True):
    await publish_depth(
        redis,
        symbol,
        DepthSnapshot(
            bids=(OrderBookLevel(price - 1.0, bid_qty),),
            asks=(OrderBookLevel(price + 1.0, ask_qty),),
            timestamp_ms=_now_ms() - int(age_s * 1000),
        ),
        ttl_seconds=60,
    )
    if tick:
        await publish_tick(redis, Tick(symbol, price, _now_ms(), "real"))


@pytest_asyncio.fixture
async def redis(redis_client):
    symbols = (PUT_HIGH, PUT_LOW, "NIFTY", "RELIANCE")
    keys = [k for s in symbols for k in (depth_key(s), tick_stream_key(s))]
    await redis_client.delete(*keys)
    yield redis_client
    await redis_client.delete(*keys)


async def _close_preview(db_session_factory, redis, subscription):
    async with db_session_factory() as db:
        return await preview_close(
            db,
            redis,
            subscription.id,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
        )


async def test_a_close_preview_walks_the_book_the_way_the_close_will(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0)  # bid 109, ask 111

    preview = await _close_preview(db_session_factory, redis, subscription)

    leg = preview.legs[0]
    assert (leg.side, leg.quantity, leg.est_filled, leg.est_remaining) == ("sell", 10, 10, 0)
    assert leg.est_avg_price == pytest.approx(109.0)  # sells into the bid, not at the mid
    assert leg.est_gross_pnl == pytest.approx((109.0 - 100.0) * 10)
    assert leg.est_costs is not None and leg.est_costs > 0
    assert leg.reference_price == 110.0 and leg.reference_source == "real"
    assert leg.book_source == "real"  # the broker's live depth, not a generated book
    assert preview.can_close and preview.blockers == [] and leg.problem is None
    assert preview.est_gross_pnl == pytest.approx(90.0)


async def test_an_estimate_on_a_generated_book_says_so(db_session_factory, redis):
    from src.engine.paper_trading.order_book import MockOrderBookProvider

    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await publish_tick(redis, Tick("RELIANCE", 110.0, _now_ms(), "real"))  # a price, no live depth

    async with db_session_factory() as db:
        preview = await preview_close(
            db, redis, subscription.id, order_book_provider=MockOrderBookProvider()
        )

    leg = preview.legs[0]
    assert leg.est_filled == 10 and leg.problem is None  # it fills, against made-up depth
    assert leg.book_source == "synthetic"
    assert leg.book_age_seconds is None  # there is no snapshot to be old


async def test_a_short_leg_is_bought_back_at_the_ask_and_gains_when_the_price_fell(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", -10, 120.0)
    await _book(redis, "RELIANCE", 110.0)

    leg = (await _close_preview(db_session_factory, redis, subscription)).legs[0]

    assert leg.side == "buy" and leg.est_avg_price == pytest.approx(111.0)
    assert leg.est_gross_pnl == pytest.approx((120.0 - 111.0) * 10)


async def test_thin_depth_gives_a_partial_estimate_not_a_full_one(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0, bid_qty=4)

    leg = (await _close_preview(db_session_factory, redis, subscription)).legs[0]

    assert (leg.est_filled, leg.est_remaining) == (4, 6)


async def test_with_no_fresh_price_the_preview_says_what_the_close_will_say(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)

    preview = await _close_preview(db_session_factory, redis, subscription)

    assert "no price has ever been published" in preview.legs[0].problem
    assert preview.can_close is False and preview.est_costs_total is None


async def test_a_stale_book_is_a_problem_even_with_a_fresh_price(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0, age_s=300.0)

    leg = (await _close_preview(db_session_factory, redis, subscription)).legs[0]

    assert "no live order book" in leg.problem and leg.reference_price == 110.0


async def test_a_spread_lists_the_sold_leg_first_and_leaves_the_hedge_when_it_cannot_close(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory)
    await _hold(db_session_factory, subscription, PUT_LOW, 50, 100.0)
    await _hold(db_session_factory, subscription, PUT_HIGH, -50, 150.0)
    await _book(redis, PUT_LOW, 90.0)
    # The sold leg has no price at all.

    preview = await _close_preview(db_session_factory, redis, subscription)

    assert [leg.symbol for leg in preview.legs] == [PUT_HIGH, PUT_LOW]
    assert preview.legs[0].problem is not None
    assert "would be left open" in preview.legs[1].problem
    assert preview.legs[1].est_filled == 0 and preview.can_close is False


async def test_a_spread_with_both_legs_priced_totals_costs_and_result(db_session_factory, redis):
    subscription = await _sub(db_session_factory)
    await _hold(db_session_factory, subscription, PUT_LOW, 50, 100.0)
    await _hold(db_session_factory, subscription, PUT_HIGH, -50, 150.0)
    await _book(redis, PUT_LOW, 90.0)
    await _book(redis, PUT_HIGH, 130.0)

    preview = await _close_preview(db_session_factory, redis, subscription)

    sold, bought = preview.legs
    assert sold.est_avg_price == pytest.approx(131.0)  # buys back at the ask
    assert bought.est_avg_price == pytest.approx(89.0)  # sells at the bid
    assert preview.est_gross_pnl == pytest.approx((150 - 131) * 50 + (89 - 100) * 50)
    assert preview.est_costs_total == pytest.approx(sold.est_costs + bought.est_costs)
    assert preview.can_close


async def test_a_supplied_price_stands_in_for_a_missing_tick_but_the_book_is_still_walked(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0, tick=False)  # a book, but no price published

    without = await _close_preview(db_session_factory, redis, subscription)
    async with db_session_factory() as db:
        supplied = await preview_close(
            db,
            redis,
            subscription.id,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            price=112.0,
        )

    assert "no price has ever been published" in without.legs[0].problem
    leg = supplied.legs[0]
    assert leg.problem is None and supplied.can_close
    assert leg.reference_price == 112.0 and leg.reference_source == "supplied"
    assert leg.reference_age_seconds is None  # it is the operator's number, not a tick
    assert leg.est_avg_price == pytest.approx(109.0)  # the book, not the supplied price, fills it


async def test_a_supplied_price_is_not_dressed_up_as_a_tick_even_when_one_exists(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0)  # a fresh tick at 110 exists too

    async with db_session_factory() as db:
        preview = await preview_close(
            db,
            redis,
            subscription.id,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            price=112.0,
        )

    leg = preview.legs[0]
    assert (leg.reference_price, leg.reference_source) == (112.0, "supplied")
    assert leg.reference_age_seconds is None  # not the age of the 110 tick


async def test_a_supplied_price_means_one_price_for_one_position_so_a_spread_ignores_it(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory)
    await _hold(db_session_factory, subscription, PUT_LOW, 50, 100.0)
    await _hold(db_session_factory, subscription, PUT_HIGH, -50, 150.0)

    async with db_session_factory() as db:
        preview = await preview_close(
            db,
            redis,
            subscription.id,
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            price=100.0,
        )

    assert preview.legs[0].problem is not None and preview.legs[0].reference_price is None
    assert any("one-position subscription only" in note for note in preview.notes)


async def test_a_flat_subscription_has_nothing_to_close(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")

    preview = await _close_preview(db_session_factory, redis, subscription)

    assert preview.legs == [] and "no open position" in preview.blockers[0]


async def test_a_tripped_kill_switch_is_a_blocker(db_session_factory, redis):
    from src.models.kill_switch_state import KillSwitchState

    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0)
    async with db_session_factory() as db:
        state = (
            await db.execute(select(KillSwitchState).where(KillSwitchState.mode == "paper"))
        ).scalar_one_or_none()
        if state is None:
            state = KillSwitchState(mode="paper", threshold_pct=15.0)
            db.add(state)
        state.tripped, state.trip_reason = True, "drawdown 16%"
        await db.commit()
    try:
        preview = await _close_preview(db_session_factory, redis, subscription)
    finally:
        async with db_session_factory() as db:
            state = (
                await db.execute(select(KillSwitchState).where(KillSwitchState.mode == "paper"))
            ).scalar_one()
            state.tripped, state.trip_reason = False, None
            await db.commit()

    assert "kill switch is tripped: drawdown 16%" in preview.blockers[0]
    assert preview.can_close is False


async def test_a_close_preview_writes_nothing(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0)

    await _close_preview(db_session_factory, redis, subscription)

    async with db_session_factory() as db:
        fills = (await db.execute(select(PaperFill))).scalars().all()
        position = (await db.execute(select(PaperPosition))).scalars().first()
    assert [f for f in fills if f.subscription_id == subscription.id] == []
    assert position.quantity == 10


# ---- changing settings ----


def _sub_obj(**overrides):
    base = {
        "instrument_kind": "equity",
        "option_strategy": None,
        "stop_loss_pct": 3.0,
        "position_size_pct": 5.0,
        "lots": 1,
        "spread_width": 2,
        "sma_window": 15,
        "builtin_strategy": "sma_crossover",
        "initial_capital": 100_000.0,
    }
    return PaperTradingSubscription(**(base | overrides))


def test_only_values_that_differ_are_changes():
    changes = plan_settings_change(
        _sub_obj(), {"stop_loss_pct": 5.0, "sma_window": 15}, has_fills=False
    )

    assert [(c.field, c.before, c.after) for c in changes] == [("stop_loss_pct", 3.0, 5.0)]


def test_changing_nothing_is_refused_not_silently_accepted():
    with pytest.raises(SubscriptionStateError, match="nothing to change"):
        plan_settings_change(_sub_obj(), {"stop_loss_pct": 3.0}, has_fills=False)


@pytest.mark.parametrize(
    ("subscription", "setting", "value"),
    [
        (_sub_obj(instrument_kind="future"), "position_size_pct", 10.0),
        (_sub_obj(instrument_kind="equity"), "lots", 3),
        (_sub_obj(instrument_kind="option", option_strategy="long_option"), "spread_width", 4),
    ],
)
def test_a_setting_that_does_not_apply_to_the_kind_is_refused(subscription, setting, value):
    with pytest.raises(SubscriptionStateError, match="does not apply"):
        plan_settings_change(subscription, {setting: value}, has_fills=False)


def test_a_hedge_distance_applies_to_a_credit_spread():
    spread = _sub_obj(instrument_kind="option", option_strategy="vertical_spread")

    assert plan_settings_change(spread, {"spread_width": 4}, has_fills=True)[0].after == 4


def test_capital_can_change_until_the_first_fill_and_not_after():
    assert plan_settings_change(_sub_obj(), {"initial_capital": 200_000.0}, has_fills=False)
    with pytest.raises(SubscriptionStateError, match="once the subscription has traded"):
        plan_settings_change(_sub_obj(), {"initial_capital": 200_000.0}, has_fills=True)


def test_what_a_subscription_trades_cannot_be_changed():
    with pytest.raises(SubscriptionStateError, match="cannot be changed"):
        plan_settings_change(_sub_obj(), {"symbol": "TCS"}, has_fills=False)  # type: ignore[dict-item]


async def test_changing_settings_updates_the_row_and_audits_before_and_after(
    db_session_factory, redis
):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)

    async with db_session_factory() as db:
        updated, changes = await update_settings(
            db,
            redis,
            subscription.id,
            actor="pm",
            reason="widen the stop",
            requested={"stop_loss_pct": 6.0, "sma_window": 20},
        )

    assert (updated.stop_loss_pct, updated.sma_window) == (6.0, 20)
    assert {c.field for c in changes} == {"stop_loss_pct", "sma_window"}
    async with db_session_factory() as db:
        entry = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == str(subscription.id))
                .where(AuditLog.action == "paper_trading.subscription_settings_changed")
            )
        ).scalar_one()
    assert entry.actor == "pm"
    assert entry.details["reason"] == "widen the stop" and entry.details["open_legs"] == 1
    assert entry.details["changes"]["stop_loss_pct"] == {"before": 3.0, "after": 6.0}


async def test_capital_is_refused_once_a_fill_exists(db_session_factory, redis):
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    async with db_session_factory() as db:
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="RELIANCE",
                side="buy",
                data_source="real",
                order_group_id=uuid.uuid4(),
                leg_index=0,
                requested_quantity=1,
                filled_quantity=1,
                avg_fill_price=100.0,
                fully_filled=True,
            )
        )
        await db.commit()

    async with db_session_factory() as db:
        with pytest.raises(SubscriptionStateError, match="once the subscription has traded"):
            await update_settings(
                db,
                redis,
                subscription.id,
                actor="pm",
                reason="more",
                requested={"initial_capital": 500_000.0},
            )


async def test_resizing_an_option_subscription_drops_the_entry_plan_chosen_under_the_old_size(
    db_session_factory, redis
):
    from src.orchestration.paper_options import plan_key

    subscription = await _sub(db_session_factory, lots=1)
    await redis.set(plan_key(subscription.id), "{}")

    async with db_session_factory() as db:
        await update_settings(
            db, redis, subscription.id, actor="pm", reason="two lots", requested={"lots": 2}
        )

    assert await redis.get(plan_key(subscription.id)) is None


async def test_a_change_that_does_not_resize_keeps_the_plan(db_session_factory, redis):
    from src.orchestration.paper_options import plan_key

    subscription = await _sub(db_session_factory)
    await redis.set(plan_key(subscription.id), "{}")

    async with db_session_factory() as db:
        await update_settings(
            db,
            redis,
            subscription.id,
            actor="pm",
            reason="tighter stop",
            requested={"stop_loss_pct": 40.0},
        )

    assert await redis.get(plan_key(subscription.id)) == "{}"
    await redis.delete(plan_key(subscription.id))


# ---- the routes ----


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


async def _login(client, make_user, email, role=Role.SYSTEM_ADMINISTRATOR) -> dict:
    await make_user(email, "supersecret1", role)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_the_symbol_search_route_returns_ranked_hits(client, make_user, api):
    _use(_Provider())
    headers = await _login(client, make_user, "ds1@example.com")

    resp = await client.get("/api/v1/paper-trading/symbols?q=rel", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "available": True,
        "reason": None,
        "symbols": [{"symbol": "RELIANCE", "kind": "equity"}],
    }


async def test_the_search_route_says_when_there_is_nothing_to_search(client, make_user, api):
    class _Disconnected:
        async def search_symbols(self, query, limit=10):
            raise NoRealDataSourceError("no broker is connected")

    _use(_Disconnected())
    headers = await _login(client, make_user, "ds2@example.com")

    body = (await client.get("/api/v1/paper-trading/symbols?q=rel", headers=headers)).json()

    assert body["available"] is False and "no broker is connected" in body["reason"]


async def test_the_search_route_validates_its_query(client, make_user, api):
    _use(_Provider())
    headers = await _login(client, make_user, "ds3@example.com")

    assert (await client.get("/api/v1/paper-trading/symbols", headers=headers)).status_code == 422
    too_many = await client.get("/api/v1/paper-trading/symbols?q=a&limit=99", headers=headers)
    assert too_many.status_code == 422


async def test_the_enrol_preview_route_matches_the_real_enrolments_refusal(client, make_user, api):
    provider = _Provider(prices={FUT: 24_000.0})
    _use(provider)
    headers = await _login(client, make_user, "ds4@example.com")
    body = {
        "symbol": "NIFTY",
        "instrument_kind": "future",
        "lots": 2,
        "initial_capital": 500_000.0,
        "strategy_version_id": str(uuid.uuid4()),
    }

    preview = await client.get("/api/v1/paper-trading/enrol-preview", params=body, headers=headers)
    real = await client.post("/api/v1/paper-trading/subscriptions", json=body, headers=headers)

    assert preview.status_code == 200
    assert preview.json()["future"]["fits"] is False
    assert real.status_code == 422
    assert preview.json()["problems"][0].split(": ", 1)[1] in real.json()["detail"]


async def test_the_enrol_preview_creates_nothing(client, make_user, api, db_session_factory):
    _use(_Provider(prices={"RELIANCE": 100.0}))
    headers = await _login(client, make_user, "ds5@example.com")
    before = await _count(db_session_factory)

    resp = await client.get(
        "/api/v1/paper-trading/enrol-preview",
        params={"symbol": "RELIANCE", "instrument_kind": "equity"},
        headers=headers,
    )

    assert resp.status_code == 200 and resp.json()["equity"]["shares"] == 50
    assert await _count(db_session_factory) == before


async def _count(db_session_factory) -> int:
    async with db_session_factory() as db:
        return len((await db.execute(select(PaperTradingSubscription))).scalars().all())


async def test_a_preview_leaves_nothing_in_the_audit_trail(
    client, make_user, api, db_session_factory
):
    _use(_Provider(prices={"RELIANCE": 100.0}))
    headers = await _login(client, make_user, "ds5b@example.com")

    for _ in range(3):
        await client.get(
            "/api/v1/paper-trading/enrol-preview",
            params={"symbol": "RELIANCE", "instrument_kind": "equity"},
            headers=headers,
        )

    async with db_session_factory() as db:
        rows = (
            (await db.execute(select(AuditLog).where(AuditLog.action.like("%enrol-preview%"))))
            .scalars()
            .all()
        )
    assert rows == []


async def test_the_previews_and_settings_are_operator_only(client, make_user, api):
    _use(_Provider())
    headers = await _login(client, make_user, "ds6@example.com", Role.RISK_MANAGER)
    sub_id = uuid.uuid4()

    assert (
        await client.get("/api/v1/paper-trading/symbols?q=a", headers=headers)
    ).status_code == 403
    enrol = await client.get(
        "/api/v1/paper-trading/enrol-preview",
        params={"symbol": "A", "instrument_kind": "equity"},
        headers=headers,
    )
    assert enrol.status_code == 403
    close = await client.get(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/close-preview", headers=headers
    )
    assert close.status_code == 403
    patch = await client.patch(
        f"/api/v1/paper-trading/subscriptions/{sub_id}/settings",
        json={"reason": "why", "lots": 2},
        headers=headers,
    )
    assert patch.status_code == 403


async def test_the_settings_route_changes_and_reports_what_changed(
    client, make_user, api, db_session_factory
):
    headers = await _login(client, make_user, "ds7@example.com")
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")

    resp = await client.patch(
        f"/api/v1/paper-trading/subscriptions/{subscription.id}/settings",
        json={"reason": "wider stop", "stop_loss_pct": 7.5, "lots": None},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subscription"]["stop_loss_pct"] == 7.5
    assert body["changes"] == [{"field": "stop_loss_pct", "before": 3.0, "after": 7.5}]


async def test_the_settings_route_refuses_a_no_op_a_short_reason_and_bad_values(
    client, make_user, api, db_session_factory
):
    headers = await _login(client, make_user, "ds8@example.com")
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    url = f"/api/v1/paper-trading/subscriptions/{subscription.id}/settings"

    assert (
        await client.patch(url, json={"reason": "same", "stop_loss_pct": 3.0}, headers=headers)
    ).status_code == 409
    assert (
        await client.patch(url, json={"reason": "x", "stop_loss_pct": 4.0}, headers=headers)
    ).status_code == 422
    assert (
        await client.patch(url, json={"reason": "ok ok", "stop_loss_pct": 0}, headers=headers)
    ).status_code == 422
    assert (
        await client.patch(url, json={"reason": "ok ok", "position_size_pct": 101}, headers=headers)
    ).status_code == 422
    missing = f"/api/v1/paper-trading/subscriptions/{uuid.uuid4()}/settings"
    assert (
        await client.patch(missing, json={"reason": "ok ok", "stop_loss_pct": 4.0}, headers=headers)
    ).status_code == 404


async def test_the_close_preview_route_returns_the_estimate(
    client, make_user, api, db_session_factory, redis
):
    headers = await _login(client, make_user, "ds9@example.com")
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0)

    resp = await client.get(
        f"/api/v1/paper-trading/subscriptions/{subscription.id}/close-preview", headers=headers
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_close"] is True and body["legs"][0]["est_avg_price"] == 109.0
    missing = await client.get(
        f"/api/v1/paper-trading/subscriptions/{uuid.uuid4()}/close-preview", headers=headers
    )
    assert missing.status_code == 404


async def test_the_close_preview_route_takes_a_supplied_price_and_refuses_a_bad_one(
    client, make_user, api, db_session_factory, redis
):
    headers = await _login(client, make_user, "ds10@example.com")
    subscription = await _sub(db_session_factory, symbol="RELIANCE", kind="equity")
    await _hold(db_session_factory, subscription, "RELIANCE", 10, 100.0)
    await _book(redis, "RELIANCE", 110.0, tick=False)
    url = f"/api/v1/paper-trading/subscriptions/{subscription.id}/close-preview"

    plain = (await client.get(url, headers=headers)).json()
    priced = (await client.get(f"{url}?price=112", headers=headers)).json()

    assert plain["can_close"] is False and priced["can_close"] is True
    assert priced["legs"][0]["reference_source"] == "supplied"
    assert (await client.get(f"{url}?price=0", headers=headers)).status_code == 422
