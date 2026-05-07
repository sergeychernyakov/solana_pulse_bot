#!/usr/bin/env python3
# scripts/runtime_regression_monitor.py
"""Runtime regression monitor (Tier-S Layer 2 safety net).

Runs every 30 minutes via systemd timer. Pulls the last 4-hour
window of closed paper_trades from PostgreSQL, computes WR / PnL /
hard-stop ratio, and compares against the baseline at
``pulse_bot/ml/regression_baseline.json``.

If the live window degrades beyond tunable thresholds, the monitor
flips the bot into **safe mode** by editing ``.env`` (preserving
all secret-bearing lines) and restarting ``pulse-bot.service``:

* ``PULSE_SURVIVAL_ACTIVE=0`` — disable survival kill switch (it was
  the source of the most expensive past regression).
* ``PULSE_ENTRY_PROBA_CEILING=0.30`` — only the most confident ML
  override BUYs are accepted; reduces selection rate by ~3-5×.

Both edits are reversible via ``.env.before-safemode.<TS>`` backup
files. The monitor never goes the *other* way (i.e. never relaxes
config) — only tightens. Loosening is a deliberate operator
decision.

Telemetry is logged to ``logs/regression_monitor.log`` (rotated by
systemd). Each tick prints either:

* ``OK <metrics>`` — within tolerance, no action.
* ``WARN <metric> ...`` — below baseline by 1-2σ, no action yet.
* ``REGRESSION <metric> ...; SAFE-MODE engaged`` — outside 2σ,
  config tightened + service restarted.

Manual one-off run:

.. code-block:: bash

   ssh rich "cd /home/sergey/www/gg && set -a && source .env && set +a && \\
     PYTHONPATH=. .venv/bin/python scripts/runtime_regression_monitor.py"

The systemd timer (``pulse-regression-monitor.timer``) drives this
on the production host.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

# Make pulse_bot importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_bot.db import _resolve_dsn  # noqa: E402

logger = logging.getLogger("runtime_regression_monitor")

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "pulse_bot" / "ml" / "regression_baseline.json"
ENV_PATH = REPO_ROOT / ".env"
SAFE_MODE_FLAG_PATH = REPO_ROOT / ".safe_mode_engaged"

# Live window — long enough to absorb minute-to-minute noise but
# short enough to catch a regressing bot fast (e.g. a buggy retrain
# pushed at 02:00 should be caught by 06:30 at the latest).
LIVE_WINDOW_HOURS = 4.0
ROLLING_BASELINE_HOURS = 48.0   # 2-day rolling baseline — short enough to
                                # exclude old regimes (e.g. pre-confidence-
                                # gate era), long enough to gather stable N
MIN_TRADES_FOR_VERDICT = 30

# Live thresholds. We compare live 4h paper-trade metrics against a
# 7-day rolling baseline computed from the SAME ``paper_trades`` table
# (apples-to-apples). The pre-deploy gate's
# ``regression_baseline.json`` is for code changes — it's based on
# ``simulate_exit_batch`` replay and not directly comparable to live
# realised PnL.
WR_DROP_PP = 5.0           # WR drop in pp vs rolling baseline
PNL_PER_TRADE_DROP_SOL = 0.005   # absolute drop in avg PnL/trade vs baseline
                                 # (0.005 SOL ≈ ~17 % of position size — meaningful
                                 # delta on the lottery distribution we have)
HARD_STOP_RATIO_MAX = 0.20    # > 20 % hard_stop exits is suspicious
SURV_PREDICT_RATIO_MAX = 0.30 # > 30 % survival_predict means confidence-gate failed
ABSOLUTE_PNL_FLOOR = -0.5     # one-sided abs floor — never below −0.5 SOL/4h regardless of baseline

SAFE_MODE_OVERRIDES = {
    "PULSE_SURVIVAL_ACTIVE": "0",
    "PULSE_ENTRY_PROBA_CEILING": "0.30",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logging(log_to_file: bool) -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_to_file:
        log_dir = REPO_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.FileHandler(log_dir / "regression_monitor.log")
        )
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _load_window(conn: Any, since_seconds_ago: float, until_seconds_ago: float = 0.0) -> list[dict]:
    """Load closed paper_trades within ``[now - since, now - until]``."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT entry_time, exit_time, exit_reason, pnl_sol, pnl_pct,
                  hold_seconds, entry_type
           FROM paper_trades
           WHERE pnl_sol IS NOT NULL
             AND entry_time > extract(epoch FROM now()) - %s
             AND entry_time <= extract(epoch FROM now()) - %s
           ORDER BY entry_time DESC""",
        (int(since_seconds_ago), int(until_seconds_ago)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def _load_live_window(conn: Any, hours: float) -> list[dict]:
    return _load_window(conn, hours * 3600.0)


def _aggregate_live(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    pnls = [float(r["pnl_sol"] or 0.0) for r in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    wr_pct = 100.0 * len(wins) / n
    sum_pnl = sum(pnls)
    hard_stop = sum(1 for r in rows if r["exit_reason"] == "hard_stop")
    surv = sum(1 for r in rows if r["exit_reason"] == "survival_predict")
    return {
        "n": n,
        "wr_pct": round(wr_pct, 2),
        "sum_pnl_sol": round(sum_pnl, 4),
        "avg_pnl_sol": round(sum_pnl / n, 5),
        "hard_stop_ratio": round(hard_stop / n, 3),
        "survival_predict_ratio": round(surv / n, 3),
    }


def _decide(live: dict[str, Any], rolling: dict[str, Any]) -> tuple[str, list[str]]:
    """Return ``(verdict, reasons)``.

    verdict ∈ {"OK", "WARN", "REGRESSION"}. ``reasons`` is a list of
    human-readable strings explaining each tripped check.

    Compares live 4h window to a rolling 7-day baseline computed
    from the same ``paper_trades`` table — both are real exits, so
    metrics are directly comparable.
    """
    if live["n"] < MIN_TRADES_FOR_VERDICT:
        return "OK", [
            f"only {live['n']} trades in live window — sample too small "
            "for verdict (need ≥%d)" % MIN_TRADES_FOR_VERDICT
        ]

    reasons: list[str] = []
    regression = False
    warn = False

    # 1. Absolute PnL floor — one-sided sanity, regardless of baseline.
    if live["sum_pnl_sol"] < ABSOLUTE_PNL_FLOOR:
        reasons.append(
            f"sum_pnl_sol={live['sum_pnl_sol']:+.4f} below ABSOLUTE floor "
            f"{ABSOLUTE_PNL_FLOOR} over {live['n']} trades — bleeding fast"
        )
        regression = True

    # Need a rolling baseline with enough samples to compare against.
    if rolling["n"] >= MIN_TRADES_FOR_VERDICT * 5:  # ≥150 trades over 7d
        base_wr = rolling["wr_pct"]
        base_avg = rolling["avg_pnl_sol"]

        # 2. WR drop vs rolling baseline.
        wr_delta = live["wr_pct"] - base_wr
        if wr_delta < -WR_DROP_PP:
            reasons.append(
                f"WR={live['wr_pct']:.2f}% vs 7d-baseline {base_wr:.2f}% "
                f"(Δ={wr_delta:+.2f}pp, floor=−{WR_DROP_PP}pp)"
            )
            regression = True
        elif wr_delta < -WR_DROP_PP / 2:
            reasons.append(
                f"WR drift: {live['wr_pct']:.2f}% vs 7d-baseline {base_wr:.2f}% "
                f"(Δ={wr_delta:+.2f}pp — within warn band)"
            )
            warn = True

        # 3. avg PnL/trade absolute drop vs rolling baseline.
        # Absolute (not ratio) because both numbers are tiny and noisy
        # — a ratio test on values close to 0 is mathematically
        # explosive (e.g. 0.001 → -0.003 reads as "−400 % of baseline"
        # which is meaningless).
        avg_drop = base_avg - live["avg_pnl_sol"]
        if avg_drop > PNL_PER_TRADE_DROP_SOL:
            reasons.append(
                f"avg_pnl_sol={live['avg_pnl_sol']:+.5f} vs "
                f"baseline {base_avg:+.5f} "
                f"(Δ=−{avg_drop:.5f} SOL/trade, "
                f"max=−{PNL_PER_TRADE_DROP_SOL:.5f})"
            )
            regression = True

    # 4. Hard-stop ratio (catches over-aggressive SL).
    if live["hard_stop_ratio"] > HARD_STOP_RATIO_MAX:
        reasons.append(
            f"hard_stop_ratio={live['hard_stop_ratio']:.1%} "
            f"> max {HARD_STOP_RATIO_MAX:.1%} — "
            "exit-config or model degraded"
        )
        regression = True

    # 5. Survival-predict ratio (catches degenerate survival model).
    if live["survival_predict_ratio"] > SURV_PREDICT_RATIO_MAX:
        reasons.append(
            f"survival_predict_ratio={live['survival_predict_ratio']:.1%} "
            f"> max {SURV_PREDICT_RATIO_MAX:.1%} — "
            "confidence gate may be leaking"
        )
        regression = True

    if regression:
        return "REGRESSION", reasons
    if warn:
        return "WARN", reasons
    return "OK", reasons


def _engage_safe_mode(reasons: list[str], dry_run: bool) -> bool:
    """Edit ``.env`` to apply safe-mode overrides + restart service.

    Returns True if safe-mode was engaged, False if it was already
    active or dry-run.
    """
    if SAFE_MODE_FLAG_PATH.exists():
        logger.warning("safe-mode already engaged at %s — not re-applying",
                       SAFE_MODE_FLAG_PATH.read_text().strip())
        return False

    if dry_run:
        logger.info("DRY-RUN: would have edited %s with: %s",
                    ENV_PATH, SAFE_MODE_OVERRIDES)
        return False

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = ENV_PATH.with_suffix(f".env.before-safemode.{ts}")
    shutil.copy2(ENV_PATH, backup)
    logger.info("backed up .env → %s", backup.name)

    # Read, edit, write atomically.
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    seen = set()
    out: list[str] = []
    for line in lines:
        replaced = False
        for k, v in SAFE_MODE_OVERRIDES.items():
            if line.startswith(f"{k}="):
                out.append(f"{k}={v}\n")
                seen.add(k)
                replaced = True
                break
        if not replaced:
            out.append(line)
    # Append any keys that weren't already in .env.
    for k, v in SAFE_MODE_OVERRIDES.items():
        if k not in seen:
            out.append(f"{k}={v}\n")
    ENV_PATH.write_text("".join(out))
    logger.warning("safe-mode applied to .env")

    # Drop the flag file with metadata for later operator review.
    SAFE_MODE_FLAG_PATH.write_text(
        f"engaged_at={_now_iso()}\n"
        f"reasons=\n  - " + "\n  - ".join(reasons) + "\n"
        f"backup={backup.name}\n"
        f"to revert: cp {backup.name} .env && "
        f"rm {SAFE_MODE_FLAG_PATH.name} && "
        f"systemctl --user restart pulse-bot.service\n"
    )

    # Restart service so the new env vars take effect.
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "pulse-bot.service"],
            check=True,
        )
        logger.warning("pulse-bot.service restarted in safe mode")
    except Exception as exc:
        logger.error("failed to restart pulse-bot.service: %s", exc)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Decide but do not modify .env / restart.")
    parser.add_argument("--no-log-file", action="store_true",
                        help="Skip the file handler "
                             "(useful for ad-hoc local runs).")
    args = parser.parse_args()
    _setup_logging(log_to_file=not args.no_log_file)

    dsn = _resolve_dsn(os.environ.get("PULSE_PG_DSN"))
    conn = psycopg2.connect(dsn)
    try:
        live_rows = _load_live_window(conn, LIVE_WINDOW_HOURS)
        # Rolling baseline: prior 7 days, EXCLUDING the current 4h window.
        rolling_rows = _load_window(
            conn,
            since_seconds_ago=ROLLING_BASELINE_HOURS * 3600.0,
            until_seconds_ago=LIVE_WINDOW_HOURS * 3600.0,
        )
    finally:
        conn.close()

    live = _aggregate_live(live_rows)
    rolling = _aggregate_live(rolling_rows)
    verdict, reasons = _decide(live, rolling)
    logger.info(
        "VERDICT=%s live_n=%d rolling_n=%d  live=%s  rolling=%s",
        verdict, live.get("n", 0), rolling.get("n", 0),
        json.dumps(live), json.dumps(rolling),
    )
    for r in reasons:
        logger.info("  %s", r)

    if verdict == "REGRESSION" and not SAFE_MODE_FLAG_PATH.exists():
        engaged = _engage_safe_mode(reasons, dry_run=args.dry_run)
        if engaged:
            logger.warning("SAFE-MODE engaged. Operator action required.")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
