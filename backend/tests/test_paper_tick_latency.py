"""The drain job on the real polling cadence: a tick is fetched every 3 s and drained every 5 s,
so a tick is routinely seconds old when it is processed. That is the normal state of the polling
design, not a degraded feed, and it must not stop trading."""

import time

from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.orchestration import paper_trading_scheduler as scheduler_module
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, age_ms):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"Latency{age_ms}", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="LATENCYSTOCK",
            builtin_strategy="sma_crossover",
        )
    await redis_client.delete(tick_stream_key("LATENCYSTOCK"))
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    processed: list[float] = []

    async def record(db, **kwargs):
        processed.append(kwargs["tick_price"])

    monkeypatch.setattr(scheduler_module, "process_tick", record)
    await publish_tick(
        redis_client,
        Tick(
            symbol="LATENCYSTOCK",
            price=100.0,
            timestamp_ms=int(time.time() * 1000) - age_ms,
            source="real",
        ),
    )
    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis_client,
        MockOrderBookProvider(),
        ReferenceTableRegulatoryDataProvider(),
    )
    await redis_client.delete(tick_stream_key("LATENCYSTOCK"))
    async with db_session_factory() as db:  # so a second call in one test is the only subscriber
        (await db.get(type(subscription), subscription.id)).is_active = False
        await db.commit()
    return processed


async def test_a_tick_a_few_seconds_old_is_still_processed(
    db_session_factory, redis_client, monkeypatch
):
    assert await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 4000) == [
        100.0
    ]


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def test_a_tick_older_than_the_limit_is_skipped_and_counted(
    db_session_factory, redis_client, monkeypatch
):
    before = _sample("tradingos_paper_ticks_skipped_stale_total")
    processed = await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 25_000)
    assert processed == []
    assert _sample("tradingos_paper_ticks_skipped_stale_total") == before + 1


async def test_the_limit_is_the_setting_not_a_websocket_bound(
    db_session_factory, redis_client, monkeypatch
):
    from src.core.config import get_settings

    assert get_settings().paper_tick_max_age_seconds == 20.0
    # Just inside it is acted on, just outside it is not.
    assert await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 15_000) == [
        100.0
    ]
    assert await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 25_000) == []


async def test_the_drain_job_measures_a_ticks_age_and_time_to_decision(
    db_session_factory, redis_client, monkeypatch
):
    age = "tradingos_paper_tick_age_seconds_count"
    decision = "tradingos_paper_tick_to_decision_seconds_count"
    before_age, before_decision = _sample(age), _sample(decision)
    await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 3_000)
    assert _sample(age) == before_age + 1
    assert _sample(decision) == before_decision + 1
    # A skipped tick has an age but no decision.
    await _drain_one_tick_aged(db_session_factory, redis_client, monkeypatch, 40_000)
    assert _sample(age) == before_age + 2
    assert _sample(decision) == before_decision + 1


async def test_a_tick_whose_processing_failed_has_no_decision_time(
    db_session_factory, redis_client, monkeypatch
):
    decision = "tradingos_paper_tick_to_decision_seconds_count"
    before = _sample(decision)

    async def fail(db, **kwargs):
        raise RuntimeError("one bad tick")

    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="LatencyFail", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="LATENCYFAIL",
            builtin_strategy="sma_crossover",
        )
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    monkeypatch.setattr(scheduler_module, "process_tick", fail)
    await publish_tick(
        redis_client,
        Tick(symbol="LATENCYFAIL", price=1.0, timestamp_ms=int(time.time() * 1000), source="real"),
    )
    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis_client,
        MockOrderBookProvider(),
        ReferenceTableRegulatoryDataProvider(),
    )
    await redis_client.delete(tick_stream_key("LATENCYFAIL"))
    assert _sample(decision) == before
