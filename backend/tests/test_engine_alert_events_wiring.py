"""The places the engine raises an alert where something happens: a stop-loss exit, a futures roll
that cannot be done, an expiry square-off that cannot be done, a position closed after its contract
expired. Each is driven through the real code path, not by calling the alert module directly.
"""

import uuid

import pandas as pd
import pytest
from sqlalchemy import select

from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.engine_alert_event import EngineAlertEvent
from src.models.paper_position import PaperPosition
from src.orchestration import engine_alerts
from src.orchestration import paper_trading_scheduler as scheduler
from src.orchestration.paper_futures import RollOutcome
from src.orchestration.paper_options import SweepOutcome
from src.orchestration.paper_trading import (
    enroll_in_paper_trading,
    process_tick,
    run_daily_signal_generation,
)
from src.orchestration.paper_trading_scheduler import (
    expiry_alerts,
    forced_close_alert,
    roll_alerts,
    run_expiry_sweep_job,
    run_futures_roll_job,
)
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
_PRICES = FakeDailyPriceProvider(seed_by_symbol={"DEMOSTOCK": 1})
_BOOKS = MockOrderBookProvider()
_REGULATORY = ReferenceTableRegulatoryDataProvider()
_BUY_DAY = pd.Timestamp("2026-09-09")  # a genuine flat -> long crossover day in the fake series


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    monkeypatch.setattr(engine_alerts, "notify", fake_notify)
    return calls


async def _subscription(db_session_factory, **overrides):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"E-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        kwargs = dict(
            strategy_version_id=version.id,
            symbol="DEMOSTOCK",
            builtin_strategy="sma_crossover",
            sma_window=15,
            initial_capital=100_000.0,
            stop_loss_pct=3.0,
        )
        kwargs.update(overrides)
        return await enroll_in_paper_trading(db, **kwargs)


async def _events(db_session_factory):
    async with db_session_factory() as db:
        return (
            (await db.execute(select(EngineAlertEvent).order_by(EngineAlertEvent.opened_at)))
            .scalars()
            .all()
        )


async def _enter(db_session_factory, subscription):
    async with db_session_factory() as db:
        [signal] = await run_daily_signal_generation(db, price_provider=_PRICES, as_of=_BUY_DAY)
    async with db_session_factory() as db:
        return await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=signal.reference_price,
            order_book_provider=_BOOKS,
            regulatory_provider=_REGULATORY,
        )


async def _tick(db_session_factory, subscription, price):
    async with db_session_factory() as db:
        return await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=price,
            order_book_provider=_BOOKS,
            regulatory_provider=_REGULATORY,
        )


# ---- a stop-loss exit ----


async def test_a_stop_loss_exit_tells_a_person_what_was_closed_and_why(db_session_factory, sent):
    subscription = await _subscription(db_session_factory)
    entry = await _enter(db_session_factory, subscription)
    assert await _events(db_session_factory) == []  # entering is not news

    exit_fill = await _tick(db_session_factory, subscription, entry.avg_fill_price * 0.9)
    await engine_alerts.wait_for_announcements()

    [event] = await _events(db_session_factory)
    assert (event.source, event.kind, event.severity) == ("event", "stop_loss", "warning")
    assert event.key == f"stop_loss:{exit_fill.id}"
    assert event.subscription_id == subscription.id
    assert event.title == "DEMOSTOCK: stop-loss hit, position closed"
    assert event.detail == (
        f"Sold {exit_fill.filled_quantity} of {exit_fill.requested_quantity} at an average "
        f"{exit_fill.avg_fill_price:.2f} (position cost {entry.avg_fill_price:.2f}, "
        "stop-loss 3%)."
    )
    assert event.notified_at is not None
    assert [c[1] for c in sent] == ["DEMOSTOCK: stop-loss hit, position closed"]


async def test_a_short_position_stopped_out_is_bought_back(db_session_factory, sent):
    subscription = await _subscription(db_session_factory)
    async with db_session_factory() as db:
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol="DEMOSTOCK", quantity=-10, avg_cost=100.0
            )
        )
        await db.commit()

    fill = await _tick(db_session_factory, subscription, 110.0)  # 10% against a short

    await engine_alerts.wait_for_announcements()
    assert fill is not None and fill.side == "buy"
    [event] = await _events(db_session_factory)
    assert event.detail.startswith(f"Bought back {fill.filled_quantity} of 10 at an average ")
    assert "position cost 100.00" in event.detail


