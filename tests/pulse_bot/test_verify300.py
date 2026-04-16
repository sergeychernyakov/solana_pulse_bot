# tests/pulse_bot/test_verify300.py
"""Tests for verify300 infrastructure — ensures the verification pipeline works correctly."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from pulse_bot.config import PulseBotConfig, get_config
from pulse_bot.db import Database


class TestVerifyInfrastructure:
    """Tests that verify300 components work independently."""

    def test_db_schema_has_source_column(self) -> None:
        """token_scores must have source column for live/backtest/provider."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        db.init_schema()

        conn = sqlite3.connect(tmp.name)
        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(token_scores)").fetchall()
        ]
        conn.close()
        os.unlink(tmp.name)

        assert "source" in cols

    def test_db_source_values(self) -> None:
        """Source column accepts live, backtest, provider."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        db.init_schema()

        conn = sqlite3.connect(tmp.name)
        for source in ("live", "backtest", "provider"):
            conn.execute(
                "INSERT INTO token_scores (mint, source, total_score) VALUES (?, ?, ?)",
                (f"mint_{source}", source, 0),
            )
        conn.commit()

        for source in ("live", "backtest", "provider"):
            row = conn.execute(
                "SELECT source FROM token_scores WHERE mint = ?", (f"mint_{source}",)
            ).fetchone()
            assert row[0] == source

        conn.close()
        os.unlink(tmp.name)

    def test_clear_backtest_scores_keeps_live(self) -> None:
        """clear_backtest_scores removes backtest but keeps live."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        db.init_schema()

        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "INSERT INTO token_scores (mint, source, total_score) VALUES ('a', 'live', 10)"
        )
        conn.execute(
            "INSERT INTO token_scores (mint, source, total_score) VALUES ('a', 'backtest', 10)"
        )
        conn.commit()
        conn.close()

        db.clear_backtest_scores()

        conn = sqlite3.connect(tmp.name)
        live = conn.execute(
            "SELECT COUNT(*) FROM token_scores WHERE source='live'"
        ).fetchone()[0]
        bt = conn.execute(
            "SELECT COUNT(*) FROM token_scores WHERE source='backtest'"
        ).fetchone()[0]
        conn.close()
        os.unlink(tmp.name)

        assert live == 1
        assert bt == 0

    def test_paper_trades_table_exists(self) -> None:
        """paper_trades table created by init_schema."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = Database(tmp.name)
        db.init_schema()

        conn = sqlite3.connect(tmp.name)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        os.unlink(tmp.name)

        assert "paper_trades" in tables

    def test_max_entry_buyer_number_in_config(self) -> None:
        """max_entry_buyer_number must be in config."""
        cfg = PulseBotConfig()
        assert hasattr(cfg, "max_entry_buyer_number")
        assert cfg.max_entry_buyer_number == 20

    def test_trailing_stop_in_config(self) -> None:
        """Trailing stop params must be in config."""
        cfg = PulseBotConfig()
        assert cfg.exit_trailing_stop_enabled is True
        assert cfg.exit_trailing_stop_activation_pct == 50.0
        assert cfg.exit_trailing_stop_distance_pct == 30.0

    def test_hard_entry_filters_in_config(self) -> None:
        """Hard entry filters must be in config."""
        cfg = PulseBotConfig()
        assert hasattr(cfg, "min_market_cap_sol")
        assert hasattr(cfg, "max_sell_pressure_for_entry")
        assert hasattr(cfg, "min_curve_for_entry")
        assert hasattr(cfg, "max_entry_buyer_number")

    def test_constants_not_in_config(self) -> None:
        """PUMPFUN constants should NOT be config fields."""
        cfg = PulseBotConfig()
        assert not hasattr(cfg, "pumpfun_graduation_sol")
        assert not hasattr(cfg, "execution_fee_pct")
        assert not hasattr(cfg, "execution_priority_fee")


class TestTrailingStop:
    """Test trailing stop logic in ExitManager."""

    def test_trailing_stop_activates_and_sells(self) -> None:
        """Trailing stop: activate at +50%, sell when drops 30% from peak."""
        from pulse_bot.pulse.exit_manager import ExitManager
        from pulse_bot.pulse.monitor import PulseSnapshot

        cfg = PulseBotConfig()
        cfg.exit_trailing_stop_enabled = True
        cfg.exit_trailing_stop_activation_pct = 50.0
        cfg.exit_trailing_stop_distance_pct = 30.0
        # Disable other exits
        cfg.exit_on_creator_dump = False
        cfg.exit_on_whale = False
        cfg.pulse_dead_buy_rate = 0.0
        cfg.exit_sell_pressure_ratio = 999
        cfg.exit_no_new_wallets_events = 999
        cfg.exit_near_graduation_pct = 999
        cfg.exit_hard_stop_loss_pct = 999
        cfg.exit_take_profit_enabled = False
        cfg.exit_max_hold_seconds = 99999
        cfg.exit_trend_dying_count = 999

        em = ExitManager(cfg)

        pulse = PulseSnapshot(
            buy_rate=0.5,
            sell_rate=0.1,
            new_wallet_rate=0.5,
            avg_buy_size_sol=0.1,
            total_sol_in_window=1.0,
            creator_selling=False,
            whale_exit=False,
            buy_rate_trend="stable",
            buy_size_trend="stable",
            trend_declining_count=0,
            curve_progress_pct=30.0,
            window_events=20,
        )

        # +40% — not activated yet
        signal = em.decide(pulse, pnl_pct=40.0, elapsed_sec=60)
        assert signal.action == "hold"

        # +60% — activated, peak = 60
        signal = em.decide(pulse, pnl_pct=60.0, elapsed_sec=120)
        assert signal.action == "hold"

        # +50% — dropped 10% from peak (60→50), not enough (need 30%)
        signal = em.decide(pulse, pnl_pct=50.0, elapsed_sec=180)
        assert signal.action == "hold"

        # +25% — dropped 35% from peak (60→25) → trailing stop!
        signal = em.decide(pulse, pnl_pct=25.0, elapsed_sec=240)
        assert signal.action == "sell_all"
        assert signal.reason == "trailing_stop"

    def test_trailing_stop_disabled(self) -> None:
        """When disabled, trailing stop never fires."""
        from pulse_bot.pulse.exit_manager import ExitManager
        from pulse_bot.pulse.monitor import PulseSnapshot

        cfg = PulseBotConfig()
        cfg.exit_trailing_stop_enabled = False
        cfg.exit_on_creator_dump = False
        cfg.exit_on_whale = False
        cfg.pulse_dead_buy_rate = 0.0
        cfg.exit_sell_pressure_ratio = 999
        cfg.exit_no_new_wallets_events = 999
        cfg.exit_near_graduation_pct = 999
        cfg.exit_hard_stop_loss_pct = 999
        cfg.exit_take_profit_enabled = False
        cfg.exit_max_hold_seconds = 99999
        cfg.exit_trend_dying_count = 999

        em = ExitManager(cfg)

        pulse = PulseSnapshot(
            buy_rate=0.5,
            sell_rate=0.1,
            new_wallet_rate=0.5,
            avg_buy_size_sol=0.1,
            total_sol_in_window=1.0,
            creator_selling=False,
            whale_exit=False,
            buy_rate_trend="stable",
            buy_size_trend="stable",
            trend_declining_count=0,
            curve_progress_pct=30.0,
            window_events=20,
        )

        # Peak at +100%, drop to +20% (80% drop) — should NOT trigger
        em.decide(pulse, pnl_pct=100.0, elapsed_sec=60)
        signal = em.decide(pulse, pnl_pct=20.0, elapsed_sec=120)
        assert signal.action == "hold"
