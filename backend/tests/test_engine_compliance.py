"""Compliance Checker tests (Build Spec §8): SEBI-style position limits and
an NSE-style circuit-filter band, both hand-calculable.
"""

import pytest

from src.engine.risk.compliance import (
    DEFAULT_CIRCUIT_BAND_PCT,
    DEFAULT_POSITION_LIMIT_PCT,
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
