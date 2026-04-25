# tests/pulse_bot/test_verify300.py
"""Tests for verify300 infrastructure.

2026-04-24 PG migration: ``pg_test_db`` fixture isolates each test in
its own Postgres DB. Legacy ``Database(tmp.name)`` calls transparently
route to the isolated DB via ``_resolve_dsn`` monkey-patch.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

pytestmark = pytest.mark.usefixtures("pg_test_db")

from pulse_bot.config import PulseBotConfig, get_config
from pulse_bot.db import Database


class TestVerifyInfrastructure:
    """Tests that verify300 components work independently."""

    def test_db_schema_has_source_column(self, pg_test_db) -> None:
        """token_scores must have source column for live/backtest/provider."""
        import psycopg2
        conn = psycopg2.connect(pg_test_db)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='token_scores'"
            )
            cols = [r[0] for r in cur.fetchall()]
        conn.close()
        assert "source" in cols

    def test_db_source_values(self, pg_test_db) -> None:
        """Source column accepts live, backtest, provider."""
        import psycopg2
        conn = psycopg2.connect(pg_test_db)
        with conn.cursor() as cur:
            for source in ("live", "backtest", "provider"):
                cur.execute(
                    "INSERT INTO token_scores (mint, source, total_score) "
                    "VALUES (%s, %s, %s)",
                    (f"mint_{source}", source, 0),
                )
            conn.commit()
            for source in ("live", "backtest", "provider"):
                cur.execute(
                    "SELECT source FROM token_scores WHERE mint = %s",
                    (f"mint_{source}",),
                )
                assert cur.fetchone()[0] == source
        conn.close()

    def test_clear_backtest_scores_keeps_live(self, pg_test_db) -> None:
        """clear_backtest_scores removes backtest but keeps live."""
        import psycopg2
        db = Database("pulse_bot.db")  # redirected to pg_test_db via fixture
        conn = psycopg2.connect(pg_test_db)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO token_scores (mint, source, total_score) "
                "VALUES ('a', 'live', 10)"
            )
            cur.execute(
                "INSERT INTO token_scores (mint, source, total_score) "
                "VALUES ('a', 'backtest', 10)"
            )
            conn.commit()
        db.clear_backtest_scores()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM token_scores WHERE source='live'")
            live = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM token_scores WHERE source='backtest'")
            bt = cur.fetchone()[0]
        conn.close()
        assert live == 1
        assert bt == 0

    def test_paper_trades_table_exists(self, pg_test_db) -> None:
        """paper_trades table created by init_schema."""
        import psycopg2
        conn = psycopg2.connect(pg_test_db)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
            tables = [r[0] for r in cur.fetchall()]
        conn.close()
        assert "paper_trades" in tables

    def test_entry_buyer_number_in_config(self) -> None:
        """min/max entry buyer number knobs must exist."""
        cfg = PulseBotConfig()
        # min=1 and max=20 per per-trade marginal CI analysis (v4 sweep).
        # Earlier values (5/50) were based on confounded per-combo averages.
        assert cfg.min_entry_buyer_number == 1
        assert cfg.max_entry_buyer_number == 20

    def test_trailing_stop_in_config(self) -> None:
        """Trailing stop params must be in config; default = enabled."""
        cfg = PulseBotConfig()
        assert cfg.exit_trailing_stop_enabled is True
        # combo4680 (v10 ROBUST combo — 3/5 folds profitable): act=50, dist=50.
        assert cfg.exit_trailing_stop_activation_pct == 50.0
        assert cfg.exit_trailing_stop_distance_pct == 50.0

    def test_strict_combo_defaults(self) -> None:
        """Pin numeric defaults. Updated 2026-04-22 after volume-cliff
        optimizer sweep showed marginal-best axis values reduce
        per-trade loss from −0.73%% to −0.21%% (still net-negative on
        pump.fun, but asymptotically closer to zero).
        """
        cfg = PulseBotConfig()
        # Strict combo from 2026-04-22 sweep: score_buy=50, vol≥10 SOL,
        # unique_buyers≥5, top1 gate disabled, tight SL=15, max_hold=90.
        assert cfg.score_threshold_buy == 50
        assert cfg.entry_mode == "both"
        assert cfg.entry_min_sol_volume_hard == 10.0
        assert cfg.entry_min_unique_buyers_hard == 5
        assert cfg.entry_max_top1_holder_pct == 100.0
        assert cfg.exit_hard_stop_loss_pct == 15.0
        assert cfg.exit_max_hold_seconds == 90.0
        assert cfg.exit_take_profit_pct == 100.0
        assert cfg.exit_inactivity_seconds == 120.0

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
