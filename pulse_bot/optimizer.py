# pulse_bot/optimizer.py
"""Optimizer — grid search over configurable parameters, stores results in SQLite."""

from __future__ import annotations

import copy
import itertools
import json
import logging
import time
import uuid
from dataclasses import asdict, fields
from typing import TYPE_CHECKING

from pulse_bot.backtest import BacktestEngine

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database

logger = logging.getLogger(__name__)

# Default parameter grid — override via Optimizer.set_grid()
DEFAULT_GRID: dict[str, list] = {
    "entry_mode": ["fast", "full", "both"],
    "fast_observe_seconds": [3, 5, 8],
    "fast_score_threshold": [10, 15, 25],
    "score_threshold_buy": [15, 20, 30],
    "exit_hard_stop_loss_pct": [30, 50, 70],
    "pulse_dead_buy_rate": [0.05, 0.10, 0.20],
}


class Optimizer:
    """Grid search optimizer. Runs BacktestEngine for each parameter combination.

    Results are saved to SQLite after each run — dashboard can show progress live.
    """

    def __init__(self, base_config: PulseBotConfig, db: Database) -> None:
        self._base_cfg = base_config
        self._db = db
        self._grid: dict[str, list] = {}
        self._session_id = f"opt_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    @property
    def session_id(self) -> str:
        return self._session_id

    def set_grid(self, grid: dict[str, list]) -> None:
        """Set custom parameter grid. Keys must be PulseBotConfig field names."""
        valid_fields = {f.name for f in fields(self._base_cfg)}
        for key in grid:
            if key not in valid_fields:
                logger.warning("Unknown config field in grid: %s (skipping)", key)
        self._grid = {k: v for k, v in grid.items() if k in valid_fields}

    def use_default_grid(self) -> None:
        """Use the default parameter grid."""
        self._grid = dict(DEFAULT_GRID)

    def estimate_runs(self) -> int:
        """Estimate total number of combinations."""
        if not self._grid:
            return 0
        counts = [len(v) for v in self._grid.values()]
        total = 1
        for c in counts:
            total *= c
        return total

    def run(self) -> list[dict]:
        """Run grid search. Returns list of results sorted by profit_factor."""
        if not self._grid:
            self.use_default_grid()

        param_names = list(self._grid.keys())
        param_values = list(self._grid.values())
        combinations = list(itertools.product(*param_values))
        total = len(combinations)

        logger.info(
            "Optimizer starting: session=%s, %d params, %d combinations",
            self._session_id, len(param_names), total,
        )
        logger.info("Grid: %s", {k: v for k, v in self._grid.items()})

        results: list[dict] = []
        start_wall = time.time()

        for i, combo in enumerate(combinations):
            run_id = f"{self._session_id}_r{i:04d}"
            params = dict(zip(param_names, combo))

            # Build config for this run
            cfg = self._make_config(params)

            logger.info(
                "[%d/%d] Running: %s",
                i + 1, total,
                " ".join(f"{k}={v}" for k, v in params.items()),
            )

            try:
                engine = BacktestEngine(cfg, self._db)
                bt_result = engine.run()

                # Build run data
                run_data = self._result_to_dict(run_id, params, cfg, bt_result)
                results.append(run_data)

                # Save immediately — dashboard sees it live
                self._db.save_optimization_run(run_data)

                logger.info(
                    "[%d/%d] Done: trades=%d win_rate=%.0f%% pnl=%+.4f pf=%.2f roi=%+.1f%%",
                    i + 1, total,
                    run_data["total_trades"], run_data["win_rate"],
                    run_data["total_pnl_sol"], run_data["profit_factor"],
                    run_data["roi_pct"],
                )
            except Exception:
                logger.exception("[%d/%d] Failed for params: %s", i + 1, total, params)

        elapsed = time.time() - start_wall
        results.sort(key=lambda r: r.get("profit_factor", 0), reverse=True)

        logger.info(
            "Optimizer done: %d runs in %.1fs, session=%s",
            len(results), elapsed, self._session_id,
        )

        self._print_top_results(results)
        return results

    def _make_config(self, params: dict) -> PulseBotConfig:
        """Create a config copy with overridden parameters."""
        cfg = copy.copy(self._base_cfg)
        for key, value in params.items():
            setattr(cfg, key, value)
        return cfg

    def _result_to_dict(self, run_id: str, params: dict, cfg: PulseBotConfig, bt_result: object) -> dict:
        """Convert BacktestResult to a storable dict."""
        trades_list = []
        for t in getattr(bt_result, "closed_trades", []):
            trades_list.append({
                "mint": t.mint, "symbol": t.symbol,
                "entry_type": t.entry_type, "exit_reason": t.exit_reason,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "entry_time": t.entry_time, "exit_time": t.exit_time,
                "sol_invested": t.sol_invested, "sol_received": t.sol_received,
                "pnl_sol": t.pnl_sol, "pnl_pct": t.pnl_pct,
                "hold_seconds": t.hold_seconds, "partial_sells": t.partial_sells,
            })

        exit_reasons = getattr(bt_result, "exit_reasons", {})

        return {
            "run_id": run_id,
            "session": self._session_id,
            "params": json.dumps(params),
            "entry_mode": cfg.entry_mode,
            "total_trades": getattr(bt_result, "total_trades", 0),
            "wins": getattr(bt_result, "wins", 0),
            "losses": getattr(bt_result, "losses", 0),
            "win_rate": getattr(bt_result, "win_rate", 0),
            "total_pnl_sol": getattr(bt_result, "total_pnl_sol", 0),
            "gross_profit_sol": getattr(bt_result, "gross_profit_sol", 0),
            "gross_loss_sol": getattr(bt_result, "gross_loss_sol", 0),
            "profit_factor": getattr(bt_result, "profit_factor", 0),
            "avg_win_pct": getattr(bt_result, "avg_win_pct", 0),
            "avg_loss_pct": getattr(bt_result, "avg_loss_pct", 0),
            "avg_win_sol": getattr(bt_result, "avg_win_sol", 0),
            "avg_loss_sol": getattr(bt_result, "avg_loss_sol", 0),
            "max_drawdown_pct": getattr(bt_result, "max_drawdown_pct", 0),
            "initial_balance_sol": cfg.portfolio_initial_sol,
            "final_balance_sol": getattr(bt_result, "final_balance_sol", 0),
            "roi_pct": getattr(bt_result, "roi_pct", 0),
            "avg_hold_seconds": getattr(bt_result, "avg_hold_seconds", 0),
            "fast_buys": getattr(bt_result, "entry_types", {}).get("fast", 0),
            "full_buys": getattr(bt_result, "entry_types", {}).get("full", 0),
            "exit_reasons": json.dumps(exit_reasons),
            "trades_json": json.dumps(trades_list),
            "trades": trades_list,
            "created_at": time.time(),
        }

    @staticmethod
    def _print_top_results(results: list[dict]) -> None:
        """Print top 10 results to console."""
        print("\n" + "=" * 80)
        print("  OPTIMIZER TOP 10 RESULTS")
        print("=" * 80)
        print(f"  {'#':>3s}  {'Trades':>6s}  {'WR%':>5s}  {'PnL SOL':>9s}  {'PF':>5s}  {'ROI%':>7s}  {'DD%':>5s}  Params")
        print("-" * 80)

        for i, r in enumerate(results[:10]):
            params = json.loads(r["params"])
            params_str = " ".join(f"{k}={v}" for k, v in params.items())
            print(
                f"  {i+1:3d}  {r['total_trades']:6d}  {r['win_rate']:5.1f}  {r['total_pnl_sol']:+9.4f}  "
                f"{r['profit_factor']:5.2f}  {r['roi_pct']:+7.1f}  {r['max_drawdown_pct']:5.1f}  {params_str}"
            )
        print("=" * 80 + "\n")
