"""Order-Intent Risk Gate tests (Build Spec §8): the acceptance-critical
one is that tripping the kill switch blocks intent *creation* -- not just
some later submission step -- and that live/paper are genuinely
independent. Also proves the naked-options scan and correlation
constraint are wired into this per-tick gate, not only pre-deployment.
"""

from dataclasses import FrozenInstanceError

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


# ---- Found by mutation testing (2026-10-13) -------------------------------------
# mutmut changed `and` to `or` in the "is there a correlation to evaluate" guard,
# the correlation's starting value from None to "", and the wording of the
# rejection reasons; no test noticed any of them.


def _uncorrelated_pair():
    idx = pd.bdate_range("2024-01-01", periods=60)
    bench = make_fake_nifty_benchmark(idx, seed=0)
    unrelated = make_fake_nifty_benchmark(idx, seed=12345).returns
    return unrelated, bench


async def test_without_a_correlation_input_none_is_evaluated_and_none_is_recorded(
    db_session_factory,
):
    returns, bench = _uncorrelated_pair()

    async with db_session_factory() as db:
        neither = await create_order_intent(db, **_kwargs())
        only_returns = await create_order_intent(db, **_kwargs(recent_returns=returns))
        only_benchmark = await create_order_intent(db, **_kwargs(benchmark_provider=bench))

    # Half the inputs is not enough to evaluate anything: no crash, and the
    # intent says "not evaluated" (None), not some placeholder.
    assert neither.correlation is None
    assert only_returns.correlation is None
    assert only_benchmark.correlation is None


async def test_an_evaluated_but_unbreached_correlation_is_recorded_on_the_intent(
    db_session_factory,
):
    returns, bench = _uncorrelated_pair()

    async with db_session_factory() as db:
        intent = await create_order_intent(
            db, **_kwargs(recent_returns=returns, benchmark_provider=bench)
        )

    assert intent.correlation is not None and intent.correlation.breached is False


async def test_the_correlation_rejection_reason_names_the_symbol_value_and_limit(
    db_session_factory,
):
    idx = pd.bdate_range("2024-01-01", periods=30)
    bench = make_fake_nifty_benchmark(idx, seed=0)

    async with db_session_factory() as db:
        with pytest.raises(OrderIntentRejectedError) as exc_info:
            await create_order_intent(
                db,
                **_kwargs(
                    recent_returns=bench.returns * 0.95 + 0.0001,
                    benchmark_provider=bench,
                    correlation_threshold=0.5,
                ),
            )

    (reason,) = exc_info.value.reasons
    assert reason == (
        "RELIANCE: recent returns correlation 1.0000 with Nifty 50 exceeds constraint 0.5000"
    )


async def test_every_rejection_reason_reaches_the_error_message(db_session_factory):
    async with db_session_factory() as db:
        with pytest.raises(OrderIntentRejectedError) as exc_info:
            # A position-limit breach and a circuit-band breach together.
            await create_order_intent(
                db, **_kwargs(proposed_position_value=900_000, proposed_price=3_000)
            )

    reasons = exc_info.value.reasons
    assert len(reasons) >= 2
    assert str(exc_info.value) == "; ".join(reasons)


async def test_an_approved_intent_cannot_be_altered_after_the_gate_approved_it(
    db_session_factory,
):
    async with db_session_factory() as db:
        intent = await create_order_intent(db, **_kwargs())

    # What was checked is what is submitted: quantity and price are frozen.
    with pytest.raises(FrozenInstanceError):
        intent.quantity = 10_000_000  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        intent.price = 1.0  # type: ignore[misc]
