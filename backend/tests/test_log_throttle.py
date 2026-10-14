"""The scheduler log throttle: one persistently failing job must not bury the log.

The live stack wrote ~1,900 identical tracebacks in about an hour and a half from a single
3-second job. The throttle lets the first of each kind through in full, drops
identical repeats inside a window, and tells the next one that got through how
many were left out -- without ever touching a record that has no exception.
"""

import logging

import pytest

from src.observability.log_throttle import (
    EXECUTOR_LOGGER_NAME,
    RepeatedJobFailureFilter,
    install_scheduler_log_throttle,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _failure(
    job: str = "tick_publish (trigger: interval[0:00:03], next run at: {at})",
    *,
    at="T1",
    exc: Exception | None = None,
    level: int = logging.ERROR,
) -> logging.LogRecord:
    exc = exc or RuntimeError("zerodha rejected the stored credentials (HTTP 403)")
    try:
        raise exc
    except Exception:  # noqa: BLE001 - building a record with a real traceback
        import sys

        exc_info = sys.exc_info()
    return logging.LogRecord(
        "apscheduler.executors.default",
        level,
        __file__,
        1,
        'Job "%s" raised an exception',
        (job.format(at=at),),
        exc_info,
    )


def test_the_first_failure_passes_and_identical_repeats_are_dropped():
    clock = _Clock()
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=clock)

    assert throttle.filter(_failure(at="14:09:40")) is True
    for second in range(3, 60, 3):  # the job fires every 3 seconds
        clock.now = second
        assert (
            throttle.filter(_failure(at=f"14:09:{second}")) is False
        )  # only the timestamp differs


def test_after_the_window_one_more_gets_through_and_says_how_many_were_dropped():
    clock = _Clock()
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=clock)
    throttle.filter(_failure())
    for second in range(3, 60, 3):
        clock.now = second
        throttle.filter(_failure())  # 19 dropped

    clock.now = 61
    record = _failure()
    assert throttle.filter(record) is True
    assert "[19 identical failures since the last report not logged]" in record.msg

    clock.now = 70
    assert throttle.filter(_failure()) is False  # and a new window begins


def test_a_different_error_or_a_different_job_is_a_different_kind():
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=_Clock())

    assert throttle.filter(_failure()) is True
    assert throttle.filter(_failure(exc=ValueError("something else"))) is True
    assert (
        throttle.filter(_failure(job="tick_drain (trigger: interval[0:00:05], next run at: {at})"))
        is True
    )
    # The same message from a different exception type is a different failure too.
    same_text = "zerodha rejected the stored credentials (HTTP 403)"
    assert throttle.filter(_failure(exc=ValueError(same_text))) is True
    # ...while each of those is individually throttled afterwards.
    assert throttle.filter(_failure(exc=ValueError("something else"))) is False


def test_records_without_an_exception_are_never_touched():
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=_Clock())
    plain = logging.LogRecord("apscheduler", logging.INFO, __file__, 1, "Added job", (), None)

    for _ in range(5):
        assert throttle.filter(plain) is True


def test_the_table_of_remembered_kinds_stays_bounded():
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=_Clock())

    for i in range(1000):
        throttle.filter(_failure(exc=RuntimeError(f"distinct {i}")))

    assert len(throttle._last_logged) <= 256


def test_installing_twice_attaches_one_filter_to_the_executor_logger():
    logger = logging.getLogger(EXECUTOR_LOGGER_NAME)
    before = [f for f in logger.filters if isinstance(f, RepeatedJobFailureFilter)]
    try:
        first = install_scheduler_log_throttle()
        second = install_scheduler_log_throttle()

        assert first is second
        ours = [f for f in logger.filters if isinstance(f, RepeatedJobFailureFilter)]
        assert len(ours) == max(1, len(before))
    finally:
        for f in list(logger.filters):
            if isinstance(f, RepeatedJobFailureFilter) and f not in before:
                logger.removeFilter(f)


def test_through_the_real_logger_only_the_first_failure_is_emitted(
    caplog: pytest.LogCaptureFixture,
):
    logger = logging.getLogger(EXECUTOR_LOGGER_NAME)
    throttle = RepeatedJobFailureFilter(window_seconds=60, clock=_Clock())
    logger.addFilter(throttle)
    try:
        with caplog.at_level(logging.ERROR, logger=EXECUTOR_LOGGER_NAME):
            for _ in range(20):
                try:
                    raise RuntimeError("zerodha rejected the stored credentials (HTTP 403)")
                except RuntimeError:
                    logger.exception(
                        'Job "%s" raised an exception', "tick_publish (next run at: X)"
                    )
    finally:
        logger.removeFilter(throttle)

    assert len([r for r in caplog.records if r.name == EXECUTOR_LOGGER_NAME]) == 1


async def test_a_real_scheduler_job_failing_every_firing_logs_one_traceback(
    caplog: pytest.LogCaptureFixture,
):
    """Against APScheduler itself, not a hand-built record: the real log message
    carries the job's changing next-run time, which must not defeat the match."""
    import asyncio

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    runs = 0

    async def always_failing() -> None:
        nonlocal runs
        runs += 1
        raise RuntimeError("zerodha rejected the stored credentials (HTTP 403)")

    logger = logging.getLogger(EXECUTOR_LOGGER_NAME)
    throttle = RepeatedJobFailureFilter(window_seconds=60)
    logger.addFilter(throttle)
    scheduler = AsyncIOScheduler()
    try:
        with caplog.at_level(logging.ERROR, logger=EXECUTOR_LOGGER_NAME):
            scheduler.add_job(always_failing, "interval", seconds=0.05, id="failing")
            scheduler.start()
            await asyncio.sleep(0.6)
            scheduler.shutdown(wait=False)
    finally:
        logger.removeFilter(throttle)

    assert runs >= 5, "the job really did fail repeatedly"
    failures = [r for r in caplog.records if r.name == EXECUTOR_LOGGER_NAME and r.exc_info]
    assert len(failures) == 1


def test_application_logging_setup_installs_the_throttle():
    from src.observability.logging import configure_logging

    configure_logging()

    logger = logging.getLogger(EXECUTOR_LOGGER_NAME)
    assert any(isinstance(f, RepeatedJobFailureFilter) for f in logger.filters)