async def test_a_tick_that_changes_nothing_is_not_an_alert(db_session_factory, sent):
    subscription = await _subscription(db_session_factory)
    entry = await _enter(db_session_factory, subscription)

    assert await _tick(db_session_factory, subscription, entry.avg_fill_price) is None
    assert await _events(db_session_factory) == []
    assert sent == []


async def test_an_alert_that_cannot_be_recorded_does_not_undo_the_stop_out(
    db_session_factory, monkeypatch
):
    async def exploding(*_a, **_k):
        raise RuntimeError("channel store on fire")

    monkeypatch.setattr(engine_alerts, "notify", exploding)
    subscription = await _subscription(db_session_factory)
    entry = await _enter(db_session_factory, subscription)

    exit_fill = await _tick(db_session_factory, subscription, entry.avg_fill_price * 0.9)

    await engine_alerts.wait_for_announcements()

    assert exit_fill is not None and exit_fill.side == "sell"  # the position was closed
    [event] = await _events(db_session_factory)  # and the record of it was kept
    assert event.kind == "stop_loss"


# ---- a futures roll that cannot be done ----


def _roll(subscription_id, status="postponed", old="NIFTY26NOVFUT", detail="no fresh tick"):
    return RollOutcome(str(subscription_id), status, old, "NIFTY26DECFUT", detail)


def test_a_postponed_roll_is_an_alert_keyed_by_its_subscription():
    sub = uuid.uuid4()

    [alert] = roll_alerts([_roll(sub)])

    assert alert.key == f"roll:{sub}"
    assert (alert.kind, alert.severity) == ("roll_postponed", "warning")
    assert alert.title == "Roll of NIFTY26NOVFUT postponed"
    assert alert.detail == "no fresh tick"
    assert alert.subscription_id == sub


def test_a_missing_next_contract_is_its_own_kind_of_alert():
    [alert] = roll_alerts([_roll(uuid.uuid4(), "no_contract", detail="the broker lists none")])

    assert alert.kind == "roll_no_contract"
    assert alert.title == "No contract to roll NIFTY26NOVFUT into"
    assert alert.detail == "the broker lists none"


def test_a_roll_that_went_through_is_not_an_alert():
    outcomes = [_roll(uuid.uuid4(), "rolled"), _roll(uuid.uuid4(), "not_due")]

    assert roll_alerts(outcomes) == []


def test_a_roll_with_no_reason_and_no_contract_name_still_reads_properly():
    [alert] = roll_alerts([RollOutcome(str(uuid.uuid4()), "postponed", None)])

    assert alert.title == "Roll of the futures contract postponed"
    assert alert.detail == "It will be tried again."


async def _market_open(monkeypatch, open_=True):
    monkeypatch.setattr(scheduler, "is_market_open_ist", lambda *a, **k: open_)


def _serve_rolls(monkeypatch, outcomes):
    async def fake_roll(*_a, **_k):
        return outcomes

    monkeypatch.setattr(scheduler, "run_futures_roll", fake_roll)


async def _run_roll_job(db_session_factory):
    await run_futures_roll_job(db_session_factory, None, _BOOKS, _REGULATORY, lambda: [])  # type: ignore[arg-type]


async def test_a_roll_that_stays_postponed_is_one_alert_until_it_goes_through(
    db_session_factory, monkeypatch, sent
):
    subscription = await _subscription(db_session_factory)
    await _market_open(monkeypatch)

    _serve_rolls(monkeypatch, [_roll(subscription.id)])
    await _run_roll_job(db_session_factory)
    await _run_roll_job(db_session_factory)  # ten minutes later, still stuck
    _serve_rolls(monkeypatch, [_roll(subscription.id, "rolled")])
    await _run_roll_job(db_session_factory)

    assert [c[1] for c in sent] == [
        "Roll of NIFTY26NOVFUT postponed",
        "Cleared: Roll of NIFTY26NOVFUT postponed",
    ]
    [event] = await _events(db_session_factory)
    assert event.resolved_at is not None and event.source == "event"


async def test_a_shut_market_neither_opens_nor_clears_a_roll_alert(
    db_session_factory, monkeypatch, sent
):
    subscription = await _subscription(db_session_factory)
    await _market_open(monkeypatch)
    _serve_rolls(monkeypatch, [_roll(subscription.id)])
    await _run_roll_job(db_session_factory)

    await _market_open(monkeypatch, open_=False)
    _serve_rolls(monkeypatch, [])
    await _run_roll_job(db_session_factory)  # returns before looking at anything

    [event] = await _events(db_session_factory)
    assert event.resolved_at is None


# ---- an expiry square-off that cannot be done ----


