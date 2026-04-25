# scripts/exit_config_sweep.py
"""Sweep (SL, TP, max_hold) exit-config grid to find best economic_backtest.

For each combo:
1. Override exit params via env vars.
2. Rebuild dataset (simulate_exit relabels with new params).
3. Train XGBoost (chrono split).
4. Score economic_backtest at proba>=0.5: sum realized_pnl_pct on entries.

Reports CSV + ranked table. Each iteration ≈10 min (wallet features dominate);
8 combos ≈ 80 min total. Run with .venv/bin/python so xgboost is available.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import xgboost as xgb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("exit_sweep")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "data" / "ml" / "sweeps"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_GRID: list[tuple[float, float, float]] = [
    # (SL, TP, max_hold)
    (8.0, 50.0, 90.0),
    (8.0, 100.0, 90.0),
    (10.0, 50.0, 90.0),
    (10.0, 100.0, 90.0),
    (12.0, 50.0, 90.0),
    (12.0, 100.0, 90.0),
    (15.0, 50.0, 90.0),
    (15.0, 100.0, 90.0),  # current baseline
]

PROBA_THRESHOLD = 0.5
POSITION_SIZE_SOL = 0.1


def _reload_config_modules() -> None:
    """Force re-import of modules that snapshot config at import time."""
    for name in [
        "pulse_bot.config",
        "pulse_bot.ml.simulate_exit",
        "pulse_bot.ml.build_dataset",
    ]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])


def _build_and_score(sl: float, tp: float, max_hold: float) -> dict:
    """Build dataset with overrides, train, return economic_backtest metrics."""
    os.environ["PULSE_EXIT_HARD_STOP_LOSS_PCT"] = str(sl)
    os.environ["PULSE_EXIT_TAKE_PROFIT_PCT"] = str(tp)
    os.environ["PULSE_EXIT_MAX_HOLD_SECONDS"] = str(max_hold)
    _reload_config_modules()
    from pulse_bot.ml.build_dataset import build_entry_dataset
    from pulse_bot.ml.features import ENTRY_FEATURE_ORDER

    feature_cols = list(ENTRY_FEATURE_ORDER)

    t0 = time.perf_counter()
    df = build_entry_dataset(db_path="pulse_bot")
    build_sec = time.perf_counter() - t0

    if df.empty:
        return {"error": "empty dataset", "build_sec": build_sec}

    df = df.sort_values("scored_at").reset_index(drop=True)
    n = len(df)
    train_end = int(n * 0.6)
    val_end = int(n * 0.8)
    train_df = df.iloc[:train_end]
    test_df = df.iloc[val_end:]

    X_train = train_df[feature_cols]
    y_train = train_df["label"].values
    X_test = test_df[feature_cols]
    y_test = test_df["label"].values
    realized_test = test_df["realized_pnl_pct"].values

    pos = int(y_train.sum())
    neg = len(y_train) - pos
    spw = neg / max(pos, 1)
    model = xgb.XGBClassifier(
        n_estimators=150,
        max_depth=3,
        learning_rate=0.05,
        min_child_weight=5,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=spw,
        objective="binary:logistic",
        eval_metric="auc",
        random_state=42,
    )
    t1 = time.perf_counter()
    model.fit(X_train, y_train)
    train_sec = time.perf_counter() - t1

    proba_test = model.predict_proba(X_test)[:, 1]
    entries = proba_test >= PROBA_THRESHOLD
    n_entries = int(entries.sum())
    n_wins = int(((y_test == 1) & entries).sum())
    if n_entries:
        pnl_sol = float((realized_test[entries] / 100.0 * POSITION_SIZE_SOL).sum())
        mean_pct = float(realized_test[entries].mean())
        wr = n_wins / n_entries
    else:
        pnl_sol = 0.0
        mean_pct = 0.0
        wr = 0.0

    # Also compute AUC on test
    from sklearn.metrics import roc_auc_score

    auc = float(roc_auc_score(y_test, proba_test)) if y_test.sum() > 0 else float("nan")

    return {
        "sl": sl,
        "tp": tp,
        "max_hold": max_hold,
        "n_rows": n,
        "n_train": int(train_end),
        "n_test": len(test_df),
        "n_train_pos": pos,
        "n_test_pos": int(y_test.sum()),
        "auc_test": auc,
        "n_entries": n_entries,
        "n_wins": n_wins,
        "wr": wr,
        "mean_realized_pct": mean_pct,
        "pnl_sol": pnl_sol,
        "build_sec": build_sec,
        "train_sec": train_sec,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--grid",
        choices=["default", "quick", "wide", "max_hold", "wider_sl", "exit_axes"],
        default="default",
        help="default=8 combos, quick=2 sanity, wide=24, max_hold=4 (vary hold only), exit_axes=12 (SL×TP×hold)",
    )
    args = p.parse_args()

    if args.grid == "quick":
        grid = [(8.0, 50.0, 90.0), (15.0, 100.0, 90.0)]
    elif args.grid == "wide":
        grid = [
            (sl, tp, mh)
            for sl in (5.0, 8.0, 10.0, 12.0, 15.0, 20.0)
            for tp in (30.0, 50.0, 100.0, 200.0)
            for mh in (90.0,)
        ]
    elif args.grid == "max_hold":
        # Hypothesis: TP=100% never fires at 90s; longer hold lets winners
        # develop without changing loss magnitude (hard_stop unchanged).
        # RESULT 2026-04-25: NO-OP — pump.fun trade data exhausts <90s,
        # extending max_hold returns same timeout_result.
        grid = [
            (15.0, 100.0, 90.0),  # baseline
            (15.0, 100.0, 180.0),
            (15.0, 100.0, 300.0),
            (15.0, 100.0, 600.0),
        ]
    elif args.grid == "wider_sl":
        # Hypothesis: looser SL avoids cutting tokens that dip -15% then
        # recover to positive. Test SL=20/25/30 with current TP/max_hold.
        grid = [
            (15.0, 100.0, 90.0),  # baseline
            (20.0, 100.0, 90.0),
            (25.0, 100.0, 90.0),
            (30.0, 100.0, 90.0),
        ]
    elif args.grid == "exit_axes":
        # Cross-product after max_hold sweep narrows the winner.
        grid = [(sl, tp, mh) for sl in (15.0, 20.0, 30.0) for tp in (50.0, 100.0) for mh in (180.0, 300.0)]
    else:
        grid = DEFAULT_GRID

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    csv_path = RESULTS_DIR / f"exit_sweep_{timestamp}.csv"
    fields = [
        "sl",
        "tp",
        "max_hold",
        "n_rows",
        "n_train",
        "n_test",
        "n_train_pos",
        "n_test_pos",
        "auc_test",
        "n_entries",
        "n_wins",
        "wr",
        "mean_realized_pct",
        "pnl_sol",
        "build_sec",
        "train_sec",
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()

        results = []
        for i, (sl, tp, max_hold) in enumerate(grid, 1):
            logger.info(
                "[%d/%d] SL=%.0f%% TP=%.0f%% max_hold=%.0fs — building dataset…",
                i,
                len(grid),
                sl,
                tp,
                max_hold,
            )
            try:
                r = _build_and_score(sl, tp, max_hold)
            except Exception as exc:
                logger.exception("Combo failed: %s", exc)
                r = {
                    "sl": sl,
                    "tp": tp,
                    "max_hold": max_hold,
                    "error": str(exc),
                }
            results.append(r)
            row = {k: r.get(k) for k in fields}
            writer.writerow(row)
            fh.flush()
            if "error" not in r:
                logger.info(
                    "  → AUC=%.3f entries=%d WR=%.1f%% mean_pct=%.2f%% pnl=%.4f SOL " "(build %.0fs train %.0fs)",
                    r["auc_test"],
                    r["n_entries"],
                    r["wr"] * 100,
                    r["mean_realized_pct"],
                    r["pnl_sol"],
                    r["build_sec"],
                    r["train_sec"],
                )

    # Print ranked summary
    valid = [r for r in results if "error" not in r]
    valid.sort(key=lambda r: r["pnl_sol"], reverse=True)
    print()
    print("=" * 90)
    print(f"EXIT CONFIG SWEEP — {len(valid)}/{len(grid)} combos succeeded")
    print(f"Results CSV: {csv_path}")
    print("=" * 90)
    print(f"{'SL%':>5} {'TP%':>5} {'hold':>5} {'AUC':>6} {'entries':>8} " f"{'WR%':>6} {'mean%':>7} {'PnL SOL':>10}")
    for r in valid:
        print(
            f"{r['sl']:>5.0f} {r['tp']:>5.0f} {r['max_hold']:>5.0f} "
            f"{r['auc_test']:>6.3f} {r['n_entries']:>8d} "
            f"{r['wr'] * 100:>6.1f} {r['mean_realized_pct']:>7.2f} "
            f"{r['pnl_sol']:>10.4f}"
        )
    print("=" * 90)
    if valid:
        best = valid[0]
        print(
            f"BEST: SL={best['sl']:.0f}% TP={best['tp']:.0f}% "
            f"max_hold={best['max_hold']:.0f}s → PnL={best['pnl_sol']:+.4f} SOL "
            f"(WR={best['wr'] * 100:.1f}% on {best['n_entries']} entries)"
        )
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
