# tests/pulse_bot/test_creator_snapshot.py
"""Determinism + leakage tests for creator_snapshots and creator_flag_history.

Codex v5 required:
  - "future snapshot ignored" for backtest reads
  - "flag flip does not change replay"
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

from pulse_bot.db import Database
from pulse_bot.helius_creator import (
    CreatorSnapshot,
    CreatorSnapshotService,
    LocalSnapshotSource,
    SnapshotSource,
)


def _fresh_db() -> tuple[Database, str]:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = Database(tmp.name)
    db.init_schema()
    return db, tmp.name


def _insert_token(path: str, mint: str, creator: str, created_at: float) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO tokens (mint, creator, created_at) VALUES (?, ?, ?)",
            (mint, creator, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_score(
    path: str,
    mint: str,
    creator: str,
    created_at: float,
    market_cap_sol: float = 0.0,
    curve_progress_pct: float = 0.0,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO token_scores
              (mint, source, creator, market_cap_sol, curve_progress_pct,
               created_at, scored_at)
            VALUES (?, 'live', ?, ?, ?, ?, ?)
            """,
            (mint, creator, market_cap_sol, curve_progress_pct, created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


class TestCreatorSnapshotsSchema:
    def test_schema_has_snapshot_and_flag_tables(self) -> None:
        db, path = _fresh_db()
        try:
            conn = sqlite3.connect(path)
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()
            assert "creator_snapshots" in names
            assert "creator_flag_history" in names
        finally:
            os.unlink(path)

    def test_snapshot_has_required_typed_columns(self) -> None:
        db, path = _fresh_db()
        try:
            conn = sqlite3.connect(path)
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(creator_snapshots)"
                ).fetchall()
            }
            conn.close()
            for col in (
                "creator",
                "observed_at",
                "computed_through_ts",
                "api_source",
                "rug_rate",
                "graduation_rate",
                "total_prior_tokens",
                "median_peak_mc_sol",
                "creator_age_days",
                "feature_version",
                "data_json",
            ):
                assert col in cols, f"missing column {col}"
        finally:
            os.unlink(path)


class TestSnapshotObservedAtSemantics:
    """Backtest must never see snapshots recorded after the query ref_ts."""

    def test_future_snapshot_ignored_by_backtest_read(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                creator="C1",
                observed_at=100.0,
                computed_through_ts=100.0,
                api_source="local",
                total_prior_tokens=3,
            )
            assert db.get_creator_snapshot_as_of("C1", 80.0) is None
            assert db.get_creator_snapshot_as_of("C1", 100.0) is not None
            assert db.get_creator_snapshot_as_of("C1", 120.0) is not None
        finally:
            os.unlink(path)

    def test_as_of_picks_latest_not_after_ref(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                "C1", 50.0, 50.0, "local", total_prior_tokens=1
            )
            db.save_creator_snapshot(
                "C1", 100.0, 100.0, "local", total_prior_tokens=5
            )
            db.save_creator_snapshot(
                "C1", 200.0, 200.0, "local", total_prior_tokens=9
            )
            row = db.get_creator_snapshot_as_of("C1", 150.0)
            assert row is not None
            assert row["total_prior_tokens"] == 5
        finally:
            os.unlink(path)

    def test_tie_break_on_observed_at_is_deterministic(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                "C1", 100.0, 100.0, "local", total_prior_tokens=3
            )
            db.save_creator_snapshot(
                "C1", 100.0, 100.0, "local", total_prior_tokens=7
            )
            row = db.get_creator_snapshot_as_of("C1", 100.0)
            assert row is not None
            assert row["total_prior_tokens"] == 7
        finally:
            os.unlink(path)

    def test_rug_rate_and_grad_rate_persisted_from_counts(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                "C1",
                100.0,
                100.0,
                "local",
                total_prior_tokens=10,
                rug_count=3,
                graduated_count=2,
            )
            row = db.get_creator_snapshot_latest("C1")
            assert row is not None
            assert abs(row["rug_rate"] - 0.3) < 1e-9
            assert abs(row["graduation_rate"] - 0.2) < 1e-9
        finally:
            os.unlink(path)


