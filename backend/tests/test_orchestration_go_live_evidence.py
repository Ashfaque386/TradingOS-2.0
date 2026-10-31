"""The Go-Live evidence (Phase 4): the four gate inputs worked out from the fills, backtests and
Shadow Mode runs the system recorded, with what was set aside and what could not be derived.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from src.engine.risk.go_live_gate import GoLiveReadinessInput
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeRun
from src.orchestration.go_live_evidence import clean_shadow_streak, derive_go_live_evidence
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
NOW = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)  # a Friday, 11:30 IST


def weekdays_only(day: date) -> bool:
    return day.weekday() < 5


# ---- the streak


def d(day: int) -> date:  # 2026-10-<day>: the 26th is a Monday
    return date(2026, 10, day)


def streak(worked, failed=(), today=30, **kwargs):
    return clean_shadow_streak(
        {d(x) for x in worked},
        {d(x) for x in failed},
        d(today),
        is_trading_day=weekdays_only,
        **kwargs,
    )


def test_no_runs_is_no_streak():
    assert streak([]) == 0


def test_consecutive_clean_trading_days_count_and_weekends_are_skipped():
    # Fri 30, Thu 29, Wed 28, Tue 27, Mon 26, then the weekend, then Fri 23.
    assert streak([30, 29, 28, 27, 26, 23]) == 6


def test_a_trading_day_with_no_run_ends_the_streak():
    assert streak([30, 29, 27]) == 2  # nothing on Wed 28


def test_a_failed_run_ends_the_streak_even_on_a_day_that_also_worked():
    assert streak([30, 29, 28], failed=[29]) == 1


def test_today_not_yet_tried_does_not_break_the_streak_but_a_failure_today_zeroes_it():
    assert streak([29, 28]) == 2
    assert streak([30, 29, 28], failed=[30]) == 0
    assert streak([], failed=[30]) == 0


def test_when_today_is_not_a_trading_day_the_count_starts_from_the_last_one():
    assert streak([30, 29], today=31) == 2  # Saturday
    assert streak([31, 30, 29], today=31) == 2  # a run on the Saturday itself does not count


def test_a_holiday_in_the_middle_is_skipped_not_held_against_the_strategy():
    def holiday_on_29(day: date) -> bool:
        return weekdays_only(day) and day != d(29)

    assert clean_shadow_streak({d(30), d(28)}, set(), d(30), is_trading_day=holiday_on_29) == 2


def test_the_lookback_is_bounded():
    every_day = {date(2026, 10, 30) - timedelta(days=n) for n in range(0, 400)}

    count = clean_shadow_streak(every_day, set(), d(30), is_trading_day=lambda day: True)

    assert count == 121  # today plus the 120 days looked back over


def test_a_streak_of_one_is_one():
    assert streak([30]) == 1
    assert streak([29]) == 1  # only yesterday, today not yet tried


# ---- derivation from the records


async def _strategy_with_subscription(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"Evidence{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        sub = PaperTradingSubscription(
            strategy_version_id=version.id,
            symbol="EVID",
            builtin_strategy="always_long",
            initial_capital=100_000.0,
        )
        db.add(sub)
        await db.commit()
        return strategy.id, version.id, sub.id


def _fill(sub_id, *, pnl, costs=0.0, source="mixed", when=NOW, administrative=False, quantity=10):
    return PaperFill(
        subscription_id=sub_id,
        symbol="EVID",
        side="sell",
        order_group_id=uuid.uuid4(),
        requested_quantity=quantity,
        filled_quantity=quantity,
        avg_fill_price=100.0,
        fully_filled=True,
        realized_pnl=pnl,
        costs_total=costs,
        data_source=source,
        administrative=administrative,
        created_at=when,
    )


async def test_a_strategy_with_nothing_recorded_has_no_evidence_and_says_what_is_missing(
    db_session_factory,
):
    strategy_id, _, _ = await _strategy_with_subscription(db_session_factory)

    async with db_session_factory() as db:
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs == GoLiveReadinessInput(
        num_trades=0,
        calendar_days_running=0,
        clean_shadow_mode_streak_days=0,
        live_win_rate=None,
        backtest_win_rate=None,
    )
    assert evidence.readiness().eligible is False
    assert {n.input for n in evidence.not_derived} == {
        "num_trades",
        "live_win_rate",
        "backtest_win_rate",
        "clean_shadow_mode_streak_days",
    }
    assert evidence.counted_fills == 0


async def test_only_closing_fills_on_real_prices_count_and_the_rest_are_set_aside(
    db_session_factory,
):
    strategy_id, _, sub_id = await _strategy_with_subscription(db_session_factory)
    first = NOW - timedelta(days=25)
    async with db_session_factory() as db:
        db.add_all(
            [
                _fill(sub_id, pnl=0.0, when=first),  # an entry: counted as a fill, not a trade
                _fill(sub_id, pnl=500.0, costs=20.0, when=NOW - timedelta(days=10)),  # a win
                _fill(sub_id, pnl=-300.0, costs=20.0, when=NOW - timedelta(days=9)),  # a loss
                _fill(
                    sub_id, pnl=10.0, costs=20.0, when=NOW - timedelta(days=8)
                ),  # +10 gross, -10 net
                _fill(sub_id, pnl=900.0, source="synthetic"),  # generated prices
                _fill(sub_id, pnl=900.0, source="supplied"),  # a typed price
                _fill(sub_id, pnl=900.0, administrative=True),  # a hand cleanup
                _fill(sub_id, pnl=900.0, source="real"),  # real counts too
            ]
        )
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs.num_trades == 4  # +500, -300, +10, +900 (real)
    assert evidence.counted_fills == 5
    assert evidence.excluded_unreal_fills == 2
    assert evidence.excluded_administrative_fills == 1
    # Wins are judged net of the fill's costs: +500-20 and +900 win; -300-20 and +10-20 do not.
    assert evidence.inputs.live_win_rate == pytest.approx(0.5)
    assert evidence.counted_closing_fills == 4


async def test_calendar_days_run_from_the_first_counted_fill_in_ist(db_session_factory):
    strategy_id, _, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add_all(
            [
                _fill(sub_id, pnl=1.0, source="synthetic", when=NOW - timedelta(days=90)),
                _fill(sub_id, pnl=1.0, when=NOW - timedelta(days=22)),
                _fill(sub_id, pnl=1.0, when=NOW - timedelta(days=2)),
            ]
        )
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs.calendar_days_running == 22  # the synthetic fill 90 days ago is ignored
    assert evidence.first_counted_fill_at == NOW - timedelta(days=22)


async def test_a_zero_quantity_fill_is_not_a_fill(db_session_factory):
    strategy_id, _, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add(_fill(sub_id, pnl=0.0, quantity=0))
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.counted_fills == 0


async def _backtest(db, version_id, *, source, win_rate, status=BacktestStatus.COMPLETED, when=NOW):
    run = BacktestRun(
        strategy_version_id=version_id,
        symbol="EVID",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        status=status,
        data_source=source,
        metrics={"win_rate": win_rate} if win_rate is not None else {},
        created_at=when,
    )
    db.add(run)
    return run


async def test_the_backtest_win_rate_comes_only_from_a_backtest_on_real_data(db_session_factory):
    strategy_id, version_id, _ = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        await _backtest(db, version_id, source="supplied", win_rate=0.9, when=NOW)
        await db.commit()
        only_supplied = await derive_go_live_evidence(db, strategy_id, now=NOW)
        real = await _backtest(
            db, version_id, source="real", win_rate=0.6, when=NOW - timedelta(days=1)
        )
        await _backtest(db, version_id, source="real", win_rate=0.8, status="failed", when=NOW)
        await db.commit()
        with_real = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert only_supplied.inputs.backtest_win_rate is None
    reason = next(n.reason for n in only_supplied.not_derived if n.input == "backtest_win_rate")
    assert "supplied" in reason and "backtest on real data is needed" in reason
    assert (
        with_real.inputs.backtest_win_rate == 0.6
    )  # the supplied 0.9 and the failed 0.8 are not used
    assert with_real.backtest_run_id == real.id
    assert with_real.backtest_data_source == "real"
    assert all(n.input != "backtest_win_rate" for n in with_real.not_derived)


async def test_a_strategy_with_no_backtest_at_all_says_so(db_session_factory):
    strategy_id, _, _ = await _strategy_with_subscription(db_session_factory)

    async with db_session_factory() as db:
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    reason = next(n.reason for n in evidence.not_derived if n.input == "backtest_win_rate")
    assert reason == "this strategy has no completed backtest"


async def test_a_backtest_without_a_win_rate_is_not_used(db_session_factory):
    strategy_id, version_id, _ = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        await _backtest(db, version_id, source="real", win_rate=None)
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs.backtest_win_rate is None


def _shadow(strategy_id, *, day, outcome="ok", sandbox=True):
    return ShadowModeRun(
        broker_name="upstox" if sandbox else "zerodha",
        has_sandbox=sandbox,
        confidence="real_sandbox_dry_run" if sandbox else "local_payload_only",
        symbol="EVID",
        side="buy",
        quantity=1,
        order_payload={},
        strategy_id=strategy_id,
        outcome=outcome,
        failure_reason=None if outcome == "ok" else "boom",
        created_at=datetime(2026, 10, day, 4, 0, tzinfo=UTC),
    )


async def test_the_shadow_streak_is_derived_from_this_strategys_runs_and_failures_are_counted(
    db_session_factory,
):
    strategy_id, _, _ = await _strategy_with_subscription(db_session_factory)
    other_id, _, _ = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add_all(
            [
                _shadow(strategy_id, day=30),
                _shadow(strategy_id, day=29, sandbox=False),
                _shadow(strategy_id, day=28),
                _shadow(strategy_id, day=27, outcome="failed"),
                _shadow(strategy_id, day=26),
                _shadow(other_id, day=27),  # someone else's run does not rescue the gap
            ]
        )
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs.clean_shadow_mode_streak_days == 3  # Fri, Thu, Wed; Tue failed
    assert (
        evidence.shadow_clean_days_real_sandbox == 3
    )  # Fri 30, Wed 28, Mon 26 (+ not Thu: payload)
    assert evidence.shadow_runs_failed_recently == 1
    assert all(n.input != "clean_shadow_mode_streak_days" for n in evidence.not_derived)


async def test_evidence_is_judged_by_the_gate_on_the_derived_numbers(db_session_factory):
    strategy_id, version_id, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add(_fill(sub_id, pnl=1.0, when=NOW - timedelta(days=30)))
        for i in range(30):
            db.add(_fill(sub_id, pnl=10.0 if i % 2 == 0 else -5.0, when=NOW - timedelta(hours=i)))
        await _backtest(db, version_id, source="real", win_rate=0.55)
        # Tue 20 October 2026 is Dussehra, a market holiday: it neither counts nor breaks the run.
        for day in (30, 29, 28, 27, 26, 23, 22, 21, 19, 16):
            db.add(_shadow(strategy_id, day=day))
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    result = evidence.readiness()
    assert evidence.inputs.num_trades == 31
    assert evidence.inputs.calendar_days_running == 30
    assert evidence.inputs.clean_shadow_mode_streak_days == 10
    assert evidence.inputs.live_win_rate == pytest.approx(16 / 31)
    assert result.eligible is True
    assert result.checks == {
        "min_trades": True,
        "min_calendar_days": True,
        "clean_shadow_streak": True,
        "win_rate_divergence": True,
    }


def test_the_evidence_types_are_immutable_values():
    from dataclasses import FrozenInstanceError

    from src.orchestration.go_live_evidence import GoLiveEvidence, NotDerived

    item = NotDerived("num_trades", "why")
    with pytest.raises(FrozenInstanceError):
        item.reason = "other"  # type: ignore[misc]
    assert not hasattr(item, "__dict__")

    evidence = GoLiveEvidence(
        strategy_id=uuid.uuid4(),
        as_of=NOW,
        inputs=GoLiveReadinessInput(0, 0, 0, None, None),
        counted_closing_fills=0,
        counted_fills=0,
        excluded_unreal_fills=0,
        excluded_administrative_fills=0,
        first_counted_fill_at=None,
        backtest_run_id=None,
        backtest_data_source=None,
        shadow_clean_days_real_sandbox=0,
        shadow_runs_failed_recently=0,
    )
    assert evidence.not_derived == []  # the default is an empty list, not shared between instances
    assert evidence.not_derived is not GoLiveEvidence.__dataclass_fields__["not_derived"].default
    with pytest.raises(FrozenInstanceError):
        evidence.counted_fills = 1  # type: ignore[misc]
    assert not hasattr(evidence, "__dict__")


async def test_a_fill_of_one_share_counts_and_a_break_even_close_is_not_a_win(db_session_factory):
    strategy_id, _, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add_all(
            [
                _fill(sub_id, pnl=20.0, costs=20.0, quantity=1),  # +20 gross, exactly 0 net
                _fill(sub_id, pnl=30.0, costs=0.0, quantity=1),
            ]
        )
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.counted_fills == 2  # one-share fills are fills
    assert evidence.inputs.num_trades == 2
    assert evidence.inputs.live_win_rate == pytest.approx(0.5)  # break-even is not a win


async def test_a_strategy_whose_first_real_fill_is_today_has_run_zero_days(db_session_factory):
    strategy_id, _, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add(_fill(sub_id, pnl=1.0, when=NOW))
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.inputs.calendar_days_running == 0


async def test_the_reasons_something_could_not_be_derived_are_spelled_out(db_session_factory):
    strategy_id, version_id, sub_id = await _strategy_with_subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add(_fill(sub_id, pnl=5.0, source="synthetic"))
        await _backtest(db, version_id, source="supplied", win_rate=0.7)
        await _backtest(db, version_id, source="synthetic", win_rate=0.7)
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    reasons = {n.input: n.reason for n in evidence.not_derived}
    assert reasons["num_trades"] == (
        "no paper fill on real prices has been recorded for this strategy yet "
        "(1 on generated or typed prices were set aside)"
    )
    assert reasons["live_win_rate"] == (
        "no closing fill on real prices yet, so there is no win rate to compare"
    )
    assert reasons["backtest_win_rate"] == (
        "its completed backtests rest on bars this system cannot vouch for "
        "(supplied, synthetic); a backtest on real data is needed"
    )
    assert reasons["clean_shadow_mode_streak_days"] == (
        "no Shadow Mode dry run has been made for this strategy: run one, or connect a "
        "broker so the daily dry run can"
    )


async def test_nothing_set_aside_adds_no_remark_to_the_trades_reason(db_session_factory):
    strategy_id, _, _ = await _strategy_with_subscription(db_session_factory)

    async with db_session_factory() as db:
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    reasons = {n.input: n.reason for n in evidence.not_derived}
    assert (
        reasons["num_trades"]
        == "no paper fill on real prices has been recorded for this strategy yet"
    )


async def test_failed_runs_count_as_recent_for_exactly_fourteen_days(db_session_factory):
    strategy_id, _, _ = await _strategy_with_subscription(db_session_factory)

    def failed_at(moment):
        run = _shadow(strategy_id, day=1, outcome="failed")
        run.created_at = moment
        return run

    async with db_session_factory() as db:
        db.add_all(
            [
                failed_at(NOW - timedelta(days=14)),  # on the boundary: recent
                failed_at(NOW - timedelta(days=14, hours=12)),  # a little older: not
                failed_at(NOW - timedelta(days=1)),
            ]
        )
        await db.commit()
        evidence = await derive_go_live_evidence(db, strategy_id, now=NOW)

    assert evidence.shadow_runs_failed_recently == 2
