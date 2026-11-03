"""What the code imports is declared, not borrowed from another package's defaults.

SQLAlchemy 2.0 installed `greenlet` on its own; 2.1 does not, so the first Dependabot pull request
that moved SQLAlchemy to 2.1 failed every import of the async engine in CI. The extra that installs
it is declared, so the next version bump cannot take it away.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _dependencies() -> list[str]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]


def test_the_async_engine_declares_the_extra_that_installs_greenlet():
    sqlalchemy = [d for d in _dependencies() if d.lower().startswith("sqlalchemy")]

    assert len(sqlalchemy) == 1
    assert sqlalchemy[0].lower().startswith("sqlalchemy[asyncio]")