class TestCreatorFlagHistory:
    def test_append_only_flip_closes_prior_row(self) -> None:
        db, path = _fresh_db()
        try:
            db.add_creator_flag(
                "C1", "blacklist", "rug_rate=0.9", set_at=100.0
            )
            db.add_creator_flag(
                "C1", "whitelist", "graduated 10", set_at=200.0
            )
            conn = sqlite3.connect(path)
            rows = conn.execute(
                "SELECT flag, valid_from, valid_to FROM creator_flag_history "
                "WHERE creator='C1' ORDER BY id"
            ).fetchall()
            conn.close()
            assert len(rows) == 2
            assert rows[0] == ("blacklist", 100.0, 200.0)
            assert rows[1][0] == "whitelist"
            assert rows[1][1] == 200.0
            assert rows[1][2] is None
        finally:
            os.unlink(path)

    def test_flag_flip_does_not_change_replay(self) -> None:
        """Codex-required: past decisions read the flag active at their time."""
        db, path = _fresh_db()
        try:
            db.add_creator_flag(
                "C1", "blacklist", "early rugs", set_at=100.0
            )
            past = db.get_creator_flag_as_of("C1", 150.0)
            assert past is not None
            assert past["flag"] == "blacklist"

            db.add_creator_flag(
                "C1", "whitelist", "later reformed", set_at=200.0
            )
            replay = db.get_creator_flag_as_of("C1", 150.0)
            assert replay is not None
            assert replay["flag"] == "blacklist"

            now_flag = db.get_creator_flag_latest("C1")
            assert now_flag is not None
            assert now_flag["flag"] == "whitelist"
        finally:
            os.unlink(path)

    def test_valid_from_inclusive_valid_to_exclusive(self) -> None:
        db, path = _fresh_db()
        try:
            db.add_creator_flag("C1", "blacklist", None, set_at=100.0)
            db.add_creator_flag("C1", "whitelist", None, set_at=200.0)

            assert db.get_creator_flag_as_of("C1", 99.9) is None
            assert db.get_creator_flag_as_of("C1", 100.0)["flag"] == "blacklist"
            assert db.get_creator_flag_as_of("C1", 199.999)["flag"] == "blacklist"
            assert db.get_creator_flag_as_of("C1", 200.0)["flag"] == "whitelist"
        finally:
            os.unlink(path)


class TestLocalSnapshotSource:
    def test_local_source_respects_ref_ts(self) -> None:
        db, path = _fresh_db()
        try:
            _insert_token(path, "m1", "C1", 10.0)
            _insert_token(path, "m2", "C1", 50.0)
            _insert_token(path, "m3", "C1", 150.0)  # future from ref_ts=100

            src = LocalSnapshotSource(path)
            snap = asyncio.run(src.compute("C1", 100.0))
            assert snap is not None
            assert snap.total_prior_tokens == 2
            assert snap.computed_through_ts == 100.0
            assert snap.api_source == "local"
        finally:
            os.unlink(path)

    def test_local_source_rug_and_graduated_counts(self) -> None:
        db, path = _fresh_db()
        try:
            # Rug: mc below threshold and no graduation.
            _insert_token(path, "m1", "C1", 10.0)
            _insert_score(path, "m1", "C1", 10.0, market_cap_sol=0.5, curve_progress_pct=15.0)
            # Graduated: curve_progress_pct >= 100.
            _insert_token(path, "m2", "C1", 20.0)
            _insert_score(path, "m2", "C1", 20.0, market_cap_sol=30.0, curve_progress_pct=100.0)
            # Mid-life: does not count as rug or graduate.
            _insert_token(path, "m3", "C1", 30.0)
            _insert_score(path, "m3", "C1", 30.0, market_cap_sol=5.0, curve_progress_pct=40.0)

            src = LocalSnapshotSource(path)
            snap = asyncio.run(src.compute("C1", 100.0))
            assert snap is not None
            assert snap.total_prior_tokens == 3
            assert snap.rug_count == 1
            assert snap.graduated_count == 1
        finally:
            os.unlink(path)

    def test_local_source_returns_none_for_unknown_creator(self) -> None:
        db, path = _fresh_db()
        try:
            src = LocalSnapshotSource(path)
            assert asyncio.run(src.compute("unknown_creator", 100.0)) is None
        finally:
            os.unlink(path)


