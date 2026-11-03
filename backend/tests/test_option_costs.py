"""What an option trade costs and what it needs (phase 5).

Rates are Zerodha's published equity-options schedule (read 2026-10-17): brokerage a flat Rs 20 per
executed order, STT 0.15% of the premium on the sell side, NSE charge 0.03553% of the premium, SEBI
Rs 10 per crore, stamp duty 0.003% on the buy side, GST 18% on brokerage + SEBI + exchange charges.
Everything but brokerage is on the PREMIUM traded (premium x quantity), not the strike value. Rates
move: the arithmetic is pinned here and `PAPER_FRICTION_OVERRIDES` is how a new rate goes in.
"""

import pytest

from src.core.config import get_settings
from src.engine.backtest.friction import FrictionModel, compute_option_leg_costs
from src.engine.paper_trading.margin import check_max_loss_margin
from src.engine.paper_trading.order_book import FillResult
from src.orchestration.paper_trading import fill_costs

# ---- the schedule, worked by hand on Rs 1 lakh of premium ----


def test_a_buy_leg_pays_the_flat_brokerage_exchange_charge_stamp_duty_and_no_stt():
    costs = compute_option_leg_costs(turnover=100_000, is_buy=True)

    assert costs.brokerage == 20.0
    assert costs.stt == 0.0
    assert costs.exchange_txn_charges == pytest.approx(35.5301)  # 0.03553% + IPFT Rs 0.01 a crore
    assert costs.sebi_fee == pytest.approx(0.1)  # Rs 10 a crore
    assert costs.stamp_duty == pytest.approx(3.0)  # 0.003%
    assert costs.gst == pytest.approx(0.18 * (20.0 + 0.1 + 35.5301))
    assert costs.total == pytest.approx(68.643518)


def test_a_sell_leg_pays_stt_and_no_stamp_duty():
    costs = compute_option_leg_costs(turnover=100_000, is_buy=False)

    assert costs.stt == pytest.approx(150.0)  # 0.15% of the premium
    assert costs.stamp_duty == 0.0
    assert costs.total == pytest.approx(215.643518)


def test_brokerage_is_flat_whatever_the_size_of_the_trade():
    small = compute_option_leg_costs(turnover=500, is_buy=True)
    large = compute_option_leg_costs(turnover=5_000_000, is_buy=True)

    assert small.brokerage == large.brokerage == 20.0


def test_everything_else_scales_with_the_premium():
    one = compute_option_leg_costs(turnover=100_000, is_buy=False)
    two = compute_option_leg_costs(turnover=200_000, is_buy=False)

    assert two.stt == pytest.approx(2 * one.stt)
    assert two.exchange_txn_charges == pytest.approx(2 * one.exchange_txn_charges)


def test_gst_includes_the_sebi_fee_and_the_flat_brokerage():
    costs = compute_option_leg_costs(turnover=1_000_000, is_buy=True)

    assert costs.gst == pytest.approx(
        0.18 * (costs.brokerage + costs.sebi_fee + costs.exchange_txn_charges)
    )


def test_a_rate_can_be_replaced_without_touching_code():
    model = FrictionModel(option_stt_sell_pct=0.001, option_brokerage_flat=0.0)

    costs = compute_option_leg_costs(turnover=100_000, is_buy=False, model=model)

    assert costs.stt == pytest.approx(100.0)
    assert costs.brokerage == 0.0


# ---- through fill_costs ----


@pytest.fixture
def settings_reset():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_fill_on_an_option_leg_is_charged_the_option_schedule_on_its_premium(
    monkeypatch, settings_reset
):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    fill = FillResult(650, 650, 0, 153.846153846, True)  # premium traded: Rs 1 lakh

    total, breakdown = fill_costs("sell", fill, instrument_kind="option")

    assert total == pytest.approx(215.6434, rel=1e-6)
    assert (breakdown["segment"], breakdown["product"]) == ("option", "nrml")
    assert breakdown["turnover"] == pytest.approx(100_000, rel=1e-6)  # the premium
    assert breakdown["stt"] == pytest.approx(150.0, rel=1e-6)


def test_an_option_override_from_the_environment_reaches_the_charge(monkeypatch, settings_reset):
    monkeypatch.setenv("PAPER_FRICTION_OVERRIDES", '{"option_brokerage_flat": 0}')
    get_settings.cache_clear()

    _, breakdown = fill_costs("buy", FillResult(100, 100, 0, 100.0, True), instrument_kind="option")

    assert breakdown["brokerage"] == 0.0


def test_the_other_segments_are_charged_as_before(monkeypatch, settings_reset):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    monkeypatch.setenv("PAPER_TRADING_PRODUCT", "cnc")
    get_settings.cache_clear()
    fill = FillResult(65, 65, 0, 15_384.615384615385, True)

    assert fill_costs("sell", fill, instrument_kind="future")[1]["segment"] == "future"
    assert fill_costs("buy", fill, instrument_kind="equity")[1]["segment"] == "equity"


# ---- what a defined-risk position commits ----


def test_a_position_that_could_lose_less_than_the_allowed_share_of_capital_fits():
    check = check_max_loss_margin(max_loss=30_000, capital=100_000, max_utilisation_pct=50)

    assert check.ok and check.reason is None
    assert (check.required, check.limit) == (30_000, 50_000)


def test_a_position_that_could_lose_more_is_refused_and_the_reason_says_how_much():
    check = check_max_loss_margin(max_loss=62_500, capital=100_000, max_utilisation_pct=50)

    assert not check.ok
    assert "62,500" in check.reason and "50,000" in check.reason
    assert "largest possible" in check.reason


def test_a_position_that_could_lose_exactly_the_limit_fits():
    assert check_max_loss_margin(max_loss=50_000, capital=100_000, max_utilisation_pct=50).ok


def test_a_structure_that_cannot_lose_anything_fits():
    assert check_max_loss_margin(max_loss=-1_000, capital=100_000, max_utilisation_pct=50).ok
