"""Compliance Checker tests (Build Spec §8): SEBI-style position limits and
an NSE-style circuit-filter band, both hand-calculable.
"""

import pytest

from src.engine.risk.compliance import (
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