def _sweep(subscription_id, status, symbol="NIFTY26NOV24500CE", detail="", forced=False):
    return SweepOutcome(str(subscription_id), symbol, status, detail, forced=forced)


def test_a_postponed_squareoff_is_a_warning_and_an_unpriceable_expiry_is_critical():
    sub = uuid.uuid4()

    postponed, no_price = expiry_alerts(
        [
            _sweep(sub, "postponed", detail="only part closed (25 left)"),
            _sweep(sub, "no_price", symbol="NIFTY26NOV24400PE", detail="no quote was ever seen"),
        ]
    )

    assert postponed.key == f"expiry:{sub}:NIFTY26NOV24500CE"
    assert (postponed.kind, postponed.severity) == ("expiry_postponed", "warning")
    assert postponed.title == "Square-off of NIFTY26NOV24500CE before expiry postponed"
    assert postponed.detail == "only part closed (25 left)"
    assert (no_price.kind, no_price.severity) == ("expiry_no_price", "critical")
    assert no_price.title == "NIFTY26NOV24400PE expired with no price to close it at"


def test_a_position_that_was_closed_is_not_a_condition():
    assert (
        expiry_alerts([_sweep(uuid.uuid4(), "closed"), _sweep(uuid.uuid4(), "closed", forced=True)])
        == []
    )


def test_a_position_closed_after_its_expiry_is_reported_as_an_event():
    sub = uuid.uuid4()
    outcome = _sweep(sub, "closed", detail="at the last quote, 12.5", forced=True)

    first, second = forced_close_alert(outcome), forced_close_alert(outcome)

    assert first.key.startswith(f"expiry_forced_close:{sub}:NIFTY26NOV24500CE:")
    assert first.key != second.key  # each is its own event
    assert first.kind == "expiry_forced_close"
    assert first.title == "NIFTY26NOV24500CE was closed after its expiry"
    assert first.detail == (
        "It was still open when the contract expired, so it was closed at the last quote, 12.5."
    )


def _serve_sweep(monkeypatch, outcomes):
    async def fake_sweep(*_a, **_k):
        return outcomes

    monkeypatch.setattr(scheduler, "run_expiry_sweep", fake_sweep)


async def test_the_expiry_sweep_alerts_for_what_it_could_not_do_and_what_it_did_late(
    db_session_factory, monkeypatch, sent
):
    subscription = await _subscription(db_session_factory)
    await _market_open(monkeypatch)
    _serve_sweep(
        monkeypatch,
        [
            _sweep(subscription.id, "postponed", detail="no fresh tick"),
            _sweep(subscription.id, "no_price", symbol="NIFTY26NOV24400PE", detail="never quoted"),
            _sweep(
                subscription.id,
                "closed",
                symbol="NIFTY26OCT24300CE",
                detail="at the last quote, 12.5",
                forced=True,
            ),
        ],
    )

    await run_expiry_sweep_job(db_session_factory, None, _BOOKS, _REGULATORY)  # type: ignore[arg-type]

    events = {e.kind: e for e in await _events(db_session_factory)}
    assert set(events) == {"expiry_postponed", "expiry_no_price", "expiry_forced_close"}
    assert events["expiry_postponed"].resolved_at is None  # still undone
    assert events["expiry_no_price"].resolved_at is None
    assert events["expiry_forced_close"].resolved_at is not None  # over the moment it happened
    assert sorted(c[1] for c in sent) == [
        "NIFTY26NOV24400PE expired with no price to close it at",
        "NIFTY26OCT24300CE was closed after its expiry",
        "Square-off of NIFTY26NOV24500CE before expiry postponed",
    ]


async def test_the_sweep_clears_its_alerts_once_the_positions_are_dealt_with(
    db_session_factory, monkeypatch, sent
):
    subscription = await _subscription(db_session_factory)
    await _market_open(monkeypatch)
    _serve_sweep(monkeypatch, [_sweep(subscription.id, "postponed", detail="no fresh tick")])
    await run_expiry_sweep_job(db_session_factory, None, _BOOKS, _REGULATORY)  # type: ignore[arg-type]

    _serve_sweep(monkeypatch, [_sweep(subscription.id, "closed")])
    await run_expiry_sweep_job(db_session_factory, None, _BOOKS, _REGULATORY)  # type: ignore[arg-type]

    [event] = await _events(db_session_factory)
    assert event.resolved_at is not None
    assert [c[1] for c in sent] == [
        "Square-off of NIFTY26NOV24500CE before expiry postponed",
        "Cleared: Square-off of NIFTY26NOV24500CE before expiry postponed",
    ]
