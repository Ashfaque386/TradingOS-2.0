"""The daily Shadow Mode dry run (Phase 4): one check per paper-trading strategy per trading day, so
the Go-Live gate's clean-streak builds up on its own, and nothing recorded when nothing was tried.
"""

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import select

from src.brokers.base import BrokerCredentials
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.shadow_mode_run import ShadowModeRun
from src.models.strategy import Strategy, StrategyStatus
from src.orchestration import shadow_mode_scheduler as scheduler_module
from src.orchestration.strategies import create_strategy, create_version_with_validation
from src.security.secrets_store import SecretsStore

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
FRIDAY = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)  # 09:30 IST
SATURDAY = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)


def _store(tmp_path, *, with_broker=True) -> SecretsStore:
    store = SecretsStore(tmp_path / "secrets.enc", Fernet.generate_key().decode())
    if with_broker:
        store.set_credentials("zerodha", BrokerCredentials(api_key="k", access_token="t"))
    return store


async def _paper_strategy(
    db_session_factory, name, *, status=StrategyStatus.PAPER_TRADING, active=True
):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=name, objective="o")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        db.add(
            PaperTradingSubscription(
                strategy_version_id=version.id,
                symbol="SHDW",
                builtin_strategy="always_long",
                is_active=active,
            )
        )
        row = await db.get(Strategy, strategy.id)
        row.status = status.value
        await db.commit()
        return strategy.id


async def _runs(db_session_factory):
    async with db_session_factory() as db:
        return list((await db.execute(select(ShadowModeRun))).scalars().all())


async def test_each_paper_trading_strategy_gets_one_dry_run_tied_to_it(
    db_session_factory, tmp_path
):
    first = await _paper_strategy(db_session_factory, "ShadowA")
    second = await _paper_strategy(db_session_factory, "ShadowB")

    made = await scheduler_module.run_daily_dry_runs(
        db_session_factory, store=_store(tmp_path), now=FRIDAY
    )

    runs = await _runs(db_session_factory)
    assert made == 2
    assert {r.strategy_id for r in runs} == {first, second}
    assert all(r.symbol == "SHDW" and r.side == "buy" and r.quantity == 1 for r in runs)
    assert all(r.outcome == "ok" and r.broker_name == "zerodha" for r in runs)


async def test_a_strategy_that_already_had_a_dry_run_today_is_not_run_again(
    db_session_factory, tmp_path
):
    await _paper_strategy(db_session_factory, "ShadowOnce")
    store = _store(tmp_path)

    first = await scheduler_module.run_daily_dry_runs(db_session_factory, store=store, now=FRIDAY)
    again = await scheduler_module.run_daily_dry_runs(db_session_factory, store=store, now=FRIDAY)

    assert (first, again) == (1, 0)
    assert len(await _runs(db_session_factory)) == 1


async def test_only_strategies_in_paper_trading_with_an_active_subscription_are_checked(
    db_session_factory, tmp_path
):
    await _paper_strategy(db_session_factory, "NotYet", status=StrategyStatus.BACKTESTING)
    await _paper_strategy(db_session_factory, "AlreadyLive", status=StrategyStatus.LIVE_ELIGIBLE)
    await _paper_strategy(db_session_factory, "SwitchedOff", active=False)

    made = await scheduler_module.run_daily_dry_runs(
        db_session_factory, store=_store(tmp_path), now=FRIDAY
    )

    assert made == 0
    assert await _runs(db_session_factory) == []


async def test_with_no_broker_connected_nothing_is_tried_and_nothing_is_recorded(
    db_session_factory, tmp_path
):
    await _paper_strategy(db_session_factory, "NoBroker")

    made = await scheduler_module.run_daily_dry_runs(
        db_session_factory, store=_store(tmp_path, with_broker=False), now=FRIDAY
    )

    assert made == 0
    assert await _runs(db_session_factory) == []  # a day nobody tried is not a clean day


async def test_nothing_is_run_on_a_day_the_market_is_shut(db_session_factory, tmp_path):
    await _paper_strategy(db_session_factory, "Weekend")

    made = await scheduler_module.run_daily_dry_runs(
        db_session_factory, store=_store(tmp_path), now=SATURDAY
    )

    assert made == 0
    assert await _runs(db_session_factory) == []


async def test_one_strategy_failing_does_not_stop_the_others(
    db_session_factory, tmp_path, monkeypatch
):
    first = await _paper_strategy(db_session_factory, "BrokenOne")
    second = await _paper_strategy(db_session_factory, "FineOne")
    real = scheduler_module.run_shadow_order_check

    async def flaky(db, *, adapter, order, strategy_id):
        if strategy_id == first:
            raise RuntimeError("database hiccup")
        return await real(db, adapter=adapter, order=order, strategy_id=strategy_id)

    monkeypatch.setattr(scheduler_module, "run_shadow_order_check", flaky)

    made = await scheduler_module.run_daily_dry_runs(
        db_session_factory, store=_store(tmp_path), now=FRIDAY
    )

    assert made == 1
    assert [r.strategy_id for r in await _runs(db_session_factory)] == [second]


async def test_the_scheduler_has_the_one_weekday_job(db_session_factory):
    scheduler = scheduler_module.start_shadow_mode_scheduler(db_session_factory)
    try:
        jobs = scheduler.get_jobs()
        assert [job.id for job in jobs] == [scheduler_module.DRY_RUN_JOB_ID]
        trigger = str(jobs[0].trigger)
        assert "mon-fri" in trigger and "hour='9'" in trigger and "minute='5'" in trigger
    finally:
        scheduler.shutdown(wait=False)
