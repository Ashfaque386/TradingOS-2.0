from dataclasses import FrozenInstanceError

import pytest

from src.engine.options_grounding import (
    GroundedLeg,
    NakedOptionsScanResult,
    ground_option_legs,
    mock_option_chain,
    naked_options_scan,
)


def test_grounding_resolves_a_real_strike_on_the_chain():
    legs = [{"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50}]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    assert grounded[0].grounded is True
    assert grounded[0].ltp > 0


def test_grounding_flags_a_strike_not_on_the_chain():
    legs = [{"option_type": "CE", "strike": 999999, "action": "sell", "qty": 50}]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    assert grounded[0].grounded is False


def test_naked_sold_leg_with_no_hedge_is_blocked():
    legs = [{"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50}]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    result = naked_options_scan(grounded)
    assert result.blocked is True
    assert "naked" in result.reasons[0]


def test_hedged_call_spread_is_not_blocked():
    legs = [
        {"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50},
        {"option_type": "CE", "strike": 20300, "action": "buy", "qty": 50},
    ]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    result = naked_options_scan(grounded)
    assert result.blocked is False


def test_hedge_must_be_further_otm_not_just_any_bought_leg():
    # A bought call at a LOWER strike doesn't hedge a sold call -- it's a
    # bear spread's wrong half, still leaves the sold leg naked upside.
    legs = [
        {"option_type": "CE", "strike": 20300, "action": "sell", "qty": 50},
        {"option_type": "CE", "strike": 20100, "action": "buy", "qty": 50},
    ]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    result = naked_options_scan(grounded)
    assert result.blocked is True


def test_ungrounded_leg_blocks_acceptance_even_if_not_sold():
    legs = [{"option_type": "CE", "strike": 999999, "action": "buy", "qty": 50}]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    result = naked_options_scan(grounded)
    assert result.blocked is True


def test_hedged_put_spread_is_not_blocked():
    legs = [
        {"option_type": "PE", "strike": 19900, "action": "sell", "qty": 50},
        {"option_type": "PE", "strike": 19700, "action": "buy", "qty": 50},
    ]
    grounded = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")
    result = naked_options_scan(grounded)
    assert result.blocked is False


# ---- Found by the mutation gate (2026-10-14) -------------------------------------
# The scan loops used `continue` to skip a leg: turned into `break`, an ungrounded leg or a
# bought leg would silently end the scan and a naked sold leg after it would go unreported.
# The same for grounding, the strictness of "further out of the money", the exact messages the
# risk gate shows, and the shape of the stand-in chain.


def _leg(option_type, strike, action, grounded=True):
    return GroundedLeg(
        option_type=option_type,
        strike=strike,
        expiry="2026-09-25",
        action=action,
        qty=50,
        ltp=10.0 if grounded else 0.0,
        grounded=grounded,
    )


def test_an_ungrounded_leg_does_not_stop_the_scan_of_the_legs_after_it():
    legs = [_leg("CE", 999999, "sell", grounded=False), _leg("CE", 20100, "sell")]

    result = naked_options_scan(legs)

    assert result.blocked is True
    assert result.reasons == [
        "leg CE 999999 could not be grounded against the option chain",
        "sold CE 20100 has no same-underlying OTM hedge (naked)",
    ]


def test_a_bought_leg_does_not_stop_the_scan_of_the_legs_after_it():
    legs = [_leg("CE", 20500, "buy"), _leg("PE", 19900, "sell")]

    result = naked_options_scan(legs)

    assert result.reasons == ["sold PE 19900 has no same-underlying OTM hedge (naked)"]


@pytest.mark.parametrize("option_type", ["CE", "PE"])
def test_a_bought_leg_at_the_same_strike_is_not_a_hedge(option_type):
    legs = [_leg(option_type, 20100, "sell"), _leg(option_type, 20100, "buy")]

    result = naked_options_scan(legs)

    assert result.blocked is True  # "further out of the money" is strict


@pytest.mark.parametrize(("option_type", "hedge_strike"), [("CE", 20200), ("PE", 20000)])
def test_a_bought_leg_strictly_further_out_of_the_money_is_a_hedge(option_type, hedge_strike):
    legs = [_leg(option_type, 20100, "sell"), _leg(option_type, hedge_strike, "buy")]

    assert naked_options_scan(legs).blocked is False


@pytest.mark.parametrize(("option_type", "wrong_side_strike"), [("CE", 20000), ("PE", 20200)])
def test_a_bought_leg_on_the_wrong_side_is_not_a_hedge(option_type, wrong_side_strike):
    legs = [_leg(option_type, 20100, "sell"), _leg(option_type, wrong_side_strike, "buy")]

    assert naked_options_scan(legs).blocked is True


def test_a_hedge_must_be_the_same_option_type_and_actually_grounded():
    assert naked_options_scan([_leg("CE", 20100, "sell"), _leg("PE", 20300, "buy")]).blocked
    ungrounded_hedge = [_leg("CE", 20100, "sell"), _leg("CE", 20300, "buy", grounded=False)]
    assert naked_options_scan(ungrounded_hedge).blocked


def test_the_scan_reports_one_reason_per_naked_leg():
    legs = [_leg("CE", 20100, "sell"), _leg("PE", 19900, "sell")]

    assert naked_options_scan(legs).reasons == [
        "sold CE 20100 has no same-underlying OTM hedge (naked)",
        "sold PE 19900 has no same-underlying OTM hedge (naked)",
    ]


def test_an_ungrounded_leg_does_not_stop_the_grounding_of_the_legs_after_it():
    legs = [
        {"option_type": "CE", "strike": 999999, "action": "sell", "qty": 50},
        {"option_type": "PE", "strike": 19900, "action": "buy", "qty": 25},
    ]

    first, second = ground_option_legs(legs, underlying="NIFTY", expiry="2026-09-25")

    assert (first.grounded, first.ltp) == (False, 0.0)
    assert second.grounded is True and second.ltp > 0
    assert (second.option_type, second.strike, second.action, second.qty) == (
        "PE",
        19900,
        "buy",
        25,
    )


def test_grounding_reads_the_ce_price_for_calls_and_the_pe_price_for_puts():
    chain = {"strikes": {20100: {"ce_ltp": 11.0, "pe_ltp": 22.0}}}
    legs = [
        {"option_type": "CE", "strike": 20100, "action": "buy", "qty": 1},
        {"option_type": "pe", "strike": 20100, "action": "buy", "qty": 1},  # case-insensitive
    ]

    call, put = ground_option_legs(legs, underlying="NIFTY", expiry="e", chain=chain)

    assert (call.option_type, call.ltp) == ("CE", 11.0)
    assert (put.option_type, put.ltp) == ("PE", 22.0)


def test_the_stand_in_chain_has_the_documented_shape():
    chain = mock_option_chain("nifty", "2026-09-25")

    assert chain["underlying"] == "nifty" and chain["expiry"] == "2026-09-25"
    assert chain["atm_strike"] == 20000
    assert sorted(chain["strikes"]) == list(range(19000, 21100, 100))  # 21 strikes, +/-10 steps
    # Prices are the stand-in's fixed formula, to two decimals.
    assert chain["strikes"][20000] == {"ce_ltp": 50.0, "pe_ltp": 50.0}
    assert chain["strikes"][20500] == {"ce_ltp": 0.1, "pe_ltp": 100.0}
    assert chain["strikes"][19500] == {"ce_ltp": 100.0, "pe_ltp": 0.1}
    assert chain["strikes"][20100] == {"ce_ltp": 40.0, "pe_ltp": 60.0}


def test_any_other_underlying_gets_the_banknifty_style_grid():
    chain = mock_option_chain("BANKNIFTY", "2026-09-25")

    assert chain["atm_strike"] == 45000
    assert min(chain["strikes"]) == 44000 and max(chain["strikes"]) == 46000


def test_the_result_types_are_immutable_and_do_not_share_a_default_list():
    leg = _leg("CE", 20100, "sell")
    with pytest.raises(FrozenInstanceError):
        leg.qty = 1  # type: ignore[misc]
    result = NakedOptionsScanResult(blocked=False)
    with pytest.raises(FrozenInstanceError):
        result.blocked = True  # type: ignore[misc]
    assert result.reasons == []
    assert NakedOptionsScanResult(blocked=False).reasons is not result.reasons
