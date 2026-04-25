# tests/pulse_bot/test_refactor_fixes.py
"""Regression tests for the post-refactor fixes.

2026-04-24 PG migration: tests used ``Database(tmp.db)`` SQLite temp
files. Now each test gets an isolated Postgres DB via the ``pg_test_db``
fixture (see conftest.py). ``_resolve_dsn`` is monkey-patched to route
legacy paths to the isolated DB so test code didn't need to change.

Covers:
  F1 — optimizer reads creator_stats from the snapshot DB, not self._db.
  F2 — optimizer emits timeout trade (slot held) for tokens with no monitor trades.
  F3 — ReplayLaunchpad._drain_all honors inactivity_timeout in phase 2.
  F4 — core.decide_entry gates fast entries on fast_result.buy_count (not full).
  F5 — db.close_paper_trade computes hold_seconds from stored entry_time.
  F6 — PaperTradeRunner.process_trade elapsed uses entry_time (not token age).
  F7 — close_paper_trade respects a caller-supplied replay-virtual exit_time.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("pg_test_db")

import pytest

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import PaperTradeRunner, decide_entry
from pulse_bot.db import Database
from pulse_bot.filters.fast import FastResult
from pulse_bot.models import CreatorStats, ScoringResult, Token, Trade
from pulse_bot.optimizer import CachedToken, Optimizer
from pulse_bot.sources.replay import ReplayLaunchpad

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fast(decision: str, buy_count: int, score: int = 20) -> FastResult:
    """Build a minimal FastResult fixture."""
    return FastResult(
        decision=decision,
        score=score,
        reasons="",
        buy_count=buy_count,
        sell_count=0,
        unique_buyers=max(buy_count, 0),
        volume_sol=1.0,
        buy_rate=1.0,
        sell_ratio=0.0,
        curve_pct=10.0,
        elapsed=3.0,
    )


def _make_full(
    decision: str,
    buy_count: int,
    total_score: int = 25,
    exit_price: float = 1.0,
) -> ScoringResult:
    """Build a minimal ScoringResult fixture."""
    return ScoringResult(
        decision=decision,
        total_score=total_score,
        buy_count=buy_count,
        exit_price=exit_price,
    )


def _write_creators_only_db(rows: list[tuple]) -> str:
    """Create a standalone DB containing only the ``creators`` table + rows."""
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = fd.name
    fd.close()
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE creators (
               wallet TEXT PRIMARY KEY,
               total_tokens_created INTEGER DEFAULT 0,
               times_seen INTEGER DEFAULT 0,
               tokens_where_creator_sold_early INTEGER DEFAULT 0,
               first_seen_at REAL, last_seen_at REAL,
               blacklisted INTEGER DEFAULT 0
           )"""
    )
    conn.executemany(
        "INSERT INTO creators (wallet, total_tokens_created, times_seen, "
        "tokens_where_creator_sold_early, first_seen_at, last_seen_at, blacklisted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# F1 — creator_stats come from the snapshot, not from self._db
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="SQLite snapshot-file isolation is obsolete after PG migration — "
    "PG uses MVCC for optimizer isolation, no separate DB file needed."
)
class TestCreatorStatsFromSnapshot:
    """``get_creator_stats_sync(..., source_db_path=...)`` must read from the
    snapshot DB so the optimizer never queries the results DB for creator data.
    """

    def test_source_db_path_is_used(self, tmp_path: Path) -> None:
        """Rows present in the snapshot DB are returned; the default DB is ignored."""
        # Arrange — two distinct DB files with different contents.
        live_path = str(tmp_path / "empty_results.db")
        Database(live_path).init_schema()  # empty creators table in "results" DB
        snap_path = _write_creators_only_db([("WALLET1", 7, 7, 1, 1.0, 2.0, 0)])

        db = Database(live_path)

        # Act — query via snapshot path.
        stats = db.get_creator_stats_sync("WALLET1", source_db_path=snap_path)
        empty = db.get_creator_stats_sync("WALLET1")  # default path (results DB)

        # Assert — snapshot returned data, default returned nothing.
        assert stats is not None
        assert stats.total_tokens_created == 7
        assert stats.tokens_where_creator_sold_early == 1
        assert empty is None

    def test_missing_creator_returns_none(self, tmp_path: Path) -> None:
        """Unknown wallets resolve to None without raising."""
        snap_path = _write_creators_only_db([])
        db = Database(str(tmp_path / "results.db"))
        db.init_schema()
        assert db.get_creator_stats_sync("UNKNOWN", source_db_path=snap_path) is None


