from src.engine.options_grounding import ground_option_legs, naked_options_scan


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