class _FakeSource:
    """Records calls so we can assert backtest never invokes the source."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.result: CreatorSnapshot | None = None
        self.raise_error: Exception | None = None

    async def compute(
        self, creator: str, ref_ts: float
    ) -> CreatorSnapshot | None:
        self.calls.append((creator, ref_ts))
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


class TestCreatorSnapshotService:
    def test_backtest_path_never_calls_source(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                "C1", 100.0, 100.0, "local", total_prior_tokens=2
            )
            src = _FakeSource()
            svc = CreatorSnapshotService(db, src)
            snap = svc.get_for_backtest("C1", 150.0)
            assert snap is not None
            assert snap["total_prior_tokens"] == 2
            assert src.calls == []
        finally:
            os.unlink(path)

    def test_backtest_path_returns_none_when_no_snapshot_visible(self) -> None:
        db, path = _fresh_db()
        try:
            db.save_creator_snapshot(
                "C1", 200.0, 200.0, "local", total_prior_tokens=5
            )
            src = _FakeSource()
            svc = CreatorSnapshotService(db, src)
            # Query at T=150 but only snapshot is at T=200 → no visible row.
            assert svc.get_for_backtest("C1", 150.0) is None
            assert src.calls == []
        finally:
            os.unlink(path)

    def test_live_uses_fresh_cache_without_refetch(self) -> None:
        db, path = _fresh_db()
        try:
            now = 1000.0
            db.save_creator_snapshot(
                "C1", now - 60.0, now - 60.0, "local", total_prior_tokens=3
            )
            src = _FakeSource()
            svc = CreatorSnapshotService(db, src)
            snap = asyncio.run(svc.get_for_live("C1", now=now))
            assert snap is not None
            assert snap["total_prior_tokens"] == 3
            assert src.calls == []
        finally:
            os.unlink(path)

    def test_live_fetch_on_cache_miss(self) -> None:
        db, path = _fresh_db()
        try:
            src = _FakeSource()
            src.result = CreatorSnapshot(
                creator="C1",
                observed_at=1000.0,
                computed_through_ts=1000.0,
                api_source="local",
                total_prior_tokens=7,
            )
            svc = CreatorSnapshotService(db, src)
            snap = asyncio.run(svc.get_for_live("C1", now=1000.0))
            assert snap is not None
            assert snap["total_prior_tokens"] == 7
            assert src.calls == [("C1", 1000.0)]
            assert db.get_creator_snapshot_latest("C1") is not None
        finally:
            os.unlink(path)

    def test_live_degrades_on_source_failure_returning_stale_if_any(self) -> None:
        db, path = _fresh_db()
        try:
            now = 100_000.0
            very_old = now - 48 * 3600.0  # older than TTL_STALE
            db.save_creator_snapshot(
                "C1", very_old, very_old, "local", total_prior_tokens=2
            )
            src = _FakeSource()
            src.raise_error = RuntimeError("helius 500")
            svc = CreatorSnapshotService(db, src)
            snap = asyncio.run(svc.get_for_live("C1", now=now))
            assert snap is not None
            assert snap["total_prior_tokens"] == 2
        finally:
            os.unlink(path)

    def test_live_degrades_to_none_when_no_cache_and_source_fails(self) -> None:
        db, path = _fresh_db()
        try:
            src = _FakeSource()
            src.raise_error = RuntimeError("timeout")
            svc = CreatorSnapshotService(db, src)
            snap = asyncio.run(svc.get_for_live("C1", now=1000.0))
            assert snap is None
        finally:
            os.unlink(path)
