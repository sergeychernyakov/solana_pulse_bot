# tests/pulse_bot/test_sync_conn_no_idle_txn.py
"""Regression test for the 2026-05-15 deadlock.

The bot froze for 5h44m because ``_sync_conn`` returned psycopg2 connections
to the pool without closing their implicit transactions — the connections
sat ``idle in transaction`` holding ``AccessShareLock`` on ``paper_trades``,
which eventually blocked DDL and every subsequent paper-trade INSERT.

Fix: ``_sync_conn`` now ``rollback()``s the connection in its ``finally``
block before ``pool.putconn``. This test asserts that contract: every borrow
ends with a rollback, even on exceptions, and the rollback happens *before*
the connection is returned to the pool.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pulse_bot import db as db_mod


def _fake_pool_factory(conn: MagicMock) -> MagicMock:
    """Return a fake ThreadedConnectionPool that always hands out ``conn``."""
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


def test_sync_conn_rolls_back_before_returning_to_pool() -> None:
    """The fix: rollback must happen before putconn on the happy path."""
    conn = MagicMock()
    pool = _fake_pool_factory(conn)

    with patch.object(db_mod, "_get_sync_pool", return_value=pool):
        with db_mod._sync_conn("dsn://fake") as got:
            assert got is conn

    # rollback() called exactly once, BEFORE putconn(conn).
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn)
    # Ordering: rollback first, then putconn — the whole point of the fix.
    calls = conn.mock_calls + pool.mock_calls
    rb_idx = next(i for i, c in enumerate(calls) if c[0] == "rollback")
    put_idx = next(i for i, c in enumerate(calls) if c[0] == "putconn")
    assert rb_idx < put_idx, "rollback must happen before putconn"


def test_sync_conn_rolls_back_on_exception() -> None:
    """If the body raises, the connection must still be cleaned + returned."""
    conn = MagicMock()
    pool = _fake_pool_factory(conn)

    with patch.object(db_mod, "_get_sync_pool", return_value=pool):
        with pytest.raises(RuntimeError, match="boom"):
            with db_mod._sync_conn("dsn://fake"):
                raise RuntimeError("boom")

    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn)


def test_sync_conn_swallows_rollback_errors() -> None:
    """If ``rollback()`` itself raises (e.g. dead connection), the pool must
    still get its connection back — otherwise the pool slowly leaks."""
    conn = MagicMock()
    conn.rollback.side_effect = Exception("dead conn")
    pool = _fake_pool_factory(conn)

    with patch.object(db_mod, "_get_sync_pool", return_value=pool):
        with db_mod._sync_conn("dsn://fake"):
            pass  # benign body

    # rollback was attempted; the exception was swallowed; putconn still ran.
    conn.rollback.assert_called_once()
    pool.putconn.assert_called_once_with(conn)
