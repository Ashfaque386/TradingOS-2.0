"""Indian friction model tests (Build Spec §10): every cost component
verified against a hand calculation using the same published rates the
implementation documents, not just checked against itself.
"""

import pytest

from src.engine.backtest.friction import (
    FrictionModel,
    compute_leg_costs,
    compute_slippage_per_share,
)


def test_delivery_buy_hand_calculation():
    # turnover=100,000: brokerage free; STT 0.1%=100; exchange 0.00297%=2.97;
    # SEBI 0.0001%=0.1; stamp duty (buy) 0.015%=15; GST 18%*(0+2.97)=0.5346.
    costs = compute_leg_costs(turnover=100_000, is_buy=True, is_delivery=True)
    assert costs.brokerage == pytest.approx(0.0)
    assert costs.stt == pytest.approx(100.0)
    assert costs.exchange_txn_charges == pytest.approx(2.97)
    assert costs.sebi_fee == pytest.approx(0.1)
    assert costs.stamp_duty == pytest.approx(15.0)
    assert costs.gst == pytest.approx(0.5346)
    assert costs.total == pytest.approx(118.6046)


def test_delivery_sell_has_no_stamp_duty_but_same_stt():
    # STT is charged on both legs for delivery; stamp duty is buy-only.
    costs = compute_leg_costs(turnover=100_000, is_buy=False, is_delivery=True)
    assert costs.stt == pytest.approx(100.0)
    assert costs.stamp_duty == pytest.approx(0.0)


def test_intraday_sell_hand_calculation():
    # turnover=50,000: brokerage min(0.03%, cap 20)=15; STT (sell only)
    # 0.025%=12.5; exchange 0.00297%=1.485; SEBI 0.0001%=0.05; no stamp
    # duty (sell leg); GST 18%*(15+1.485)=2.9673.
    costs = compute_leg_costs(turnover=50_000, is_buy=False, is_delivery=False)
    assert costs.brokerage == pytest.approx(15.0)
    assert costs.stt == pytest.approx(12.5)
    assert costs.exchange_txn_charges == pytest.approx(1.485)
    assert costs.sebi_fee == pytest.approx(0.05)
    assert costs.stamp_duty == pytest.approx(0.0)
    assert costs.gst == pytest.approx(2.9673)
    assert costs.total == pytest.approx(32.0023)


def test_intraday_buy_has_no_stt_but_has_stamp_duty():
    costs = compute_leg_costs(turnover=50_000, is_buy=True, is_delivery=False)
    assert costs.stt == pytest.approx(0.0)
    assert costs.stamp_duty == pytest.approx(50_000 * 0.00003)


def test_intraday_brokerage_is_capped_at_twenty_rupees():
    # turnover=100,000: uncapped 0.03% would be 30, but the cap is ₹20.
    costs = compute_leg_costs(turnover=100_000, is_buy=True, is_delivery=False)
    assert costs.brokerage == pytest.approx(20.0)


def test_small_intraday_trade_brokerage_is_uncapped_percentage():
    # turnover=10,000: 0.03% = 3, well under the ₹20 cap.
    costs = compute_leg_costs(turnover=10_000, is_buy=True, is_delivery=False)
    assert costs.brokerage == pytest.approx(3.0)


def test_gst_applies_only_to_brokerage_and_exchange_charges():
    model = FrictionModel(
        stt_delivery_pct=0.01, sebi_fee_pct=0.01, stamp_duty_delivery_buy_pct=0.01
    )
    costs = compute_leg_costs(turnover=100_000, is_buy=True, is_delivery=True, model=model)
    # STT/SEBI/stamp duty inflated to 1000 each above -- GST must still
    # only apply to brokerage (0, delivery is free) + exchange charges.
    assert costs.gst == pytest.approx(0.18 * costs.exchange_txn_charges)


def test_slippage_hand_calculation():
    # atr_component = 0.1 * 2.0 = 0.2
    # volume_component = 0.1 * (100/10000) * 500 = 0.5
    # total = 0.7
    slippage = compute_slippage_per_share(atr=2.0, quantity=100, avg_daily_volume=10_000, price=500)
    assert slippage == pytest.approx(0.7)


def test_slippage_is_zero_with_zero_atr_and_zero_volume_impact():
    model = FrictionModel(atr_slippage_multiplier=0.0, volume_impact_coefficient=0.0)
    slippage = compute_slippage_per_share(
        atr=5.0, quantity=100, avg_daily_volume=10_000, price=500, model=model
    )
    assert slippage == pytest.approx(0.0)


def test_slippage_handles_zero_avg_volume_without_dividing_by_zero():
    slippage = compute_slippage_per_share(atr=2.0, quantity=100, avg_daily_volume=0, price=500)
    assert slippage == pytest.approx(0.2)  # only the ATR component
