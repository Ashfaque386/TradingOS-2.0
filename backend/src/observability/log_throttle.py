"""Keeps one persistently failing scheduled job from flooding the log.

APScheduler logs a full traceback (`Job "..." raised an exception`) on every
failed firing. A job that fires every 3 seconds against a broker that has
rejected its credentials therefore wrote ~1,900 identical tracebacks in about
an hour and a half, burying everything else. The job itself still fails each time -- on
purpose: `scheduled_job_history` records every firing's real outcome, and a
job that quietly "succeeded" while doing nothing would make that history lie --
so the throttle sits on the *log record*, not the job: the first failure of a
given kind is logged in full, identical repeats inside the window are dropped
and counted, and the next one to get through says how many were left out.
"""

import logging
import re
import time
from collections.abc import Callable

# APScheduler's executor logger (the "default" executor every scheduler here uses).
EXECUTOR_LOGGER_NAME = "apscheduler.executors.default"
DEFAULT_WINDOW_SECONDS = 60.0
_MAX_TRACKED_KINDS = 256

# The job's repr ends in "(trigger: ..., next run at: <timestamp>)", and the
# timestamp changes every firing; strip it so repeats compare equal.
_NEXT_RUN = re.compile(r"next run at: [^)]*")


class RepeatedJobFailureFilter(logging.Filter):
    """Passes the first record of each (job, exception type, message) and then
    at most one more per `window_seconds`, annotated with the number dropped
    in between. Records without an exception are never touched."""

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._window = window_seconds
        self._clock = clock
        self._last_logged: dict[tuple[str, str, str], float] = {}
        self._dropped: dict[tuple[str, str, str], int] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.exc_info or record.exc_info[1] is None:
            return True
        exc = record.exc_info[1]
        key = (
            _NEXT_RUN.sub("", record.getMessage()),
            type(exc).__name__,
            str(exc)[:300],
        )
        now = self._clock()
        last = self._last_logged.get(key)
        if last is not None and now - last < self._window:
            self._dropped[key] = self._dropped.get(key, 0) + 1
            return False

        dropped = self._dropped.pop(key, 0)
        if dropped:
            record.msg = (
                f"{record.msg} [{dropped} identical failures since the last report not logged]"
            )
        if len(self._last_logged) >= _MAX_TRACKED_KINDS:
            self._last_logged.clear()
            self._dropped.clear()
        self._last_logged[key] = now
        return True


def install_scheduler_log_throttle() -> RepeatedJobFailureFilter:
    """Attach one throttle to APScheduler's executor logger (idempotent)."""
    logger = logging.getLogger(EXECUTOR_LOGGER_NAME)
    for existing in logger.filters:
        if isinstance(existing, RepeatedJobFailureFilter):
            return existing
    throttle = RepeatedJobFailureFilter()
    logger.addFilter(throttle)
    return throttle
