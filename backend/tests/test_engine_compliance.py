"""Compliance Checker tests (Build Spec §8): a house single-position limit and a circuit-filter
band (the exchange's own limits when the quote carried them, the reference table's otherwise),
both hand-calculable.
"""

import pytest

from src.engine.risk.compliance import (
    DEFAULT_CIRCUIT_BAND_PCT,
    DEFAULT_POSITION_LIMIT_PCT,
    ExchangeCircuitLimits,
    ReferenceTableRegulatoryDataProvider,
    check_circuit_filter_band,
    check_position_limit,
    run_compliance_checks,
)


def test_position_within_limit_is_not_blocked():
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)
    result = check_position_limit(
        symbol="RELIANCE",
        proposed_position_value=50_000,
        portfolio_value=1_000_000,
        provider=provider,
    )
    assert result.blocked is False


def test_position_exceeding_limit_is_blocked_with_a_reason():
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)
    result = check_position_limit(
        symbol="RELIANCE",
        proposed_position_value=150_000,
        portfolio_value=1_000_000,
        provider=provider,
    )
    assert result.blocked is True
    assert "RELIANCE" in result.reasons[0]


def test_position_limit_raises_on_invalid_portfolio_value():
    provider = ReferenceTableRegulatoryDataProvider()
    with pytest.raises(ValueError):
        check_position_limit(
            symbol="RELIANCE", proposed_position_value=1_000, portfolio_value=0, provider=provider
        )


def test_price_move_within_circuit_band_is_not_blocked():
    provider = ReferenceTableRegulatoryDataProvider()
    result = check_circuit_filter_band(
        symbol="RELIANCE", proposed_price=2_050, reference_price=2_000, provider=provider
    )
    assert result.blocked is False


def test_price_move_at_circuit_band_is_blocked():
    provider = ReferenceTableRegulatoryDataProvider()  # NIFTY has a 10% seeded band
    result = check_circuit_filter_band(
        symbol="NIFTY", proposed_price=22_000, reference_price=20_000, provider=provider
    )
    assert result.blocked is True


def test_run_compliance_checks_combines_both_rules():
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)
    result = run_compliance_checks(
        symbol="RELIANCE",
        proposed_position_value=150_000,
        portfolio_value=1_000_000,
        proposed_price=2_100,
        reference_price=2_000,
        provider=provider,
    )
    assert result.blocked is True
    assert len(result.reasons) == 1  # only the position limit breached, not the circuit band


# Build Spec §21-22 hardening pass: mutation testing (a real mutmut run,
# not hypothesized) found the gaps below -- every test above either passes
# limits/bands explicitly or stays well clear of any boundary, so none of
# them noticed when mutmut silently changed a default constant, a
# boundary comparison, or the 100.0 scaling factor and the suite stayed
# green either way.


def test_default_constants_are_actually_used_when_not_overridden():
    """Every existing test above passes an explicit limit/band via the
    provider's constructor kwargs -- nothing exercised
    DEFAULT_POSITION_LIMIT_PCT/DEFAULT_CIRCUIT_BAND_PCT themselves, so a
    real mutmut run silently changed them (10.0 -> 11.0, 10.0 -> None,
    20.0 -> 21.0) with the full test suite still green either way."""
    assert DEFAULT_POSITION_LIMIT_PCT == 10.0
    assert DEFAULT_CIRCUIT_BAND_PCT == 20.0
    provider = ReferenceTableRegulatoryDataProvider()
    assert provider.position_limit_pct("RELIANCE") == 10.0  # unseeded symbol -> default
    assert provider.circuit_filter_band_pct("RELIANCE") == 20.0


def test_portfolio_value_of_exactly_one_still_computes_a_real_result():
    """The "invalid portfolio" guard is portfolio_value <= 0, not <= 1 -- a
    real mutmut run changed the boundary to <= 1 and nothing caught it,
    since every existing test used portfolio_value either 0 or >= 1_000_000."""
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)
    result = check_position_limit(
        symbol="RELIANCE", proposed_position_value=0.05, portfolio_value=1, provider=provider
    )
    assert result.blocked is False


