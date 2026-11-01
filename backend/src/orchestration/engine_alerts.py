"""Telling a person when the trading engine needs one (the `engine` alert level).

Until now a position nobody was watching, a price feed that had stopped, or a futures roll that
could not be done was visible only to someone looking at the Paper Desk. Everything here turns
those into an *episode* (src.models.engine_alert_event): it opens when the condition is first
seen, the subscribed channels are told once, and it resolves when the condition clears -- with a
"cleared" notice if it had been announced. A condition that comes back is a new episode and is
announced again, so "one alert per episode" and "alert again on recurrence" are the same rule.

Three things raise one:

- the **watcher** (src.orchestration.engine_alert_watcher): the Paper Desk's attention rules,
  checked every minute, so a warning must last `ENGINE_ALERT_CONFIRM_SECONDS` before it is
  announced (a price that is late for one poll messages no one) and a critical one is announced at
  once;
- an **event**: something the engine did or could not do at a moment -- a stop-loss exit, a futures
  roll or an expiry square-off that was postponed, a position closed at the last quote after
  expiry;
- **Alertmanager**: a Prometheus rule firing or resolving, posted to the webhook
  (src.api.routes.alertmanager_webhook).

Delivery is `notify()` (src.notifications.dispatch), which never raises and skips a channel that
is not subscribed to `engine`. The episode row is the durable record either way: the console's
alerts panel and the top-bar count read it, so an alert is not lost with the message that
announced it.

The `*_safely` forms are for call sites inside the trading flow: bookkeeping about an alert must
never be the reason an order or a roll fails, so they use a savepoint, log and swallow. They also
send the message in the background: the tick drain job handles every subscription in turn, and a
slow chat service (each send may take ten seconds to time out) must not hold up the next
subscription's stop-loss.
"""

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel
from src.observability.metrics import engine_alerts_opened_total

logger = structlog.get_logger(__name__)

Source = Literal["watcher", "event", "alertmanager"]
Severity = Literal["critical", "warning", "info"]

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_TITLE_MAX = 300


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    """A condition that needs a person. `key` names it: the same key on a later look is the same
    episode, a different key a different one."""

    key: str
    kind: str
    severity: Severity
    title: str
    detail: str = ""
    subscription_id: uuid.UUID | None = None


# Messages sent in the background, kept so they are not collected before they finish.
_announcing: set["asyncio.Task[None]"] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def _fresh_row(source: Source, candidate: AlertCandidate, now: datetime) -> EngineAlertEvent:
    return EngineAlertEvent(
        key=candidate.key,
        source=source,
        kind=candidate.kind,
        severity=candidate.severity,
        title=candidate.title[:_TITLE_MAX],
        detail=candidate.detail,
        subscription_id=candidate.subscription_id,
        opened_at=now,
        last_seen_at=now,
    )


