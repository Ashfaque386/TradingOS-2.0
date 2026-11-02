"""What a scheduled job's schedule may be changed to, and the code's own schedule it can go back to.

The rules are the ones the route has always had (an interval job takes `seconds`; a cron job takes
`hour` and `minute` and keeps every other field of its expression), now stated once so the route and
the restart apply the same ones. No database here: reading and saving the stored choice is
`src.orchestration.job_schedule_store`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

TriggerKind = Literal["interval", "cron"]


class ScheduleRefused(ValueError):
    """The schedule asked for cannot be used; the message says why."""


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """A schedule to set: `seconds` for an interval job, `hour` and `minute` for a cron one."""

    seconds: int | None = None
    hour: int | None = None
    minute: int | None = None


def trigger_kind(trigger: BaseTrigger) -> TriggerKind:
    if isinstance(trigger, IntervalTrigger):
        return "interval"
    if isinstance(trigger, CronTrigger):
        return "cron"
    raise ScheduleRefused(f"Unsupported trigger type: {type(trigger).__name__}")


def check_schedule(kind: TriggerKind, spec: ScheduleSpec) -> ScheduleSpec:
    """Returns `spec` if it fits a job of this kind; otherwise raises `ScheduleRefused`."""
    if kind == "interval":
        if spec.hour is not None or spec.minute is not None:
            raise ScheduleRefused(
                "This job runs on a fixed interval; supply 'seconds', not hour/minute."
            )
        if spec.seconds is None or spec.seconds < 1:
            raise ScheduleRefused("seconds must be a positive integer")
        return spec
    if spec.seconds is not None:
        raise ScheduleRefused("This job runs on a cron schedule; supply hour/minute, not seconds.")
    if spec.hour is None or spec.minute is None:
        raise ScheduleRefused("Both hour and minute are required")
    if not (0 <= spec.hour <= 23) or not (0 <= spec.minute <= 59):
        raise ScheduleRefused("hour must be 0-23 and minute must be 0-59")
    return spec


def trigger_for(current: BaseTrigger, spec: ScheduleSpec) -> BaseTrigger:
    """The trigger a job would run on with `spec`: a cron job keeps every other field of its
    expression (day of the week, month...), so a change of hour cannot drop `day_of_week="mon"`."""
    kind = trigger_kind(current)
    check_schedule(kind, spec)
    if kind == "interval":
        assert spec.seconds is not None
        return IntervalTrigger(seconds=spec.seconds)
    assert isinstance(current, CronTrigger) and spec.hour is not None and spec.minute is not None
    fields: dict[str, str | int] = {field.name: str(field) for field in current.fields}
    fields["hour"] = spec.hour
    fields["minute"] = spec.minute
    return CronTrigger(**fields, timezone=current.timezone)


# The schedule each job was registered with by the code, remembered before any stored choice is
# applied, so "restore the default" has something to restore.
_DEFAULTS: dict[tuple[str, str], BaseTrigger] = {}


def remember_defaults(schedulers: Mapping[str, AsyncIOScheduler]) -> None:
    """Records every job's current trigger as its default. Call it once the schedulers are
    registered and before `apply_stored_overrides`; a job already remembered keeps its first."""
    for name, scheduler in schedulers.items():
        for job in scheduler.get_jobs():
            _DEFAULTS.setdefault((name, job.id), job.trigger)


def remember_default(scheduler_name: str, job_id: str, trigger: BaseTrigger) -> BaseTrigger:
    """The job's default, remembering `trigger` as it if none is recorded yet."""
    return _DEFAULTS.setdefault((scheduler_name, job_id), trigger)


def default_for(scheduler_name: str, job_id: str) -> BaseTrigger | None:
    return _DEFAULTS.get((scheduler_name, job_id))


def forget_defaults() -> None:
    _DEFAULTS.clear()