def test_position_exactly_at_the_limit_boundary_is_not_blocked():
    """The check is a strict `>` (proposed_pct > limit_pct), not `>=`, and
    proposed_pct is precisely `* 100.0`, not a drifted `* 101.0`. A real
    mutmut run flipped both independently and nothing caught either,
    because no existing test landed exactly on the limit. 100_000 /
    1_000_000 * 100.0 == 10.0 exactly equals the 10.0% default limit, so
    this single input is wrongly blocked under either mutant."""
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)
    result = check_position_limit(
        symbol="RELIANCE",
        proposed_position_value=100_000,
        portfolio_value=1_000_000,
        provider=provider,
    )
    assert result.blocked is False


def test_circuit_band_raises_on_invalid_reference_price():
    """The existing tests only ever call check_circuit_filter_band with a
    valid reference_price -- a real mutmut run changed the `<= 0` guard to
    `< 0`, which would silently let reference_price=0 through into a
    division by zero, and nothing caught it."""
    provider = ReferenceTableRegulatoryDataProvider()
    with pytest.raises(ValueError):
        check_circuit_filter_band(
            symbol="RELIANCE", proposed_price=100, reference_price=0, provider=provider
        )


def test_reference_price_of_exactly_one_still_computes_a_real_result():
    """The "invalid reference price" guard is reference_price <= 0, not
    <= 1 -- a real mutmut run changed the boundary to <= 1 and nothing
    caught it."""
    provider = ReferenceTableRegulatoryDataProvider()
    result = check_circuit_filter_band(
        symbol="RELIANCE", proposed_price=1.05, reference_price=1, provider=provider
    )
    assert result.blocked is False


def test_circuit_band_move_just_under_the_limit_is_not_blocked():
    """move_pct is precisely `* 100.0`, not a drifted `* 101.0` -- a real
    mutmut run flipped it and every existing test passed anyway, since the
    only circuit-band test at the boundary (NIFTY's exact 10%) stays
    blocked either way. 9.95% is far enough under NIFTY's 10% seeded band
    that only the `* 101.0` mutant (10.05%) would wrongly block it."""
    provider = ReferenceTableRegulatoryDataProvider()  # NIFTY has a 10% seeded band
    result = check_circuit_filter_band(
        symbol="NIFTY", proposed_price=21_990, reference_price=20_000, provider=provider
    )
    assert result.blocked is False


def test_the_index_symbols_keep_their_own_circuit_band_while_others_get_the_default():
    """Found by mutation testing (2026-10-13): the table's NIFTY/BANKNIFTY bands could
    be changed (10.0 -> 11.0) or a key renamed with every test still green, because
    only the unlisted-symbol default was ever asserted. These are regulatory numbers."""
    provider = ReferenceTableRegulatoryDataProvider()

    assert provider.circuit_filter_band_pct("NIFTY") == 10.0
    assert provider.circuit_filter_band_pct("BANKNIFTY") == 10.0
    assert provider.circuit_filter_band_pct("banknifty") == 10.0  # symbols are case-insensitive
    assert provider.circuit_filter_band_pct("RELIANCE") == DEFAULT_CIRCUIT_BAND_PCT


# ---- the exchange's own circuit limits, and what the reasons say ----

LIMITS = ExchangeCircuitLimits(lower=90.0, upper=110.0)


def band(price, *, reference=100.0, limits=LIMITS, symbol="RELIANCE"):
    return check_circuit_filter_band(
        symbol=symbol,
        proposed_price=price,
        reference_price=reference,
        provider=ReferenceTableRegulatoryDataProvider(),
        exchange_limits=limits,
    )


def test_a_price_inside_the_exchanges_limits_is_not_blocked():
    for price in (90.01, 100.0, 109.99):
        assert band(price).blocked is False, price


def test_a_price_at_or_above_the_upper_limit_is_blocked_and_the_reason_names_the_exchange():
    for price in (110.0, 110.01, 150.0):
        result = band(price)
        assert result.blocked is True, price
        assert result.reasons == [
            f"RELIANCE: proposed price {price:.2f} is at/above the exchange's upper circuit "
            "limit 110.00 (from the broker's quote)"
        ]


def test_a_price_at_or_below_the_lower_limit_is_blocked_and_the_reason_names_the_exchange():
    for price in (90.0, 89.99, 50.0):
        result = band(price)
        assert result.blocked is True, price
        assert result.reasons == [
            f"RELIANCE: proposed price {price:.2f} is at/below the exchange's lower circuit "
            "limit 90.00 (from the broker's quote)"
        ]


