"""Engine alert episodes (src.orchestration.engine_alerts): opened once, announced once, resolved
with a "cleared" notice, and opened afresh if the condition comes back.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.types import AlertLevel
from src.orchestration import engine_alerts
from src.orchestration.engine_alerts import (
    AlertCandidate,
    clear_conditions,
    open_or_refresh,
    record_event,
    record_event_safely,
    report_condition,
    sync_conditions_safely,
    sync_watched,
    wait_for_announcements,
)

T0 = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
CONFIRM = {"critical": 0.0, "warning": 120.0}
BOTH = frozenset({"critical", "warning"})
CRITICAL_ONLY = frozenset({"critical"})


@pytest.fixture
def sent(monkeypatch):
    """Every notice that would have gone out, as (level, title, body, details)."""
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    monkeypatch.setattr(engine_alerts, "notify", fake_notify)
    return calls


def _unwatched(sub="s1", symbol="RELIANCE"):
    return AlertCandidate(
        key=f"{sub}:unmanaged",
        kind="unmanaged",
        severity="critical",
        title=f"{symbol} is holding a position nobody is watching",
        detail="Close the position, or switch the subscription back on.",
    )


def _halted(sub="s2", symbol="TCS"):
    return AlertCandidate(
        key=f"{sub}:halted",
        kind="halted",
        severity="warning",
        title=f"{symbol} is not trading",
        detail="no broker is connected",
    )


async def _all(db_session_factory):
    async with db_session_factory() as db:
        return (
            (await db.execute(select(EngineAlertEvent).order_by(EngineAlertEvent.opened_at)))
            .scalars()
            .all()
        )


# ---- one open episode per condition ----


async def test_a_condition_seen_again_is_the_same_episode_with_its_wording_refreshed(
    db_session_factory,
):
    async with db_session_factory() as db:
        first, created = await open_or_refresh(db, "watcher", _halted(), now=T0)
        await db.commit()
        again = _halted()
        again = AlertCandidate(
            again.key, again.kind, "critical", "TCS is not trading at all", "now a new reason"
        )
        second, created_again = await open_or_refresh(
            db, "watcher", again, now=T0 + timedelta(minutes=1)
        )
        await db.commit()

    assert (created, created_again) == (True, False)
    assert second.id == first.id
    rows = await _all(db_session_factory)
    assert len(rows) == 1
    assert (rows[0].severity, rows[0].title, rows[0].detail) == (
        "critical",
        "TCS is not trading at all",
        "now a new reason",
    )
    assert rows[0].opened_at == T0
    assert rows[0].last_seen_at == T0 + timedelta(minutes=1)


async def test_a_second_writer_that_loses_the_race_finds_the_winners_episode(db_session_factory):
    async with db_session_factory() as winner_db:
        await open_or_refresh(winner_db, "watcher", _halted(), now=T0)
        await winner_db.flush()  # inserted, not yet committed -- the other writer cannot see it
        async with db_session_factory() as loser_db:
            # The loser's read finds nothing, its insert waits on the winner and then conflicts.
            loser = asyncio.create_task(open_or_refresh(loser_db, "watcher", _halted(), now=T0))
            await asyncio.sleep(0.2)
            await winner_db.commit()
            row, created = await loser
            await loser_db.commit()

    assert created is False
    assert len(await _all(db_session_factory)) == 1
    assert row.key == "s2:halted"


async def test_a_resolved_episode_does_not_block_the_next_one(db_session_factory):
    async with db_session_factory() as db:
        row, _ = await open_or_refresh(db, "watcher", _halted(), now=T0)
        engine_alerts.resolve_row(row, now=T0 + timedelta(minutes=5))
        await db.commit()
        again, created = await open_or_refresh(
            db, "watcher", _halted(), now=T0 + timedelta(minutes=9)
        )
        await db.commit()

    assert created is True
    assert again.id != row.id
    assert len(await _all(db_session_factory)) == 2


# ---- the watcher's look ----


async def test_a_critical_condition_is_announced_at_once(db_session_factory, sent):
    async with db_session_factory() as db:
        result = await sync_watched(db, [_unwatched()], scope=BOTH, confirm_seconds=CONFIRM, now=T0)

    assert (result.opened, result.announced, result.resolved) == (1, 1, 0)
    assert sent == [
        (
            AlertLevel.ENGINE,
            "RELIANCE is holding a position nobody is watching",
            "Close the position, or switch the subscription back on.",
            {"severity": "critical"},
        )
    ]
    (row,) = await _all(db_session_factory)
    assert row.notified_at == T0
    assert row.source == "watcher"


async def test_a_warning_waits_until_it_has_lasted_the_confirmation_time(db_session_factory, sent):
    async with db_session_factory() as db:
        first = await sync_watched(db, [_halted()], scope=BOTH, confirm_seconds=CONFIRM, now=T0)
        too_soon = await sync_watched(
            db, [_halted()], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(seconds=119)
        )
        on_time = await sync_watched(
            db, [_halted()], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(seconds=120)
        )
        later = await sync_watched(
            db, [_halted()], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(minutes=30)
        )

    assert [r.announced for r in (first, too_soon, on_time, later)] == [0, 0, 1, 0]
    assert len(sent) == 1
    assert sent[0][1] == "TCS is not trading"


async def test_a_warning_that_clears_before_it_is_announced_makes_no_noise(
    db_session_factory, sent
):
    async with db_session_factory() as db:
        await sync_watched(db, [_halted()], scope=BOTH, confirm_seconds=CONFIRM, now=T0)
        result = await sync_watched(
            db, [], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(seconds=60)
        )

    assert result.resolved == 1
    assert sent == []  # a price that was late for one poll messages no one, then or when it clears
    (row,) = await _all(db_session_factory)
    assert row.resolved_at == T0 + timedelta(seconds=60)
    assert (row.notified_at, row.resolution_notified_at) == (None, None)


async def test_an_announced_episode_says_when_it_clears(db_session_factory, sent):
    async with db_session_factory() as db:
        await sync_watched(db, [_unwatched()], scope=BOTH, confirm_seconds=CONFIRM, now=T0)
        await sync_watched(
            db, [], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(minutes=3)
        )

    assert [(c[1], c[2]) for c in sent] == [
        (
            "RELIANCE is holding a position nobody is watching",
            "Close the position, or switch the subscription back on.",
        ),
        (
            "Cleared: RELIANCE is holding a position nobody is watching",
            "It is no longer happening.",
        ),
    ]
    assert [c[3] for c in sent] == [{"severity": "critical"}, None]
    (row,) = await _all(db_session_factory)
    assert row.resolution_notified_at == T0 + timedelta(minutes=3)


async def test_a_condition_that_comes_back_is_announced_again(db_session_factory, sent):
    async with db_session_factory() as db:
        for minute, present in ((0, True), (1, False), (2, True)):
            await sync_watched(
                db,
                [_unwatched()] if present else [],
                scope=BOTH,
                confirm_seconds=CONFIRM,
                now=T0 + timedelta(minutes=minute),
            )

    titles = [c[1] for c in sent]
    assert titles == [
        "RELIANCE is holding a position nobody is watching",
        "Cleared: RELIANCE is holding a position nobody is watching",
        "RELIANCE is holding a position nobody is watching",
    ]
    assert len(await _all(db_session_factory)) == 2


async def test_conditions_that_open_together_are_one_message(db_session_factory, sent):
    async with db_session_factory() as db:
        opened = await sync_watched(
            db,
            [_unwatched("a", "AAA"), _unwatched("b", "BBB"), _unwatched("c", "CCC")],
            scope=BOTH,
            confirm_seconds=CONFIRM,
            now=T0,
        )
        await sync_watched(
            db, [], scope=BOTH, confirm_seconds=CONFIRM, now=T0 + timedelta(minutes=1)
        )

    assert (opened.opened, opened.announced, opened.resolved) == (3, 3, 0)
    assert len(sent) == 2
    assert sent[0][1] == "3 things need attention"
    assert sent[0][2].splitlines() == [
        "- AAA is holding a position nobody is watching",
        "- BBB is holding a position nobody is watching",
        "- CCC is holding a position nobody is watching",
    ]
    assert sent[0][3] == {"worst severity": "critical"}
    assert sent[1][1] == "3 alerts cleared"
    assert sent[1][2].splitlines() == sent[0][2].splitlines()  # the same three, listed again
    assert sent[1][3] is None  # nothing to add under a cleared notice


async def test_the_worst_severity_heads_a_mixed_message(db_session_factory, sent):
    async with db_session_factory() as db:
        await sync_watched(
            db,
            [_halted(), _unwatched()],
            scope=BOTH,
            confirm_seconds={"critical": 0.0, "warning": 0.0},
            now=T0,
        )

    assert sent[0][3] == {"worst severity": "critical"}


async def test_outside_market_hours_only_critical_conditions_are_judged(db_session_factory, sent):
    async with db_session_factory() as db:
        # In the session a warning opens and is announced.
        await sync_watched(
            db, [_halted()], scope=BOTH, confirm_seconds={"critical": 0.0, "warning": 0.0}, now=T0
        )
        # After the close the same look is made for critical conditions only: the warning is
        # neither opened again nor "cleared" just because it is not being looked for.
        result = await sync_watched(
            db,
            [],
            scope=CRITICAL_ONLY,
            confirm_seconds=CONFIRM,
            now=T0 + timedelta(hours=7),
        )
        # And a warning offered out of hours is not opened.
        ignored = await sync_watched(
            db,
            [_halted("s9", "ZED")],
            scope=CRITICAL_ONLY,
            confirm_seconds=CONFIRM,
            now=T0 + timedelta(hours=7),
        )

    assert (result.resolved, ignored.opened) == (0, 0)
    assert [c[1] for c in sent] == ["TCS is not trading"]
    rows = await _all(db_session_factory)
    assert [(r.key, r.resolved_at) for r in rows] == [("s2:halted", None)]


async def test_a_critical_condition_is_judged_out_of_hours_too(db_session_factory, sent):
    async with db_session_factory() as db:
        await sync_watched(db, [_unwatched()], scope=CRITICAL_ONLY, confirm_seconds=CONFIRM, now=T0)
        result = await sync_watched(
            db, [], scope=CRITICAL_ONLY, confirm_seconds=CONFIRM, now=T0 + timedelta(hours=1)
        )

    assert result.resolved == 1
    assert len(sent) == 2


async def test_an_unknown_severity_has_no_waiting_time(db_session_factory, sent):
    # `confirm_seconds` names only the severities the watcher raises; others are not held back.
    info = AlertCandidate("s3:history", "history", "info", "TCS is waiting", "x")
    async with db_session_factory() as db:
        result = await sync_watched(
            db, [info], scope=frozenset({"info"}), confirm_seconds={}, now=T0
        )

    assert result.announced == 1


# ---- conditions and events raised where they happen ----


async def test_a_reported_condition_is_announced_once_and_cleared_when_it_stops(
    db_session_factory, sent
):
    roll = AlertCandidate(
        "roll:s1", "roll_postponed", "warning", "Roll of NIFTY26NOVFUT postponed", "no fresh tick"
    )
    async with db_session_factory() as db:
        await report_condition(db, "event", roll, now=T0)
        await report_condition(db, "event", roll, now=T0 + timedelta(minutes=10))
        await report_condition(db, "event", roll, now=T0 + timedelta(minutes=20))
        cleared = await clear_conditions(db, prefix="roll:", now=T0 + timedelta(minutes=30))
        await db.commit()

    assert cleared == 1
    assert [c[1] for c in sent] == [
        "Roll of NIFTY26NOVFUT postponed",
        "Cleared: Roll of NIFTY26NOVFUT postponed",
    ]
    (row,) = await _all(db_session_factory)
    assert row.source == "event"
    assert row.last_seen_at == T0 + timedelta(minutes=30)


async def test_clearing_keeps_what_is_still_true_and_leaves_other_prefixes_alone(
    db_session_factory, sent
):
    def roll(n):
        return AlertCandidate(f"roll:{n}", "roll_postponed", "warning", f"roll {n}", "")

    stop = AlertCandidate("expiry:x", "expiry_postponed", "warning", "expiry x", "")
    async with db_session_factory() as db:
        for candidate in (roll(1), roll(2), stop):
            await report_condition(db, "event", candidate, now=T0)
        cleared = await clear_conditions(
            db, prefix="roll:", keep=frozenset({"roll:2"}), now=T0 + timedelta(minutes=1)
        )
        await db.commit()

    assert cleared == 1
    rows = {r.key: r.resolved_at for r in await _all(db_session_factory)}
    assert rows == {"roll:1": T0 + timedelta(minutes=1), "roll:2": None, "expiry:x": None}


async def test_an_event_opens_and_resolves_in_the_same_instant_and_is_announced(
    db_session_factory, sent
):
    stop = AlertCandidate(
        f"stop_loss:{uuid.uuid4()}", "stop_loss", "warning", "TCS: stop-loss hit", "Sold 10"
    )
    async with db_session_factory() as db:
        row = await record_event(db, stop, now=T0)
        # Sent before the call returns: only the *_safely form sends in the background.
        assert [c[1] for c in sent] == ["TCS: stop-loss hit"]
        await db.commit()

    assert (row.opened_at, row.resolved_at, row.notified_at) == (T0, T0, T0)
    assert row.source == "event"
    assert [c[1] for c in sent] == ["TCS: stop-loss hit"]
    # Over at once, so a resolved event never sends a "cleared" notice.
    assert row.resolution_notified_at is None


async def test_the_same_kind_of_event_can_happen_twice(db_session_factory, sent):
    async with db_session_factory() as db:
        for n in range(2):
            await record_event(
                db,
                AlertCandidate(f"stop_loss:{n}", "stop_loss", "warning", "TCS stopped", ""),
                now=T0,
            )
        await db.commit()

    assert len(await _all(db_session_factory)) == 2
    assert len(sent) == 2


async def test_bookkeeping_that_fails_is_swallowed_where_it_must_not_break_a_fill(
    db_session_factory, monkeypatch
):
    async def exploding_record(*_a, **_k):
        raise RuntimeError("could not write the row")

    monkeypatch.setattr(engine_alerts, "record_event", exploding_record)

    async with db_session_factory() as db:
        await record_event_safely(
            db, AlertCandidate("stop_loss:1", "stop_loss", "warning", "TCS stopped", "")
        )
        # The session is usable afterwards.
        assert (await db.execute(select(EngineAlertEvent))).scalars().all() == []


async def test_a_message_that_cannot_be_sent_does_not_lose_the_record(
    db_session_factory, monkeypatch
):
    async def exploding_notify(*_a, **_k):
        raise RuntimeError("channel store on fire")

    monkeypatch.setattr(engine_alerts, "notify", exploding_notify)

    async with db_session_factory() as db:
        await record_event_safely(
            db, AlertCandidate("stop_loss:1", "stop_loss", "warning", "TCS stopped", "")
        )
        await wait_for_announcements()  # the send fails in the background, quietly

    [row] = await _all(db_session_factory)
    assert row.title == "TCS stopped"


async def test_a_slow_chat_service_does_not_hold_up_the_caller(db_session_factory, monkeypatch):
    release = asyncio.Event()
    delivered = []

    async def slow_notify(level, *, title, body, details=None):
        await release.wait()  # a chat service that is taking its time
        delivered.append(title)
        return []

    monkeypatch.setattr(engine_alerts, "notify", slow_notify)

    async with db_session_factory() as db:
        await asyncio.wait_for(
            record_event_safely(
                db, AlertCandidate("stop_loss:1", "stop_loss", "warning", "TCS stopped", "")
            ),
            timeout=5,
        )  # returned although the message has not gone out
        assert delivered == []
        release.set()
        await wait_for_announcements()

    assert delivered == ["TCS stopped"]
    [row] = await _all(db_session_factory)
    assert row.notified_at is not None


async def test_a_job_syncs_its_conditions_in_one_call(db_session_factory, sent):
    def roll(n):
        return AlertCandidate(f"roll:{n}", "roll_postponed", "warning", f"roll {n}", "")

    async with db_session_factory() as db:
        await sync_conditions_safely(db, source="event", prefix="roll:", present=[roll(1), roll(2)])
    async with db_session_factory() as db:
        await sync_conditions_safely(db, source="event", prefix="roll:", present=[roll(2)])
    async with db_session_factory() as db:
        await sync_conditions_safely(db, source="event", prefix="roll:", present=[])

    rows = {r.key: r.resolved_at is not None for r in await _all(db_session_factory)}
    assert rows == {"roll:1": True, "roll:2": True}
    # roll 1 and roll 2 announced separately (two calls' worth), then each cleared once.
    assert sorted(c[1] for c in sent) == [
        "Cleared: roll 1",
        "Cleared: roll 2",
        "roll 1",
        "roll 2",
    ]


async def test_a_long_title_is_cut_to_what_the_column_holds(db_session_factory, sent):
    long_title = "x" * 500
    async with db_session_factory() as db:
        await record_event(db, AlertCandidate("k:1", "k", "info", long_title, ""), now=T0)
        await db.commit()

    (row,) = await _all(db_session_factory)
    assert len(row.title) == 300


# ---- when the bookkeeping itself goes wrong ----


async def test_a_commit_that_fails_is_logged_and_does_not_reach_the_caller(
    db_session_factory, sent, monkeypatch
):
    async with db_session_factory() as db:

        async def refuse():
            raise RuntimeError("connection lost")

        monkeypatch.setattr(db, "commit", refuse)

        await record_event_safely(
            db, AlertCandidate("stop_loss:9", "stop_loss", "warning", "TCS stopped", "")
        )
        await wait_for_announcements()

    assert [c[1] for c in sent] == ["TCS stopped"]  # it was announced; only the save failed


async def test_a_job_whose_alert_bookkeeping_fails_carries_on(db_session_factory, monkeypatch):
    async def exploding_notify(*_a, **_k):
        raise RuntimeError("channel store on fire")

    monkeypatch.setattr(engine_alerts, "notify", exploding_notify)
    roll = AlertCandidate("roll:1", "roll_postponed", "warning", "roll 1", "")

    async with db_session_factory() as db:
        await sync_conditions_safely(db, source="event", prefix="roll:", present=[roll])
        assert (await db.execute(select(EngineAlertEvent))).scalars().all() == []


async def test_a_conflict_with_nothing_to_find_afterwards_is_not_hidden(
    db_session_factory, monkeypatch
):
    from sqlalchemy.exc import IntegrityError

    async with db_session_factory() as db:
        await open_or_refresh(db, "watcher", _halted(), now=T0)
        await db.commit()

    async def sees_nothing(db, key):
        return None

    monkeypatch.setattr(engine_alerts, "open_episode", sees_nothing)
    async with db_session_factory() as db:
        with pytest.raises(IntegrityError):
            await open_or_refresh(db, "watcher", _halted(), now=T0)


# ---- details that are easy to get subtly wrong ----


async def test_the_worst_severity_of_a_warning_and_an_info_is_the_warning(db_session_factory, sent):
    note = AlertCandidate("s3:history", "history", "info", "TCS is waiting", "x")
    async with db_session_factory() as db:
        await sync_watched(
            db,
            [note, _halted()],
            scope=frozenset({"critical", "warning", "info"}),
            confirm_seconds={"critical": 0.0, "warning": 0.0, "info": 0.0},
            now=T0,
        )

    assert sent[0][3] == {"worst severity": "warning"}


def test_a_candidate_has_no_detail_and_no_subscription_unless_given_and_cannot_be_changed():
    from dataclasses import FrozenInstanceError

    candidate = AlertCandidate("k", "kind", "info", "title")

    assert (candidate.detail, candidate.subscription_id) == ("", None)
    with pytest.raises(FrozenInstanceError):
        candidate.title = "other"  # type: ignore[misc]
    assert not hasattr(candidate, "__dict__")


def test_what_a_look_did_is_a_value_that_cannot_be_changed():
    from dataclasses import FrozenInstanceError

    from src.orchestration.engine_alerts import WatchResult

    result = WatchResult(opened=1, announced=2, resolved=3)

    assert (result.opened, result.announced, result.resolved) == (1, 2, 3)
    with pytest.raises(FrozenInstanceError):
        result.opened = 9  # type: ignore[misc]
    assert not hasattr(result, "__dict__")


async def test_the_episode_remembers_which_subscription_it_is_about(db_session_factory, sent):
    from src.orchestration.paper_trading import enroll_in_paper_trading
    from src.orchestration.strategies import create_strategy, create_version_with_validation

    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="Alerts", objective="o")
        version = await create_version_with_validation(
            db, strategy, "def run_backtest(data, config):\n    return {}\n"
        )
        subscription = await enroll_in_paper_trading(
            db, strategy_version_id=version.id, symbol="TCS", builtin_strategy="sma_crossover"
        )
        about_it = AlertCandidate(
            "k", "halted", "warning", "TCS is not trading", "why", subscription.id
        )
        await open_or_refresh(db, "watcher", about_it, now=T0)
        await record_event(
            db, AlertCandidate("e", "stop_loss", "warning", "TCS", "", subscription.id)
        )
        await db.commit()

    rows = await _all(db_session_factory)
    assert {r.key: r.subscription_id for r in rows} == {"k": subscription.id, "e": subscription.id}


async def test_a_refreshed_episode_keeps_its_title_within_what_the_column_holds(
    db_session_factory,
):
    async with db_session_factory() as db:
        await open_or_refresh(db, "watcher", _halted(), now=T0)
        longer = AlertCandidate("s2:halted", "halted", "warning", "y" * 500, "d")
        await open_or_refresh(db, "watcher", longer, now=T0 + timedelta(minutes=1))
        await db.commit()

    (row,) = await _all(db_session_factory)
    assert row.title == "y" * 300


async def test_the_loser_of_a_race_marks_the_winners_episode_as_seen(db_session_factory):
    async with db_session_factory() as winner_db:
        await open_or_refresh(winner_db, "watcher", _halted(), now=T0)
        async with db_session_factory() as loser_db:
            loser = asyncio.create_task(
                open_or_refresh(loser_db, "watcher", _halted(), now=T0 + timedelta(minutes=2))
            )
            await asyncio.sleep(0.2)
            await winner_db.commit()
            await loser
            await loser_db.commit()

    (row,) = await _all(db_session_factory)
    assert row.last_seen_at == T0 + timedelta(minutes=2)


async def test_episodes_are_counted_by_who_raised_them_and_how_serious(db_session_factory, sent):
    from src.observability.metrics import engine_alerts_opened_total as opened

    def count(source, severity):
        return opened.labels(source=source, severity=severity)._value.get()

    before = {
        key: count(*key)
        for key in (("watcher", "critical"), ("event", "warning"), ("alertmanager", "info"))
    }
    async with db_session_factory() as db:
        await open_or_refresh(db, "watcher", _unwatched(), now=T0)
        await open_or_refresh(db, "watcher", _unwatched(), now=T0)  # the same episode: not again
        await record_event(db, AlertCandidate("e:1", "stop_loss", "warning", "TCS", ""), now=T0)
        await open_or_refresh(
            db, "alertmanager", AlertCandidate("am:1", "X", "info", "X", ""), now=T0
        )
        await db.commit()

    for key, was in before.items():
        assert count(*key) == was + 1, key


async def test_a_message_sent_in_the_background_says_the_same_as_one_sent_in_line(
    db_session_factory, sent
):
    from src.orchestration import engine_alerts as module

    async with db_session_factory() as db:
        await record_event_safely(
            db, AlertCandidate("stop_loss:7", "stop_loss", "warning", "TCS stopped", "Sold 10")
        )
        await wait_for_announcements()

    assert sent == [(AlertLevel.ENGINE, "TCS stopped", "Sold 10", {"severity": "warning"})]
    assert not module._announcing  # finished messages are not kept
