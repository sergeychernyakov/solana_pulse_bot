# tests/pulse_bot/conftest.py
"""Pytest configuration for pulse_bot tests.

* Loads ``.env`` so HELIUS_API_KEY etc are visible during tests.
* Provides ``pg_test_db`` fixture: per-test isolated Postgres database
  with full schema applied, auto-dropped on teardown. Legacy tests that
  use ``Database(tmp_path/"live.db")`` have their DSN rewritten to this
  isolated DB so no code changes are needed on the test side.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


def pytest_configure() -> None:
    """Load .env from project root before tests run."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value


@pytest.fixture
def pg_test_db(monkeypatch):
    """Per-test isolated Postgres DB with full schema applied.

    Monkey-patches ``pulse_bot.db._resolve_dsn`` so any ``Database(...)``
    call inside the test — regardless of the path argument — routes to
    this isolated DB. Drops the DB on teardown.

    Yields the DSN URL (e.g. ``postgresql://user@localhost/pulse_test_abc``).
    """
    import psycopg2
    import psycopg2.extensions

    db_name = f"pulse_test_{uuid.uuid4().hex[:8]}"
    admin_dsn = "dbname=postgres user=sergeychernyakov"
    test_dsn_url = f"postgresql://sergeychernyakov@localhost/{db_name}"

    # CREATE DATABASE requires autocommit.
    admin = psycopg2.connect(admin_dsn)
    admin.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    with admin.cursor() as c:
        c.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()

    # Apply schema DDL.
    schema_sql = (
        Path(__file__).parent.parent.parent / "pulse_bot" / "db_schema_pg.sql"
    ).read_text()
    tc = psycopg2.connect(f"dbname={db_name} user=sergeychernyakov")
    tc.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    with tc.cursor() as c:
        c.execute(schema_sql)
    tc.close()

    # Redirect Database("*.db") calls to this isolated DB.
    import asyncio

    import pulse_bot.db as _pgdb

    # Close any stale pool from prior tests (asyncpg pool is bound to the
    # event loop that created it; asyncio.run() between tests invalidates
    # it). Force-null so the first call in this test re-creates.
    if _pgdb._asyncpg_pool is not None:
        try:
            asyncio.new_event_loop().run_until_complete(_pgdb._asyncpg_pool.close())
        except Exception:
            pass
    if _pgdb._psycopg_pool is not None:
        try:
            _pgdb._psycopg_pool.closeall()
        except Exception:
            pass

    monkeypatch.setattr(_pgdb, "_resolve_dsn", lambda _path=None: test_dsn_url)
    monkeypatch.setattr(_pgdb, "_asyncpg_pool", None)
    monkeypatch.setattr(_pgdb, "_psycopg_pool", None)

    yield test_dsn_url

    # Teardown: close pools, drop DB.
    import pulse_bot.db as _pgdb

    if _pgdb._asyncpg_pool is not None:
        try:
            asyncio.new_event_loop().run_until_complete(_pgdb._asyncpg_pool.close())
        except Exception:
            pass
    _pgdb._asyncpg_pool = None
    if _pgdb._psycopg_pool is not None:
        try:
            _pgdb._psycopg_pool.closeall()
        except Exception:
            pass
    _pgdb._psycopg_pool = None

    admin = psycopg2.connect(admin_dsn)
    admin.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    with admin.cursor() as c:
        c.execute(
            """SELECT pg_terminate_backend(pid) FROM pg_stat_activity
               WHERE datname = %s AND pid <> pg_backend_pid()""",
            (db_name,),
        )
        c.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    admin.close()