def test_the_exchanges_limits_replace_the_reference_tables_band_not_add_to_it():
    # 8% from the reference, well inside a 10% band and inside the exchange's limits: fine.
    assert band(108.0, symbol="NIFTY").blocked is False
    # 40% from the reference is far beyond the table's 20% default band, but the exchange itself
    # allows it (a stock re-listed with a wide band): the exchange's word is the one that counts.
    wide = ExchangeCircuitLimits(lower=50.0, upper=150.0)
    assert band(140.0, limits=wide).blocked is False
    # And the other way: inside the table's band, beyond the exchange's tighter one.
    assert band(105.0, limits=ExchangeCircuitLimits(lower=96.0, upper=104.0)).blocked is True


def test_a_reference_price_that_is_not_positive_is_still_a_caller_bug_with_limits():
    with pytest.raises(ValueError, match="reference_price must be positive"):
        band(100.0, reference=0.0)
    with pytest.raises(ValueError, match="reference_price must be positive"):
        band(100.0, reference=-1.0)


def test_without_exchange_limits_the_reason_says_the_reference_table_decided():
    provider = ReferenceTableRegulatoryDataProvider()  # a 20% default band

    result = check_circuit_filter_band(
        symbol="RELIANCE", proposed_price=125.0, reference_price=100.0, provider=provider
    )

    assert result.blocked is True
    assert result.reasons == [
        "RELIANCE: proposed price move 25.00% from the reference price is at/beyond the "
        "reference table's circuit band 20.00% (the quote carried no exchange circuit limits)"
    ]


def test_the_position_limit_reason_says_it_is_a_house_rule_not_an_exchange_or_sebi_limit():
    provider = ReferenceTableRegulatoryDataProvider(default_position_limit_pct=10.0)

    result = check_position_limit(
        symbol="RELIANCE",
        proposed_position_value=150_000,
        portfolio_value=1_000_000,
        provider=provider,
    )

    assert result.reasons == [
        "RELIANCE: proposed position 15.00% of portfolio exceeds the house single-position "
        "limit 10.00% (a concentration rule of this system, not an exchange or SEBI limit)"
    ]


def compliance(**overrides):
    inputs = dict(
        symbol="RELIANCE",
        proposed_position_value=50_000,
        portfolio_value=1_000_000,
        proposed_price=100.0,
        reference_price=100.0,
        provider=ReferenceTableRegulatoryDataProvider(),
    )
    inputs.update(overrides)
    return run_compliance_checks(**inputs)


def test_the_combined_check_hands_the_exchange_limits_to_the_band():
    assert compliance(proposed_price=112.0).blocked is False  # 12% from the reference, no limits
    result = compliance(proposed_price=112.0, exchange_limits=LIMITS)
    assert result.blocked is True
    assert "upper circuit limit 110.00" in result.reasons[0]


def test_a_stop_loss_exit_can_leave_the_circuit_band_out_and_only_that():
    gapped = dict(proposed_price=70.0)  # 30% below the reference: beyond the 20% table band

    assert compliance(**gapped).blocked is True
    assert compliance(**gapped, check_circuit_band=False).blocked is False
    # Exchange limits do not bring it back: the band is not being checked at all.
    assert compliance(**gapped, exchange_limits=LIMITS, check_circuit_band=False).blocked is False
    # The position limit still applies to the same order.
    too_big = compliance(**gapped, proposed_position_value=150_000, check_circuit_band=False)
    assert too_big.blocked is True
    assert len(too_big.reasons) == 1 and "house single-position limit" in too_big.reasons[0]


def test_the_circuit_band_is_checked_unless_asked_not_to():
    result = compliance(proposed_price=70.0, proposed_position_value=150_000)

    assert result.blocked is True
    assert len(result.reasons) == 2  # the position limit and the band


def test_exchange_limits_are_a_plain_value_that_cannot_be_changed():
    from dataclasses import FrozenInstanceError

    assert (LIMITS.lower, LIMITS.upper) == (90.0, 110.0)
    with pytest.raises(FrozenInstanceError):
        LIMITS.lower = 1.0  # type: ignore[misc]
    assert not hasattr(LIMITS, "__dict__")
