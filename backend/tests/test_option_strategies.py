"""Which option legs a signal calls for, and when a held structure is stopped (phase 5).

Defined risk only: a long option, or a debit spread whose sold leg is covered by the bought one.
Expiries and strikes are whatever the broker lists -- these tests use awkward ones on purpose
(uneven strike steps, weekly expiries on odd days) so nothing assumes a weekday or a step.
"""

from datetime import date

import pytest

from src.brokers.base import OptionContract, option_symbol
from src.engine.paper_trading.option_strategies import (
    LegPlan,
    direction_for_signal,
    held_direction,
    max_loss,
    nearest_strike,
    pick_expiry,
    plan_entry,
    stop_triggered,
    structure_value,
)

TODAY = date(2026, 10, 19)
NEAR = date(2026, 10, 20)  # 1 day away: too near to enter
WEEKLY = date(2026, 10, 22)  # 3 days away: the soonest that qualifies
MONTHLY = date(2026, 10, 27)


def _contract(expiry, strike, kind, lot=65):
    return OptionContract(
        option_symbol("NIFTY", expiry, strike, kind), "NIFTY", expiry, strike, kind, lot, 5.0
    )


def _chain(expiry, strikes, lot=65):
    return [_contract(expiry, s, kind, lot) for s in strikes for kind in ("CE", "PE")]


STRIKES = [24300.0, 24400.0, 24500.0, 24550.0, 24600.0, 24700.0, 24800.0]  # uneven on purpose
CHAIN = _chain(NEAR, STRIKES) + _chain(WEEKLY, STRIKES) + _chain(MONTHLY, STRIKES)


def _plan(strategy="long_option", signal="BUY", spot=24510.0, **overrides):
    values = {
        "contracts": CHAIN,
        "today": TODAY,
        "min_days": 3,
        "lots": 1,
        "width": 2,
    } | overrides
    return plan_entry(strategy, signal, spot=spot, **values)


# ---- the expiry ----


def test_the_soonest_expiry_with_enough_days_left_is_chosen():
    assert pick_expiry(CHAIN, today=TODAY, min_days=3) == WEEKLY


@pytest.mark.parametrize(
    ("min_days", "expected"),
    [(1, NEAR), (2, WEEKLY), (3, WEEKLY), (4, MONTHLY), (8, MONTHLY), (9, None)],
)
def test_the_minimum_days_decide_which_expiry_qualifies(min_days, expected):
    assert pick_expiry(CHAIN, today=TODAY, min_days=min_days) == expected


def test_an_expiry_with_exactly_the_minimum_days_left_qualifies():
    assert pick_expiry(_chain(WEEKLY, STRIKES), today=date(2026, 10, 19), min_days=3) == WEEKLY
    assert pick_expiry(_chain(WEEKLY, STRIKES), today=date(2026, 10, 20), min_days=3) is None


def test_no_contracts_means_no_expiry():
    assert pick_expiry([], today=TODAY, min_days=3) is None


# ---- the at-the-money strike ----


@pytest.mark.parametrize(
    ("spot", "expected"),
    [
        (24510.0, 24500.0),
        (24530.0, 24550.0),
        (24525.0, 24500.0),  # equally close to 24500 and 24550: the lower one
        (24000.0, 24300.0),  # below the chain
        (26000.0, 24800.0),  # above it
    ],
)
def test_the_listed_strike_nearest_the_spot_is_at_the_money(spot, expected):
    assert nearest_strike(STRIKES, spot) == expected


def test_no_strikes_means_no_at_the_money():
    assert nearest_strike([], 24500.0) is None


# ---- long option ----


def test_a_buy_signal_buys_the_at_the_money_call():
    plan = _plan(signal="BUY")

    [leg] = plan.legs
    assert plan.direction == "bullish" and plan.expiry == WEEKLY
    assert (leg.contract.option_type, leg.contract.strike, leg.side) == ("CE", 24500.0, "buy")
    assert leg.quantity == 65  # one lot of the contract's own size


def test_a_sell_signal_buys_the_at_the_money_put():
    plan = _plan(signal="SELL")

    [leg] = plan.legs
    assert plan.direction == "bearish"
    assert (leg.contract.option_type, leg.contract.strike, leg.side) == ("PE", 24500.0, "buy")


