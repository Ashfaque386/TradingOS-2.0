"""What a futures trade costs, what it needs, and which order a signal calls for (phase 4).

Rates are Zerodha's published equity-futures schedule (read 2026-10-17): brokerage 0.03% or Rs 20,
STT 0.05% on the sell side, NSE charge 0.00183%, SEBI Rs 10 per crore, stamp duty 0.002% on the buy
side, GST 18% on brokerage + SEBI + exchange charges. Figures change: the tests pin the arithmetic,
and `PAPER_FRICTION_OVERRIDES` is how a new rate goes in.
"""

import pytest

from src.core.config import get_settings
from src.engine.backtest.friction import FrictionModel, compute_futures_leg_costs
from src.engine.paper_trading.margin import check_futures_margin
from src.engine.paper_trading.order_book import FillResult
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_trading import decide_order, fill_costs, friction_model

# ---- the cost schedule, worked by hand on a Rs 10 lakh leg ----


def test_a_buy_leg_pays_stamp_duty_and_no_stt():
    costs = compute_futures_leg_costs(turnover=1_000_000, is_buy=True)

    assert costs.brokerage == 20.0  # 0.03% would be Rs 300: the Rs 20 cap applies
    assert costs.stt == 0.0
    assert costs.exchange_txn_charges == pytest.approx(18.301)  # 0.00183% + IPFT Rs 0.01 a crore
    assert costs.sebi_fee == pytest.approx(1.0)  # Rs 10 a crore
    assert costs.stamp_duty == pytest.approx(20.0)  # 0.002%
    assert costs.gst == pytest.approx(0.18 * (20.0 + 1.0 + 18.301))  # brokerage + SEBI + exchange
    assert costs.total == pytest.approx(66.37518)


def test_a_sell_leg_pays_stt_and_no_stamp_duty():
    costs = compute_futures_leg_costs(turnover=1_000_000, is_buy=False)

    assert costs.stt == pytest.approx(500.0)  # 0.05%
    assert costs.stamp_duty == 0.0
    assert costs.total == pytest.approx(546.37518)


def test_brokerage_is_a_percentage_below_the_cap():
    costs = compute_futures_leg_costs(turnover=10_000, is_buy=True)

    assert costs.brokerage == pytest.approx(3.0)  # 0.03% of Rs 10,000


def test_gst_is_charged_on_the_sebi_fee_for_futures_unlike_the_cash_schedule():
    futures = compute_futures_leg_costs(turnover=1_000_000, is_buy=True)

    assert futures.gst == pytest.approx(
        0.18 * (futures.brokerage + futures.sebi_fee + futures.exchange_txn_charges)
    )
    assert futures.gst > 0.18 * (futures.brokerage + futures.exchange_txn_charges)


def test_a_rate_can_be_replaced_without_touching_code():
    model = FrictionModel(futures_stt_sell_pct=0.0002, futures_brokerage_cap=0.0)

    costs = compute_futures_leg_costs(turnover=1_000_000, is_buy=False, model=model)

    assert costs.stt == pytest.approx(200.0)
    assert costs.brokerage == pytest.approx(300.0)  # no cap: the full 0.03%


# ---- configured overrides ----


@pytest.fixture
def settings_reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_overrides_from_the_environment_reach_the_model(monkeypatch, settings_reset):
    monkeypatch.setenv("PAPER_FRICTION_OVERRIDES", '{"futures_stt_sell_pct": 0.0002}')
    get_settings.cache_clear()

    assert friction_model().futures_stt_sell_pct == 0.0002
    assert friction_model().futures_exchange_txn_pct == FrictionModel().futures_exchange_txn_pct


def test_no_overrides_means_the_published_defaults(monkeypatch, settings_reset):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()

    assert friction_model() == FrictionModel()


def test_an_override_naming_no_such_rate_is_an_error_not_ignored(monkeypatch, settings_reset):
    monkeypatch.setenv("PAPER_FRICTION_OVERRIDES", '{"futures_stt_sel_pct": 0.0002}')  # a typo
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="futures_stt_sel_pct"):
        friction_model()


def test_a_fill_on_a_future_is_charged_the_futures_schedule(monkeypatch, settings_reset):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    fill = FillResult(65, 65, 0, 15_384.615384615385, True)  # turnover Rs 10 lakh

    total, breakdown = fill_costs("sell", fill, instrument_kind="future")

    assert total == pytest.approx(546.37518)
    assert breakdown["segment"] == "future" and breakdown["product"] == "nrml"
    assert breakdown["stt"] == pytest.approx(500.0)


