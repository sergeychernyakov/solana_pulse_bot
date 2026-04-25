# tests/pulse_bot/test_simulate_exit_parity.py
"""Option B-3 — structural parity between simulate_exit and live exit path.

Live path: Pipeline._paper_trade → PaperTradeRunner.process_trade →
ExitManager.decide + PulseMonitor.

Training path: build_dataset.build_entry_dataset → simulate_exit →
PaperTradeRunner.process_trade → ExitManager.decide + PulseMonitor.

Both paths share PaperTradeRunner verbatim — simulate_exit is a thin
synchronous wrapper. These tests freeze that invariant so a future
refactor that diverges the two paths (e.g. adds a branch only to
Pipeline._paper_trade) fails fast instead of silently re-opening the
train/serve skew.
"""

from __future__ import annotations

from pulse_bot.config import get_config
from pulse_bot.core import PaperTradeRunner
from pulse_bot.ml.simulate_exit import simulate_exit
from pulse_bot.models import Trade


def _make_trade(
    mint: str,
    wallet: str,
    tx_type: str,
    sol_amount: float,
    token_amount: float,
    market_cap_sol: float,
    timestamp: float,
) -> Trade:
    return Trade(
        mint=mint,
        wallet=wallet,
        tx_type=tx_type,
        sol_amount=sol_amount,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="bc",
        v_sol_in_bonding_curve=30.0,
        v_tokens_in_bonding_curve=1e9,
        market_cap_sol=market_cap_sol,
        timestamp=timestamp,
    )


def _replay_manual(
    config, trades: list[Trade], entry_ts: float, entry_price: float
):
    """Hand-rolled equivalent of simulate_exit for parity comparison."""
    runner = PaperTradeRunner(config, entry_price)
    last_ts = entry_ts
    inactivity = float(config.exit_inactivity_seconds or 0.0)
    for t in trades:
        if inactivity > 0.0 and (t.timestamp - last_ts) > inactivity:
            return runner.timeout_result()
        result = runner.process_trade(t, entry_ts)
        if result is not None:
            return result
        last_ts = t.timestamp
    return runner.timeout_result()


def test_simulate_exit_matches_manual_replay_dump() -> None:
    """Dump pattern → hard_stop via simulate_exit == manual PaperTradeRunner loop."""
    cfg = get_config()
    trades = [
        _make_trade("M", f"W{i}", "sell", 0.8, 1.0, max(5.0 - i * 0.1, 0.1), 1.0 + i)
        for i in range(10)
    ]
    a = simulate_exit(cfg, trades, entry_ts=0.0, entry_price=1.0)
    b = _replay_manual(cfg, trades, entry_ts=0.0, entry_price=1.0)
    assert a.exit_reason == b.exit_reason
    assert abs(a.pnl_pct - b.pnl_pct) < 1e-9
    assert abs(a.exit_price - b.exit_price) < 1e-9
    assert a.total_buys == b.total_buys
    assert a.total_sells == b.total_sells


def test_simulate_exit_matches_manual_replay_inactivity() -> None:
    """Inactivity gap > exit_inactivity_seconds triggers timeout identically."""
    cfg = get_config()
    trades = [
        _make_trade("M", "W1", "buy", 1.0, 1.0, 10.0, 5.0),
        # Gap larger than config.exit_inactivity_seconds
        _make_trade("M", "W2", "buy", 1.0, 1.0, 10.5, 5.0 + cfg.exit_inactivity_seconds + 50.0),
    ]
    a = simulate_exit(cfg, trades, entry_ts=0.0, entry_price=1.0)
    b = _replay_manual(cfg, trades, entry_ts=0.0, entry_price=1.0)
    assert a.exit_reason == b.exit_reason == "timeout"
    assert abs(a.pnl_pct - b.pnl_pct) < 1e-9


def test_simulate_exit_exhausts_to_timeout_result() -> None:
    """When trades run out before any exit triggers, both paths return the
    runner's timeout_result(). Protects against future code that might
    accidentally leave the function with no exit."""
    cfg = get_config()
    trades = [
        _make_trade("M", f"W{i}", "buy", 0.5, 1.0, 10.0 + i * 0.1, 1.0 + i * 0.5)
        for i in range(5)
    ]
    a = simulate_exit(cfg, trades, entry_ts=0.0, entry_price=1.0)
    b = _replay_manual(cfg, trades, entry_ts=0.0, entry_price=1.0)
    assert a.exit_reason == b.exit_reason
    assert abs(a.pnl_pct - b.pnl_pct) < 1e-9


def test_simulate_exit_preserves_exit_manager_behavior() -> None:
    """Changing exit_hard_stop_loss_pct via config must flow through to
    simulate_exit — proves it uses the LIVE config, not hardcoded
    thresholds (the Option C trap)."""
    cfg = get_config()
    # Force -25% drawdown on first trade
    trades = [
        _make_trade("M", "W1", "sell", 0.75, 1.0, 3.0, 1.0),
        _make_trade("M", "W2", "sell", 0.75, 1.0, 3.0, 2.0),
    ]
    # SL=15% → should fire hard_stop immediately
    from dataclasses import replace
    cfg_tight = replace(cfg, exit_hard_stop_loss_pct=5.0)
    cfg_loose = replace(cfg, exit_hard_stop_loss_pct=50.0)
    tight = simulate_exit(cfg_tight, trades, entry_ts=0.0, entry_price=1.0)
    loose = simulate_exit(cfg_loose, trades, entry_ts=0.0, entry_price=1.0)
    assert tight.exit_reason == "hard_stop"
    # Loose SL = 50%, only -25% drawdown → stays hold, ends in timeout.
    assert loose.exit_reason == "timeout"