def test_lots_multiply_the_contracts_own_lot_size():
    plan = _plan(lots=3, contracts=_chain(MONTHLY, STRIKES, lot=75))

    assert plan.legs[0].quantity == 225


def test_the_legs_are_in_the_chosen_expiry_only():
    assert {leg.contract.expiry for leg in _plan(strategy="vertical_spread").legs} == {WEEKLY}


# ---- vertical spread: a credit spread, the shape the naked-options scan accepts ----


def test_a_buy_signal_makes_a_bull_put_spread_sold_at_the_money_and_hedged_further_down():
    plan = _plan(strategy="vertical_spread", signal="BUY")

    hedge, sold = plan.legs
    assert plan.direction == "bullish"
    assert (sold.side, sold.contract.strike, sold.contract.option_type) == ("sell", 24500.0, "PE")
    # Two listed strikes down from 24500 is 24300 (24400, then 24300): by position, not by price.
    assert (hedge.side, hedge.contract.strike, hedge.contract.option_type) == ("buy", 24300.0, "PE")
    assert hedge.quantity == sold.quantity == 65


def test_a_sell_signal_makes_a_bear_call_spread_sold_at_the_money_and_hedged_further_up():
    plan = _plan(strategy="vertical_spread", signal="SELL")

    hedge, sold = plan.legs
    assert plan.direction == "bearish"
    assert (sold.side, sold.contract.strike, sold.contract.option_type) == ("sell", 24500.0, "CE")
    assert (hedge.side, hedge.contract.strike, hedge.contract.option_type) == ("buy", 24600.0, "CE")


def test_the_hedge_is_always_first_so_a_sold_leg_is_never_opened_before_it():
    for signal in ("BUY", "SELL"):
        assert [leg.side for leg in _plan(strategy="vertical_spread", signal=signal).legs] == [
            "buy",
            "sell",
        ]


def test_the_hedge_is_always_further_out_of_the_money_than_the_sold_leg():
    """The one thing the naked-options scan asks of a sold leg."""
    for signal in ("BUY", "SELL"):
        plan = _plan(strategy="vertical_spread", signal=signal)
        hedge, sold = plan.legs
        if sold.contract.option_type == "CE":
            assert hedge.contract.strike > sold.contract.strike
        else:
            assert hedge.contract.strike < sold.contract.strike


@pytest.mark.parametrize("width", [1, 2])  # 24500 is index 2: a put hedge cannot go 3 down
def test_the_width_is_counted_in_listed_strikes(width):
    atm = STRIKES.index(24500.0)

    puts = _plan(strategy="vertical_spread", signal="BUY", width=width)
    calls = _plan(strategy="vertical_spread", signal="SELL", width=width)

    assert puts.legs[0].contract.strike == STRIKES[atm - width]
    assert calls.legs[0].contract.strike == STRIKES[atm + width]


def test_a_spread_whose_hedge_strike_is_not_listed_is_not_planned():
    # 24500 is index 2 of 7: puts need index 2 - width, calls 2 + width.
    assert _plan(strategy="vertical_spread", signal="BUY", width=3) is None
    assert _plan(strategy="vertical_spread", signal="SELL", width=5) is None
    assert _plan(strategy="vertical_spread", signal="BUY", width=2) is not None
    assert _plan(strategy="vertical_spread", signal="SELL", width=4) is not None


@pytest.mark.parametrize("width", [0, -1])
def test_a_spread_needs_a_positive_width(width):
    assert _plan(strategy="vertical_spread", width=width) is None


def test_a_spread_whose_legs_would_not_cover_each_other_is_not_planned():
    contracts = [_contract(WEEKLY, 24500.0, "CE", lot=65), _contract(WEEKLY, 24600.0, "CE", lot=75)]

    assert _plan(strategy="vertical_spread", signal="SELL", contracts=contracts, width=1) is None


def test_the_long_option_strategy_never_sells_anything():
    for signal in ("BUY", "SELL"):
        assert all(leg.side == "buy" for leg in _plan(signal=signal).legs)


# ---- no plan ----


def test_with_no_expiry_far_enough_out_there_is_no_plan():
    assert _plan(contracts=_chain(NEAR, STRIKES)) is None


def test_with_no_contracts_of_the_wanted_type_there_is_no_plan():
    calls_only = [c for c in CHAIN if c.option_type == "CE"]

    assert _plan(signal="BUY", contracts=calls_only) is not None
    assert _plan(signal="SELL", contracts=calls_only) is None


