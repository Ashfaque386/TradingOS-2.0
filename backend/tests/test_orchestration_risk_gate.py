"""Order-Intent Risk Gate tests (Build Spec §8): the acceptance-critical
one is that tripping the kill switch blocks intent *creation* -- not just
some later submission step -- and that live/paper are genuinely
independent. Also proves the naked-options scan and correlation
constraint are wired into this per-tick gate, not only pre-deployment.
"""

import pandas as pd
import pytest

from src.engine.options_grounding import ground_option_legs
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.correlation_constraint import make_fake_nifty_benchmark
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.orchestration.kill_switch import check_drawdown
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

_PROVIDER = ReferenceTableRegulatoryDataProvider()


def _kwargs(**overrides):
    base = dict(
        mode="paper",
        symbol="RELIANCE",
        side="buy",
        quantity=10,
        proposed_price=2_000,
        reference_price=2_000,
        proposed_position_value=20_000,
        portfolio_value=1_000_000,
        regulatory_provider=_PROVIDER,
    )
    base.update(overrides)
    return base


async def test_intent_is_created_when_every_check_passes(db_session_factory):
    async with db_session_factory() as db:
        intent = await create_order_intent(db, **_kwargs())

    assert intent.symbol == "RELIANCE"
    assert intent.compliance.blocked is False


async def test_tripped_kill_switch_blocks_intent_creation_before_anything_else(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        with pytest.raises(KillSwitchTrippedError):
            # A position limit breach is also present here (150k on a 1M
            # portfolio > the 10% default limit) -- if the kill switch
            # weren't checked first, this would raise
            # OrderIntentRejectedError instead, proving check order.
            await create_order_intent(db, **_kwargs(mode="live", proposed_position_value=150_000))


async def test_paper_mode_is_unaffected_by_a_tripped_live_switch(db_session_factory):
    async with db_session_factory() as db:
        await check_drawdown(db, "live", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        intent = await create_order_intent(db, **_kwargs(mode="paper"))

    assert intent.mode == "paper"


async def test_compliance_breach_rejects_the_intent(db_session_factory):
    async with db_session_factory() as db:
        with pytest.raises(OrderIntentRejectedError) as exc_info:
            await create_order_intent(db, **_kwargs(proposed_position_value=150_000))

    assert any("position" in reason for reason in exc_info.value.reasons)


async def test_naked_option_legs_reject_the_intent(db_session_factory):
    legs = ground_option_legs(
        [{"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50}],
        underlying="NIFTY",
        expiry="2026-09-25",
    )

    async with db_session_factory() as db:
        with pytest.raises(OrderIntentRejectedError) as exc_info:
            await create_order_intent(db, **_kwargs(option_legs=legs))

    assert any("naked" in reason for reason in exc_info.value.reasons)


async def test_hedged_option_legs_do_not_reject_the_intent(db_session_factory):
    legs = ground_option_legs(
        [
            {"option_type": "CE", "strike": 20100, "action": "sell", "qty": 50},
            {"option_type": "CE", "strike": 20300, "action": "buy", "qty": 50},
        ],
        underlying="NIFTY",
        expiry="2026-09-25",
    )

    async with db_session_factory() as db:
        intent = await create_order_intent(db, **_kwargs(option_legs=legs))

    assert intent.symbol == "RELIANCE"


async def test_per_tick_correlation_breach_rejects_the_intent(db_session_factory):
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=0)
    correlated_returns = bench.returns * 0.95 + 0.0001

    async with db_session_factory() as db:
        with pytest.raises(OrderIntentRejectedError) as exc_info:
            await create_order_intent(
                db, **_kwargs(recent_returns=correlated_returns, benchmark_provider=bench)
            )

    assert any("correlation" in reason for reason in exc_info.value.reasons)