async def open_episode(db: AsyncSession, key: str) -> EngineAlertEvent | None:
    return (
        await db.execute(
            select(EngineAlertEvent).where(
                EngineAlertEvent.key == key, EngineAlertEvent.resolved_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def open_or_refresh(
    db: AsyncSession, source: Source, candidate: AlertCandidate, *, now: datetime
) -> tuple[EngineAlertEvent, bool]:
    """The open episode for this condition (its wording refreshed), or a new one. The flag says
    whether it is new. At most one episode per key is open (a unique index), so a second writer
    that loses the race finds the winner's row instead of failing."""
    existing = await open_episode(db, candidate.key)
    if existing is not None:
        existing.last_seen_at = now
        existing.severity = candidate.severity
        existing.title = candidate.title[:_TITLE_MAX]
        existing.detail = candidate.detail
        return existing, False
    row = _fresh_row(source, candidate, now)
    try:
        async with db.begin_nested():
            db.add(row)
    except IntegrityError:
        winner = await open_episode(db, candidate.key)
        if winner is None:  # resolved between the two reads: take the next look to open it
            raise
        winner.last_seen_at = now
        return winner, False
    engine_alerts_opened_total.labels(source=source, severity=candidate.severity).inc()
    return row, True


def resolve_row(row: EngineAlertEvent, *, now: datetime) -> None:
    row.resolved_at = now
    row.last_seen_at = now


def _opened_message(rows: Sequence[EngineAlertEvent]) -> tuple[str, str, dict[str, object]]:
    worst = min(rows, key=lambda r: SEVERITY_RANK[r.severity]).severity
    if len(rows) == 1:
        row = rows[0]
        return row.title, row.detail, {"severity": row.severity}
    body = "\n".join(f"- {row.title}" for row in rows)
    return f"{len(rows)} things need attention", body, {"worst severity": worst}


def _resolved_message(rows: Sequence[EngineAlertEvent]) -> tuple[str, str, dict[str, object]]:
    if len(rows) == 1:
        return f"Cleared: {rows[0].title}", "It is no longer happening.", {}
    return (
        f"{len(rows)} alerts cleared",
        "\n".join(f"- {row.title}" for row in rows),
        {},
    )


async def announce_opened(rows: Sequence[EngineAlertEvent], *, now: datetime) -> None:
    """One message for however many episodes opened together (a feed that fails stops six
    subscriptions at once; that is one message, not six). Marks them announced."""
    if not rows:
        return
    title, body, details = _opened_message(rows)
    await notify(AlertLevel.ENGINE, title=title, body=body, details=details or None)
    for row in rows:
        row.notified_at = now


async def announce_resolved(rows: Sequence[EngineAlertEvent], *, now: datetime) -> None:
    """The "cleared" notice, only for episodes that were announced when they opened."""
    announced = [row for row in rows if row.notified_at is not None]
    if not announced:
        return
    title, body, details = _resolved_message(announced)
    await notify(AlertLevel.ENGINE, title=title, body=body, details=details or None)
    for row in announced:
        row.resolution_notified_at = now


def _announce_in_background(rows: Sequence[EngineAlertEvent]) -> None:
    """Sends the opening notice without making the caller wait for it."""
    title, body, details = _opened_message(rows)

    async def send() -> None:
        try:
            await notify(AlertLevel.ENGINE, title=title, body=body, details=details or None)
        except Exception:  # noqa: BLE001 - `notify` does not raise; this is for what it calls
            logger.exception("engine_alerts.background_announce_failed", title=title)

    task = asyncio.get_running_loop().create_task(send())
    _announcing.add(task)
    task.add_done_callback(_announcing.discard)


async def wait_for_announcements() -> None:
    """Returns once every message sent in the background has finished (for tests, and a clean
    shutdown)."""
    if _announcing:
        await asyncio.gather(*list(_announcing), return_exceptions=True)


# ---- conditions the watcher looks at on a schedule ----


@dataclass(frozen=True, slots=True)
class WatchResult:
    opened: int
    announced: int
    resolved: int


async def sync_watched(
    db: AsyncSession,
    candidates: Iterable[AlertCandidate],
    *,
    scope: frozenset[str],
    confirm_seconds: dict[str, float],
    now: datetime | None = None,
) -> WatchResult:
    """Brings the open `watcher` episodes in line with what is true now.

    `scope` is the severities this look is entitled to speak for: outside market hours only the
    critical ones are judged, so a warning that is only meaningful while the market is open is
    neither opened nor cleared overnight. A candidate opens an episode (or refreshes its open
    one); an open episode in scope that is no longer a candidate resolves. An episode is announced
    once it has lasted `confirm_seconds[severity]` (0 for a critical one).
    """
    now = now or _now()
    wanted = {c.key: c for c in candidates if c.severity in scope}

    open_rows = (
        (
            await db.execute(
                select(EngineAlertEvent).where(
                    EngineAlertEvent.source == "watcher",
                    EngineAlertEvent.resolved_at.is_(None),
                    EngineAlertEvent.severity.in_(scope),
                )
            )
        )
        .scalars()
        .all()
    )
    open_by_key = {row.key: row for row in open_rows}

    opened = 0
    for key, candidate in wanted.items():
        row, created = await open_or_refresh(db, "watcher", candidate, now=now)
        open_by_key[key] = row
        opened += int(created)

    due = [
        row
        for key, row in open_by_key.items()
        if key in wanted
        and row.notified_at is None
        and (now - row.opened_at).total_seconds() >= confirm_seconds.get(row.severity, 0.0)
    ]
    await announce_opened(due, now=now)

    gone = [row for key, row in open_by_key.items() if key not in wanted]
    for row in gone:
        resolve_row(row, now=now)
    await announce_resolved(gone, now=now)

    await db.commit()
    return WatchResult(opened=opened, announced=len(due), resolved=len(gone))


# ---- conditions and events raised where they happen ----


async def report_condition(
    db: AsyncSession, source: Source, candidate: AlertCandidate, *, now: datetime | None = None
) -> EngineAlertEvent:
    """A condition seen at a moment, announced at once if it is new (a postponed roll is retried
    every ten minutes; the first failure is the news). Does not commit."""
    now = now or _now()
    row, created = await open_or_refresh(db, source, candidate, now=now)
    if created:
        await announce_opened([row], now=now)
    return row


async def clear_conditions(
    db: AsyncSession,
    *,
    prefix: str,
    keep: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> int:
    """Resolves the open episodes whose key starts with `prefix` and is not in `keep` (the roll
    went through, the position was closed). Does not commit. Returns how many resolved."""
    now = now or _now()
    rows = (
        (
            await db.execute(
                select(EngineAlertEvent).where(
                    EngineAlertEvent.resolved_at.is_(None),
                    EngineAlertEvent.key.startswith(prefix),
                )
            )
        )
        .scalars()
        .all()
    )
    gone = [row for row in rows if row.key not in keep]
    for row in gone:
        resolve_row(row, now=now)
    await announce_resolved(gone, now=now)
    return len(gone)


async def record_event(
    db: AsyncSession,
    candidate: AlertCandidate,
    *,
    now: datetime | None = None,
    in_background: bool = False,
) -> EngineAlertEvent:
    """Something that happened and is over: it opens and resolves in the same instant, and is
    announced at once (in the background if `in_background`, so the caller is not kept waiting for
    a chat service). Does not commit."""
    now = now or _now()
    row = _fresh_row("event", candidate, now)
    row.resolved_at = now
    async with db.begin_nested():
        db.add(row)
    engine_alerts_opened_total.labels(source="event", severity=candidate.severity).inc()
    if in_background:
        row.notified_at = now
        _announce_in_background([row])
    else:
        await announce_opened([row], now=now)
    return row


async def record_event_safely(db: AsyncSession, candidate: AlertCandidate) -> None:
    """`record_event` for a call site in the trading flow: commits, and never raises. The work is
    done inside a savepoint, so if it fails only the note about the alert is undone: the fill the
    caller has just committed, and the objects it is still holding, are untouched."""
    try:
        async with db.begin_nested():
            await record_event(db, candidate, in_background=True)
    except Exception:  # noqa: BLE001 - an alert about a fill must never undo or fail the fill
        logger.exception("engine_alerts.record_event_failed", key=candidate.key)
        return
    await _commit_quietly(db)


async def _commit_quietly(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 - see above: bookkeeping must not break the caller
        logger.exception("engine_alerts.commit_failed")


async def sync_conditions_safely(
    db: AsyncSession,
    *,
    source: Source,
    prefix: str,
    present: Sequence[AlertCandidate],
) -> None:
    """For a job that knows which of its conditions are true right now (the futures roll, the
    expiry sweep): opens the new ones, resolves the ones of this `prefix` that are no longer.
    Commits, and never raises."""
    try:
        async with db.begin_nested():
            for candidate in present:
                await report_condition(db, source, candidate)
            await clear_conditions(db, prefix=prefix, keep=frozenset(c.key for c in present))
    except Exception:  # noqa: BLE001 - bookkeeping about an alert must not break the job
        logger.exception("engine_alerts.sync_failed", prefix=prefix)
        return
    await _commit_quietly(db)