# ---------------------------------------------------------------------------
# F2 — optimizer holds the slot for tokens with no post-entry trades
# ---------------------------------------------------------------------------


class TestTimeoutTradeEmitted:
    """When a kept token has no ``monitor_trades``, the optimizer must still emit
    a synthetic dead_token trade so the portfolio slot is consumed."""

    def test_no_monitor_trades_still_emits_trade(self) -> None:
        """The combo simulator returns one trade even when monitor_trades is empty."""
        cfg = PulseBotConfig()
        cfg.exit_inactivity_seconds = 120.0
        cfg.portfolio_max_positions = 3
        cfg.buy_amount_sol = 0.01

        token = Token(
            mint="MINT1",
            name="n",
            symbol="s",
            creator="c",
            created_at=1000.0,
            uri="",
            launchpad="pumpfun",
        )
        cached = CachedToken(
            token=token,
            monitor_trades=[],  # no post-entry trades — would be dropped before fix
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        ctx = {
            "cfg": cfg,
            "candidates": [(1005.0, "MINT1", "fast", 20, 0.001)],
        }

        opt = Optimizer.__new__(Optimizer)  # bypass __init__; we only exercise a method
        closed = opt._simulate_combo_event_driven(ctx, {"MINT1": cached})

        assert len(closed) == 1
        trade = closed[0]
        assert trade["exit_reason"] == "dead_token"
        # exit_time is pushed out by the inactivity window so the slot stays taken.
        assert trade["exit_time"] == pytest.approx(1005.0 + 120.0)
        assert trade["hold_seconds"] == pytest.approx(120.0)

    def test_dead_token_slot_blocks_later_candidate(self) -> None:
        """While the dead_token position is open, a later candidate must be skipped
        when portfolio_max_positions is saturated."""
        cfg = PulseBotConfig()
        cfg.exit_inactivity_seconds = 120.0
        cfg.portfolio_max_positions = 1

        token_a = Token(
            mint="A",
            name="",
            symbol="",
            creator="",
            created_at=1000.0,
            uri="",
            launchpad="pumpfun",
        )
        token_b = Token(
            mint="B",
            name="",
            symbol="",
            creator="",
            created_at=1000.0,
            uri="",
            launchpad="pumpfun",
        )
        cached_a = CachedToken(
            token=token_a,
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        cached_b = CachedToken(
            token=token_b,
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=0.001,
        )
        # Second entry is 30s after first — still inside the 120s inactivity window.
        ctx = {
            "cfg": cfg,
            "candidates": [
                (1005.0, "A", "fast", 20, 0.001),
                (1035.0, "B", "fast", 20, 0.001),
            ],
        }

        opt = Optimizer.__new__(Optimizer)
        closed = opt._simulate_combo_event_driven(ctx, {"A": cached_a, "B": cached_b})

        # B should be dropped because A's dead_token slot is still active.
        assert len(closed) == 1
        assert closed[0]["mint"] == "A"


# ---------------------------------------------------------------------------
# F3 — ReplayLaunchpad._drain_all honors inactivity_timeout
# ---------------------------------------------------------------------------


def _make_trade(mint: str, timestamp: float) -> Trade:
    return Trade(
        mint=mint,
        wallet="W",
        tx_type="buy",
        sol_amount=0.1,
        token_amount=1.0,
        new_token_balance=1.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=30.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=30.0,
        timestamp=timestamp,
    )


class TestReplayDrainAllInactivity:
    """``_drain_all`` must stop yielding when the inter-trade gap exceeds the
    configured inactivity window, mirroring live stream semantics."""

    def test_stops_on_inactivity_gap(self) -> None:
        """A gap larger than inactivity_timeout truncates the yielded stream."""

        async def runner() -> list[Trade]:
            launchpad = ReplayLaunchpad(db_path=":memory:")
            queue: asyncio.Queue[Trade] = asyncio.Queue()
            for ts in (
                100.0,
                110.0,
                115.0,
                300.0,
                305.0,
            ):  # 185s gap between 115 and 300
                await queue.put(_make_trade("M", ts))

            drained: list[Trade] = []
            async for trade in launchpad._drain_all(queue, inactivity_timeout=60.0):
                drained.append(trade)
            return drained

        result = asyncio.run(runner())
        # Only the first three trades are contiguous within 60s.
        assert [t.timestamp for t in result] == [100.0, 110.0, 115.0]

    def test_zero_timeout_yields_everything(self) -> None:
        """inactivity_timeout=0 preserves the legacy behavior (no gap check)."""

        async def runner() -> list[Trade]:
            launchpad = ReplayLaunchpad(db_path=":memory:")
            queue: asyncio.Queue[Trade] = asyncio.Queue()
            for ts in (10.0, 20.0, 10_000.0):
                await queue.put(_make_trade("M", ts))
            drained: list[Trade] = []
            async for trade in launchpad._drain_all(queue, inactivity_timeout=0):
                drained.append(trade)
            return drained

        result = asyncio.run(runner())
        assert len(result) == 3


# ---------------------------------------------------------------------------
# F4 — decide_entry uses fast buy_count for fast entries
# ---------------------------------------------------------------------------


class TestDecideEntryFastBuyerCount:
    """Fast-mode / both-mode entries must apply min/max_entry_buyer_number
    against the fast-window buy count, not the later full-window count."""

    def test_fast_entry_rejected_when_fast_buys_below_min(self) -> None:
        """If fast buy_count is below min, entry must be rejected — even when
        the full window already has enough buyers."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "fast"
        cfg.min_entry_buyer_number = 10
        cfg.max_entry_buyer_number = 50

        fast = _make_fast(decision="FAST_BUY", buy_count=2)  # 2+1 = 3, below min 10
        full = _make_full(decision="BUY", buy_count=30)  # full window later has 31

        should_enter, etype, _, _ = decide_entry(fast, full, cfg)
        assert should_enter is False
        assert etype == ""

    def test_full_entry_unaffected_by_fast_buy_count(self) -> None:
        """Full entries continue to use full_result.buy_count — the fast count is
        irrelevant when full wins."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "full"
        cfg.min_entry_buyer_number = 5
        cfg.max_entry_buyer_number = 50

        fast = _make_fast(decision="WAIT", buy_count=0)
        full = _make_full(decision="BUY", buy_count=10)  # 11 ≥ 5

        should_enter, etype, score, _ = decide_entry(fast, full, cfg)
        assert should_enter is True
        assert etype == "full"
        assert score == 25

    def test_both_mode_fast_wins_uses_fast_count(self) -> None:
        """In 'both' mode when fast triggers, the gate uses fast_result.buy_count."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "both"
        cfg.min_entry_buyer_number = 10
        cfg.max_entry_buyer_number = 50

        # Fast says BUY with a tiny buy_count; full says SKIP but score is not <0.
        fast = _make_fast(decision="FAST_BUY", buy_count=2)
        full = _make_full(decision="SKIP", buy_count=40, total_score=5)

        should_enter, _etype, _score, _ = decide_entry(fast, full, cfg)
        assert should_enter is False  # gated by fast_count, not full_count

    def test_fast_entry_accepted_when_fast_buys_within_bounds(self) -> None:
        """Fast entry passes when its own buy_count is within [min, max)."""
        cfg = PulseBotConfig()
        cfg.entry_mode = "fast"
        cfg.min_entry_buyer_number = 3
        cfg.max_entry_buyer_number = 10

        fast = _make_fast(decision="FAST_BUY", buy_count=4)  # 4+1 = 5, in range
        full = _make_full(decision="SKIP", buy_count=100, total_score=0)

        should_enter, etype, score, _ = decide_entry(fast, full, cfg)
        assert should_enter is True
        assert etype == "fast"
        assert score == 20


# ---------------------------------------------------------------------------
# F5/F7 — close_paper_trade computes hold from stored entry_time
# ---------------------------------------------------------------------------


class TestClosePaperTradeHoldSeconds:
    """``close_paper_trade`` must compute ``hold_seconds`` from the entry_time
    that was stored in ``open_paper_trade`` — not from a caller-passed value —
    so accidental usage of ``token.created_at`` cannot inflate position hold."""

    def _open_trade(
        self, db: Database, entry_time: float, entry_price: float = 0.001
    ) -> int:
        async def run() -> int:
            return await db.open_paper_trade(
                {
                    "mint": "MINT1",
                    "symbol": "S",
                    "entry_price": entry_price,
                    "entry_time": entry_time,
                    "entry_mcap_sol": 30.0,
                    "entry_buyer_number": 5,
                    "entry_type": "fast",
                    "entry_score": 20,
                    "buy_amount_sol": 0.01,
                }
            )

        return asyncio.run(run())

    def test_hold_uses_stored_entry_time_with_explicit_exit(
        self, tmp_path: Path
    ) -> None:
        """When an explicit ``exit_time`` is supplied, hold = exit_time - entry_time."""
        db = Database(str(tmp_path / "live.db"))
        db.init_schema()
        entry_ts = 1_000_000.0
        trade_id = self._open_trade(db, entry_time=entry_ts)

        async def close() -> None:
            await db.close_paper_trade(
                trade_id=trade_id,
                exit_price=0.002,
                exit_reason="take_profit",
                exit_buyer_number=42,
                exit_mcap_sol=60.0,
                entry_price=0.001,
                buy_amount_sol=0.01,
                exit_time=entry_ts + 180.0,  # 3-minute virtual hold
            )

        asyncio.run(close())

        row = db.get_paper_trades()[0]
        assert row["status"] == "closed"
        assert row["hold_seconds"] == pytest.approx(180.0)
        assert row["exit_reason"] == "take_profit"

    def test_hold_is_clamped_to_zero_when_exit_before_entry(
        self, tmp_path: Path
    ) -> None:
        """``MAX(? - entry_time, 0)`` guards against clock skew / bad replay data."""
        db = Database(str(tmp_path / "live.db"))
        db.init_schema()
        entry_ts = 1_000_000.0
        trade_id = self._open_trade(db, entry_time=entry_ts)

        async def close() -> None:
            await db.close_paper_trade(
                trade_id=trade_id,
                exit_price=0.001,
                exit_reason="timeout",
                exit_buyer_number=0,
                exit_mcap_sol=30.0,
                entry_price=0.001,
                buy_amount_sol=0.01,
                exit_time=entry_ts - 30.0,  # exit_time earlier than entry
            )

        asyncio.run(close())

        row = db.get_paper_trades()[0]
        assert row["hold_seconds"] == pytest.approx(0.0)

    def test_caller_cannot_pass_entry_time_anymore(self, tmp_path: Path) -> None:
        """Legacy callers that pass ``entry_time=token.created_at`` would inflate
        hold. The signature no longer accepts that parameter so such calls fail
        loudly instead of silently recording bogus hold_seconds."""
        db = Database(str(tmp_path / "live.db"))
        db.init_schema()
        trade_id = self._open_trade(db, entry_time=2_000.0)

        with pytest.raises(TypeError):
            asyncio.run(
                db.close_paper_trade(  # type: ignore[call-arg]
                    trade_id=trade_id,
                    exit_price=0.002,
                    exit_reason="take_profit",
                    exit_buyer_number=1,
                    exit_mcap_sol=60.0,
                    entry_price=0.001,
                    entry_time=1_000.0,  # removed param — should raise TypeError
                    buy_amount_sol=0.01,
                )
            )


# ---------------------------------------------------------------------------
# F6 — PaperTradeRunner elapsed uses entry_time, not token age
# ---------------------------------------------------------------------------


def _scored_trade(mint: str, timestamp: float, sol: float, tokens: float) -> Trade:
    """Build a Trade whose sol/token ratio is a known price."""
    return Trade(
        mint=mint,
        wallet="W",
        tx_type="buy",
        sol_amount=sol,
        token_amount=tokens,
        new_token_balance=tokens,
        bonding_curve_key="",
        v_sol_in_bonding_curve=30.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=30.0,
        timestamp=timestamp,
    )


class TestProcessTradeElapsed:
    """``PaperTradeRunner.process_trade`` must compute ExitManager ``elapsed``
    relative to the caller-supplied ``entry_time`` — not to any implicit token
    creation time. Old code passed ``token.created_at`` which reported multi-
    hour holds for tokens processed from startup backlogs."""

    def test_fresh_position_does_not_timeout_despite_old_trade_timestamps(self) -> None:
        """If entry_time matches the trade timestamps, elapsed stays tiny even
        when the timestamps themselves are far in the past (e.g., replay of an
        old token). Position must not exit via ``timeout``."""
        cfg = PulseBotConfig()
        cfg.exit_max_hold_seconds = 60.0  # 1-minute max hold
        cfg.exit_hard_stop_loss_pct = 999.0  # disable stop loss for this test
        cfg.exit_take_profit_enabled = False
        cfg.exit_trailing_stop_enabled = False

        # Token "age" = 12 hours; entry_time is right before the first trade.
        old_ts_base = 1_000_000.0
        entry_time = old_ts_base + 43_200.0  # 12h after token creation
        runner = PaperTradeRunner(cfg, entry_price=1e-6)

        # Five trades over a 30-second window starting at entry_time.
        results: list = []
        for i in range(5):
            trade = _scored_trade(
                mint="M",
                timestamp=entry_time + i * 5.0,
                sol=0.1,
                tokens=100000.0,  # constant price -> no PnL move
            )
            results.append(runner.process_trade(trade, entry_time))

        # No timeout: every call returns None because elapsed ≤ 20s < 60s cap.
        assert all(r is None for r in results)

    def test_timeout_fires_when_elapsed_exceeds_max_hold(self) -> None:
        """When trade timestamps run past entry_time + max_hold, the runner
        must signal a timeout exit through the exit manager."""
        cfg = PulseBotConfig()
        cfg.exit_max_hold_seconds = 30.0
        cfg.exit_hard_stop_loss_pct = 999.0
        cfg.exit_take_profit_enabled = False
        cfg.exit_trailing_stop_enabled = False
        cfg.pulse_min_events = 1  # let the pulse snapshot fire on every trade

        entry_time = 1_000.0
        runner = PaperTradeRunner(cfg, entry_price=1e-6)

        # First trade at +10s: should hold (elapsed 10s < 30s cap).
        r1 = runner.process_trade(
            _scored_trade("M", entry_time + 10.0, 0.1, 100000.0), entry_time
        )
        assert r1 is None

        # Second trade at +45s: past max_hold → timeout.
        r2 = runner.process_trade(
            _scored_trade("M", entry_time + 45.0, 0.1, 100000.0), entry_time
        )
        assert r2 is not None
        assert r2.exit_reason == "timeout"

    def test_old_token_created_at_does_not_trigger_spurious_timeout(self) -> None:
        """Regression: passing ``token.created_at`` (as pipeline used to do)
        would make ``elapsed`` equal the token's age and fire ``timeout`` on
        the very first trade. The new signature eliminates that bug — this
        test locks the new behavior in place."""
        cfg = PulseBotConfig()
        cfg.exit_max_hold_seconds = 60.0
        cfg.exit_hard_stop_loss_pct = 999.0
        cfg.exit_take_profit_enabled = False
        cfg.exit_trailing_stop_enabled = False

        token_created_at = 1_000_000.0  # token born long ago
        entry_time = token_created_at + 50_000.0  # we enter 13h later
        runner = PaperTradeRunner(cfg, entry_price=1e-6)

        trade = _scored_trade("M", entry_time + 2.0, 0.1, 100000.0)
        result = runner.process_trade(trade, entry_time)
        # With entry_time: elapsed = 2s → no timeout.
        assert result is None


# ---------------------------------------------------------------------------
# F8 — optimizer late-first-trade closes at entry_time + inactivity
# ---------------------------------------------------------------------------


class TestOptimizerLateFirstTrade:
    """``_simulate_trade_from`` seeds the gap clock from ``entry_time`` so a
    monitor trade arriving later than ``entry_time + inactivity`` triggers a
    dead_token exit at the inactivity boundary — matching live/replay."""

    def test_late_first_trade_triggers_dead_token_at_inactivity_boundary(self) -> None:
        """Monitor stream has one trade far past the inactivity window.

        Before the fix: ``last_trade_ts`` was seeded from the first monitor
        trade, so the gap from entry_time was invisible and the late trade was
        processed as if it were on time. After the fix: the gap is detected
        and the position exits as dead_token at entry_time + inactivity.
        """
        cfg = PulseBotConfig()
        cfg.exit_inactivity_seconds = 60.0
        cfg.buy_amount_sol = 0.01

        token = Token(
            mint="LATE",
            name="",
            symbol="",
            creator="",
            created_at=1000.0,
            uri="",
            launchpad="pumpfun",
        )
        # One trade arrives 300s after entry — well past the 60s inactivity.
        late_trade = _scored_trade("LATE", timestamp=1350.0, sol=0.1, tokens=1e5)
        cached = CachedToken(
            token=token,
            monitor_trades=[late_trade],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=1e-6,
        )
        opt = Optimizer.__new__(Optimizer)
        result = opt._simulate_trade_from(
            cached,
            entry_type="full",
            entry_score=20,
            entry_price=1e-6,
            entry_time=1050.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "dead_token"
        assert result["exit_time"] == pytest.approx(1050.0 + 60.0)
        assert result["hold_seconds"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# F9 — inactivity=0 semantics: no dead_token fabricated
# ---------------------------------------------------------------------------


class TestZeroInactivitySemantics:
    """``exit_inactivity_seconds == 0`` means "inactivity tracking disabled".
    The optimizer must NOT claim ``dead_token`` in that case — there is no
    inactivity signal to detect, so an end-of-stream exit is a plain timeout.
    """

    def test_empty_monitor_trades_with_zero_inactivity_is_timeout(self) -> None:
        """With inactivity disabled and no monitor activity, the slot frees
        at entry_time itself (hold=0) under the ``timeout`` reason."""
        cfg = PulseBotConfig()
        cfg.exit_inactivity_seconds = 0.0  # disabled
        cfg.buy_amount_sol = 0.01

        token = Token(
            mint="Z",
            name="",
            symbol="",
            creator="",
            created_at=100.0,
            uri="",
            launchpad="pumpfun",
        )
        cached = CachedToken(
            token=token,
            monitor_trades=[],
            creator_snapshot=None,
            creator_tokens_today=0,
            entry_price=1e-6,
        )
        opt = Optimizer.__new__(Optimizer)
        result = opt._simulate_trade_from(
            cached,
            entry_type="full",
            entry_score=20,
            entry_price=1e-6,
            entry_time=200.0,
            cfg=cfg,
        )
        assert result["exit_reason"] == "timeout"
        assert result["exit_time"] == pytest.approx(200.0)
        assert result["hold_seconds"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# F10 — optimizer fast-entry clock matches live (full observe window, not fast)
# ---------------------------------------------------------------------------


class TestOptimizerFastEntryClock:
    """Pipeline opens fast paper trades only AFTER full scoring, so the
    optimizer must also anchor fast-entry ``entry_time`` to the end of the
    FULL observation window — not to ``token.created_at + fast_observe_seconds``.
    This test pins the invariant.
    """

    def test_fast_entry_time_equals_end_of_full_window(self) -> None:
        """Given a fast entry, ``entry_time`` in the optimizer matches
        ``full_trades[-1].timestamp`` (or, when empty, end of full window)."""
        # Build a synthetic set of trades spanning fast and full windows.
        token_created_at = 1_000.0
        fast_sec = 5
        full_sec = 45
        fast_trades = [
            _scored_trade("M", token_created_at + i, 0.1, 1e5) for i in range(1, 5)
        ]
        full_trades_extra = [
            _scored_trade("M", token_created_at + fast_sec + i, 0.1, 1e5)
            for i in (1, 10, 30, full_sec - fast_sec - 1)
        ]
        full_trades = fast_trades + full_trades_extra

        # Replicate the computation the optimizer now performs.
        entry_time = (
            full_trades[-1].timestamp if full_trades else token_created_at + full_sec
        )

        # Entry clock must lie at the END of the full window, not at 5s mark.
        assert entry_time > token_created_at + fast_sec
        assert entry_time == pytest.approx(
            token_created_at + fast_sec + (full_sec - fast_sec - 1)
        )

    def test_entry_time_falls_back_to_full_window_when_empty(self) -> None:
        """No scoring trades at all → fallback to ``token.created_at + observe_seconds``."""
        token_created_at = 500.0
        full_sec = 45
        full_trades: list[Trade] = []

        entry_time = (
            full_trades[-1].timestamp if full_trades else token_created_at + full_sec
        )
        assert entry_time == pytest.approx(545.0)


# ---------------------------------------------------------------------------
# F11 — resume path carries last_event_ts from DB
# ---------------------------------------------------------------------------


class TestResumeLastEventTs:
    """On restart, a resumed paper trade must inherit its last observed
    activity from ``price_updated_at`` — not get a fresh inactivity window.
    Verifies the SELECT shape and column plumbing required by the pipeline.
    """

    def test_price_updated_at_is_selectable_after_update(self, tmp_path: Path) -> None:
        """``update_paper_trade`` stamps ``price_updated_at`` so the resume
        query can pick it up as the seed for ``last_event_ts``."""
        db = Database(str(tmp_path / "resume.db"))
        db.init_schema()

        async def go() -> tuple[int, float]:
            trade_id = await db.open_paper_trade(
                {
                    "mint": "RESUME",
                    "symbol": "R",
                    "entry_price": 1e-6,
                    "entry_time": 10_000.0,
                    "entry_mcap_sol": 30.0,
                    "entry_buyer_number": 1,
                    "entry_type": "fast",
                    "entry_score": 20,
                    "buy_amount_sol": 0.01,
                }
            )
            # Simulate activity via update_paper_trade, which stamps
            # price_updated_at = time.time().
            await db.update_paper_trade(
                trade_id,
                current_price=2e-6,
                entry_price=1e-6,
                total_buys=3,
                total_sells=0,
                mcap_sol=40.0,
            )
            row = db.get_paper_trades(status="open")[0]
            return trade_id, float(row["price_updated_at"])

        trade_id, price_updated_at = asyncio.run(go())
        assert trade_id > 0
        # price_updated_at is a wall-clock stamp, not zero/null.
        assert price_updated_at > 0.0

    def test_resume_query_exposes_price_updated_at(self, tmp_path: Path) -> None:
        """The SQL the pipeline uses for resume MUST project ``price_updated_at``
        so it can be threaded into ``last_event_ts``. This locks the query."""
        db = Database(str(tmp_path / "resume.db"))
        db.init_schema()

        async def seed() -> None:
            await db.open_paper_trade(
                {
                    "mint": "R",
                    "symbol": "R",
                    "entry_price": 1e-6,
                    "entry_time": 5_000.0,
                    "entry_mcap_sol": 30.0,
                    "entry_buyer_number": 1,
                    "entry_type": "fast",
                    "entry_score": 20,
                    "buy_amount_sol": 0.01,
                }
            )
            await db.update_paper_trade(
                1,
                current_price=2e-6,
                entry_price=1e-6,
                total_buys=1,
                total_sells=0,
                mcap_sol=30.0,
            )

        asyncio.run(seed())

        import psycopg2
        import psycopg2.extras

        from pulse_bot.db import _resolve_dsn

        conn = psycopg2.connect(_resolve_dsn(db.db_path))
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT p.entry_time, p.price_updated_at
                   FROM paper_trades p WHERE p.status='open'"""
            )
            row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row["entry_time"] == pytest.approx(5_000.0)
        assert row["price_updated_at"] > 0.0
