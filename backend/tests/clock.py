"""A frozen clock for tests of code that reads `datetime.now()` itself.

A test that builds a time relative to the real clock and then lets the code under test read the
clock again is comparing two different instants: it passes until a runner stalls, or the date
changes in between. Freezing the module's `datetime` makes both reads the same instant.

    freeze_clock(monkeypatch, tick_source_module, datetime(2026, 3, 10, 5, 0, tzinfo=UTC))

Only `datetime.now()` is frozen; every other `datetime` attribute (constructors, `combine`,
`fromisoformat`) is the real one, because the frozen class subclasses it.
"""

from datetime import datetime
from types import ModuleType

import pytest


def freeze_clock(monkeypatch: pytest.MonkeyPatch, module: ModuleType, instant: datetime) -> None:
    """Make `datetime.now(tz)` inside `module` return `instant` (converted to `tz` when given).

    `instant` must be timezone-aware; `now()` with no `tz` returns it as given.
    """
    if instant.tzinfo is None:
        raise ValueError("freeze_clock needs a timezone-aware instant")

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