def test_a_fill_on_equity_is_still_charged_the_cash_schedule(monkeypatch, settings_reset):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    monkeypatch.setenv("PAPER_TRADING_PRODUCT", "cnc")
    get_settings.cache_clear()

    _, breakdown = fill_costs("buy", FillResult(100, 100, 0, 100.0, True))

    assert breakdown["segment"] == "equity" and breakdown["product"] == "cnc"
    assert breakdown["stt"] == pytest.approx(10_000 * 0.001)  # delivery STT on the buy leg


# ---- margin ----


def test_an_entry_within_the_share_of_capital_is_allowed():
    check = check_futures_margin(
        price=24_000, quantity=65, capital=1_000_000, margin_pct=15, max_utilisation_pct=50
    )

    assert check.ok and check.reason is None
    assert check.required == pytest.approx(234_000.0)  # 15% of Rs 15.6 lakh
    assert check.limit == pytest.approx(500_000.0)


def test_an_entry_that_needs_more_than_the_allowed_share_is_refused_and_says_why():
    check = check_futures_margin(
        price=24_000, quantity=65, capital=100_000, margin_pct=15, max_utilisation_pct=50
    )

    assert not check.ok
    assert "234,000" in check.reason and "50,000" in check.reason
    assert "approximation, not SPAN" in check.reason


def test_an_entry_needing_exactly_the_limit_is_allowed():
    check = check_futures_margin(
        price=1_000, quantity=100, capital=200_000, margin_pct=50, max_utilisation_pct=25
    )

    assert check.required == check.limit == 50_000.0
    assert check.ok


# ---- which order a signal calls for ----


def _subscription(kind: str, **overrides) -> PaperTradingSubscription:
    values = {
        "symbol": "NIFTY" if kind == "future" else "INFY",
        "instrument_kind": kind,
        "contract_symbol": "NFO:NIFTY26OCTFUT" if kind == "future" else None,
        "lot_size": 65 if kind == "future" else None,
        "lots": 2,
        "initial_capital": 100_000.0,
        "position_size_pct": 5.0,
    }
    values.update(overrides)
    return PaperTradingSubscription(**values)


@pytest.mark.parametrize(
    ("position", "signal", "expected"),
    [
        (0, "BUY", ("buy", 130)),  # 2 lots of 65
        (0, "SELL", ("sell", 130)),  # a short
        (130, "SELL", ("sell", 130)),  # a long sold out
        (-130, "BUY", ("buy", 130)),  # a short covered
        (130, "BUY", None),  # already long
        (-130, "SELL", None),  # already short
    ],
)
def test_a_future_trades_both_ways_in_whole_lots(position, signal, expected):
    assert (
        decide_order(
            _subscription("future"),
            position_quantity=position,
            signal_type=signal,
            tick_price=24_000.0,
        )
        == expected
    )


def test_the_exit_is_the_whole_position_even_if_it_is_not_a_multiple_of_the_lot():
    order = decide_order(
        _subscription("future"), position_quantity=-45, signal_type="BUY", tick_price=24_000.0
    )

    assert order == ("buy", 45)  # 130 asked, only 45 had filled: cover exactly what is held


def test_a_future_with_no_lot_size_orders_nothing():
    subscription = _subscription("future", lot_size=0)

    assert (
        decide_order(subscription, position_quantity=0, signal_type="BUY", tick_price=24_000.0)
        is None
    )


@pytest.mark.parametrize(
    ("position", "signal", "expected"),
    [
        (0, "BUY", ("buy", 50)),  # Rs 5,000 of capital at Rs 100
        (50, "SELL", ("sell", 50)),
        (0, "SELL", None),  # equity is long-only: no short
        (50, "BUY", None),
    ],
)
def test_equity_is_still_long_only_and_sized_by_capital(position, signal, expected):
    assert (
        decide_order(
            _subscription("equity"),
            position_quantity=position,
            signal_type=signal,
            tick_price=100.0,
        )
        == expected
    )


def test_a_futures_subscription_trades_its_contract_and_an_equity_its_symbol():
    assert _subscription("future").trading_symbol == "NFO:NIFTY26OCTFUT"
    assert _subscription("equity").trading_symbol == "INFY"
    assert _subscription("future", contract_symbol=None).trading_symbol == "NIFTY"