def test_no_lots_means_no_plan():
    assert _plan(lots=0) is None


def test_an_unknown_strategy_is_an_error():
    with pytest.raises(ValueError, match="naked_straddle"):
        _plan(strategy="naked_straddle")


# ---- reading a position ----


@pytest.mark.parametrize(
    ("signal", "direction"), [("BUY", "bullish"), ("SELL", "bearish"), ("anything else", "bearish")]
)
def test_a_signal_is_a_view(signal, direction):
    assert direction_for_signal(signal) == direction


def test_a_credit_spreads_view_is_its_sold_leg_and_a_bought_options_is_itself():
    call = option_symbol("NIFTY", MONTHLY, 24500.0, "CE")
    hedge_call = option_symbol("NIFTY", MONTHLY, 24600.0, "CE")
    put = option_symbol("NIFTY", MONTHLY, 24500.0, "PE")
    hedge_put = option_symbol("NIFTY", MONTHLY, 24300.0, "PE")

    assert held_direction([(call, 65)]) == "bullish"  # a bought call
    assert held_direction([(put, 65)]) == "bearish"  # a bought put
    assert held_direction([(put, -65), (hedge_put, 65)]) == "bullish"  # a bull put spread
    assert held_direction([(hedge_put, 65), (put, -65)]) == "bullish"  # order does not matter
    assert held_direction([(call, -65), (hedge_call, 65)]) == "bearish"  # a bear call spread


def test_with_no_option_legs_there_is_no_view():
    assert held_direction([]) is None
    assert held_direction([("INFY", 10)]) is None  # not an option
    flat = option_symbol("NIFTY", MONTHLY, 24500.0, "CE")
    assert held_direction([(flat, 0)]) is None  # a closed leg


def test_the_most_a_bought_option_can_lose_is_what_it_cost():
    [leg] = _plan(signal="BUY").legs

    assert max_loss([leg], net_debit=6_565.0) == 6_565.0


def test_the_most_a_credit_spread_can_lose_is_its_width_less_the_credit():
    plan = _plan(strategy="vertical_spread", signal="BUY")  # sold 24500 PE, bought 24300 PE
    credit = 65 * 59.0 - 65 * 20.0  # sold at 59, bought at 20: a net credit
    width_in_rupees = (24500.0 - 24300.0) * 65

    assert max_loss(plan.legs, net_debit=-credit) == pytest.approx(width_in_rupees - credit)
    assert max_loss(plan.legs, net_debit=-credit) == pytest.approx(10_465.0)


def test_a_partly_hedged_quantity_counts_only_what_is_covered():
    plan = _plan(strategy="vertical_spread", signal="SELL", lots=2)
    hedge, sold = plan.legs
    thin = [LegPlan(hedge.contract, "buy", 65), LegPlan(sold.contract, "sell", 130)]

    assert max_loss(thin, net_debit=0.0) == pytest.approx(100.0 * 65)  # the covered 65 only


# ---- valuing and stopping ----


def test_a_structure_is_valued_leg_by_leg_with_signs():
    # Bought 65 at 120, sold 65 at 70: a debit of 65 * 50.
    assert structure_value([(65, 120.0), (-65, 70.0)]) == pytest.approx(3250.0)
    assert structure_value([]) == 0.0


@pytest.mark.parametrize(
    ("loss", "stopped"),
    [
        (0.0, False),  # worth what it cost
        (4_999.0, False),  # lost 49.99% of what is at risk
        (5_000.0, True),  # lost exactly 50%
        (8_000.0, True),
        (10_000.0, True),
        (-300.0, False),  # a gain
    ],
)
def test_a_structure_is_stopped_once_it_has_lost_the_stop_share_of_what_it_can_lose(loss, stopped):
    assert stop_triggered(risk=10_000.0, loss=loss, stop_pct=50.0) is stopped


def test_a_structure_with_nothing_at_risk_is_never_stopped():
    assert stop_triggered(risk=0.0, loss=500.0, stop_pct=50.0) is False
    assert stop_triggered(risk=-200.0, loss=900.0, stop_pct=50.0) is False


def test_the_stop_share_is_respected():
    assert stop_triggered(risk=10_000.0, loss=3_000.0, stop_pct=30.0) is True
    assert stop_triggered(risk=10_000.0, loss=2_900.0, stop_pct=30.0) is False
