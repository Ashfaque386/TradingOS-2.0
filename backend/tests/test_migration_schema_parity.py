"""The migrations and the models describe the same database.

Why this exists: pytest builds its schema with `Base.metadata.create_all()`, so no test ever runs a
migration, and `alembic check` (which compares the two) is blind to CHECK constraints, to enum
labels, to server defaults and to triggers. A model change without a migration, or the reverse,
would therefore pass every other test and surface only on a real deployment, as a missing column
or a CHECK that was never created.

Two scratch databases are built, one with `alembic upgrade head` and one with `create_all()`, and
Postgres itself is asked to describe both (columns, constraints with their definitions, indexes,
enum labels, triggers, functions). The descriptions must be identical, so any drift is reported as
the exact line that differs.
"""

import os
import subprocess  # nosec B404 -- runs this interpreter's own alembic, see _alembic
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url

from src.core.config import get_settings
from src.models import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]

# What a database "is": each query returns rows that must match line for line.
_FINGERPRINT_QUERIES: dict[str, str] = {
    "columns": """
        select table_name, column_name, data_type, udt_name, character_maximum_length::text,
               is_nullable, coalesce(column_default, '')
        from information_schema.columns
        where table_schema = 'public' and table_name <> 'alembic_version'
        order by 1, 2""",
    "constraints": """
        select conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(oid)
        from pg_constraint
        where connamespace = 'public'::regnamespace
          and conrelid::regclass::text <> 'alembic_version'
        order by 1, 2""",
    "indexes": """
        select tablename, indexname, indexdef
        from pg_indexes
        where schemaname = 'public' and tablename <> 'alembic_version'
        order by 1, 2""",
    "enums": """
        select t.typname, string_agg(e.enumlabel, ',' order by e.enumsortorder)
        from pg_type t join pg_enum e on e.enumtypid = t.oid
        where t.typnamespace = 'public'::regnamespace
        group by t.typname order by 1""",
    "triggers": """
        select tgrelid::regclass::text, tgname, pg_get_triggerdef(oid)
        from pg_trigger where not tgisinternal order by 1, 2""",
    "functions": """
        select proname, prosrc from pg_proc
        where pronamespace = 'public'::regnamespace order by 1""",
}


def _server_url() -> URL:
    return make_url(get_settings().database_url.replace("+asyncpg", "+psycopg2"))


def _connect(database: str):
    url = _server_url()
    return psycopg2.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=database,
    )


def _create_database(name: str) -> None:
    connection = _connect("postgres")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f'create database "{name}"')
    connection.close()


def _drop_database(name: str) -> None:
    connection = _connect("postgres")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f'drop database if exists "{name}" with (force)')
    connection.close()


def _fingerprint(database: str) -> dict[str, list[str]]:
    connection = _connect(database)
    try:
        result: dict[str, list[str]] = {}
        with connection.cursor() as cursor:
            for section, query in _FINGERPRINT_QUERIES.items():
                cursor.execute(query)
                rows = cursor.fetchall()
                result[section] = [" | ".join(str(cell) for cell in row) for row in rows]
        return result
    finally:
        connection.close()


def _alembic(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic against a named database, in its own interpreter.

    A subprocess, because `alembic/env.py` reads the settings, which this process has already
    cached against the test database."""
    url = _server_url().set(database=database).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": url.replace("+psycopg2", "+asyncpg")}
    return subprocess.run(  # nosec B603 -- sys.executable and constant arguments
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        env=env,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    name = f"tradingos_alembic_{uuid.uuid4().hex[:10]}"
    _create_database(name)
    try:
        upgraded = _alembic(name, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr[-2000:]
        yield name
    finally:
        _drop_database(name)


@pytest.fixture(scope="module")
def metadata_database() -> Iterator[str]:
    name = f"tradingos_metadata_{uuid.uuid4().hex[:10]}"
    _create_database(name)
    try:
        engine = create_engine(_server_url().set(database=name))
        Base.metadata.create_all(engine)
        engine.dispose()
        yield name
    finally:
        _drop_database(name)


def test_there_is_exactly_one_migration_head():
    result = _alembic("postgres", "heads")

    assert result.returncode == 0, result.stderr[-2000:]
    heads = [line for line in result.stdout.splitlines() if "(head)" in line]
    assert len(heads) == 1, f"two migrations branch from the same parent: {heads}"


def test_alembic_check_finds_no_unmigrated_model_change(migrated_database):
    result = _alembic(migrated_database, "check")

    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]


@pytest.mark.parametrize("section", list(_FINGERPRINT_QUERIES))
def test_the_migrated_schema_matches_the_models(section, migrated_database, metadata_database):
    """What `alembic check` cannot see: CHECK definitions, enum labels, defaults, triggers."""
    from_migrations = set(_fingerprint(migrated_database)[section])
    from_models = set(_fingerprint(metadata_database)[section])

    only_in_migrations = sorted(from_migrations - from_models)
    only_in_models = sorted(from_models - from_migrations)
    assert not only_in_migrations and not only_in_models, (
        f"{section}: the migrations and the models disagree.\n"
        f"only after `alembic upgrade head`:\n  " + "\n  ".join(only_in_migrations) + "\n"
        "only after `create_all()` from the models:\n  " + "\n  ".join(only_in_models)
    )
