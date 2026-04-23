# pulse_bot/ml/weekly_retrain.py
"""Weekly retrain orchestrator: rebuild, train, snapshot, noise-floor.

Runs once per week (NOT daily). Daily validation operates on the
snapshot this script produces. Separating the two prevents daily-retrain
variance from masquerading as regime drift.

Steps:
    1. Rebuild entry + exit parquet datasets from current DB state.
    2. Train entry + exit models (chronological split).
    3. Copy trained models to ``data/ml/history/{kind}_model_YYYY-MM-DD.ubj``
       so daily validation can diff today's predictions against last
       week's model.
    4. Run label_noise_floor on the fresh datasets and write
       ``data/ml/noise_floor.json``.

The "current" model at ``data/ml/{kind}_model.ubj`` is overwritten each
run. The history directory is append-only (never purged by this script).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

from pulse_bot.ml import build_dataset, label_noise_floor, train

logger = logging.getLogger(__name__)


def snapshot_model(model_path: Path, history_dir: Path, today: date) -> Path:
    """Copy current model to history/{name}_{YYYY-MM-DD}.ubj."""
    history_dir.mkdir(parents=True, exist_ok=True)
    target = history_dir / f"{model_path.stem}_{today.isoformat()}.ubj"
    shutil.copy2(model_path, target)
    meta_src = model_path.with_suffix(".meta.json")
    if meta_src.exists():
        shutil.copy2(meta_src, target.with_suffix(".meta.json"))
    logger.info("Snapshot: %s", target)
    return target


def run(
    db_path: str,
    data_dir: Path,
    history_dir: Path,
    today: date | None = None,
) -> None:
    today = today or date.today()
    data_dir.mkdir(parents=True, exist_ok=True)

    logger.info("─" * 70)
    logger.info("WEEKLY RETRAIN — %s", today.isoformat())
    logger.info("─" * 70)

    # 1. Rebuild datasets
    entry_df = build_dataset.build_entry_dataset(db_path)
    entry_out = data_dir / "entry.parquet"
    try:
        entry_df.to_parquet(entry_out, index=False)
    except Exception:
        entry_out = data_dir / "entry.csv"
        entry_df.to_csv(entry_out, index=False)
    logger.info("Wrote %s", entry_out)

    exit_df = build_dataset.build_exit_dataset(db_path)
    exit_out = data_dir / "exit.parquet"
    try:
        exit_df.to_parquet(exit_out, index=False)
    except Exception:
        exit_out = data_dir / "exit.csv"
        exit_df.to_csv(exit_out, index=False)
    logger.info("Wrote %s", exit_out)

    # 2. Train
    entry_model = data_dir / "entry_model.ubj"
    train.train_entry(entry_out, entry_model, split="chrono")
    exit_model = data_dir / "exit_model.ubj"
    train.train_exit(exit_out, exit_model)

    # 3. Snapshot
    snapshot_model(entry_model, history_dir, today)
    snapshot_model(exit_model, history_dir, today)

    # 4. Noise floor
    label_noise_floor.run(
        db_path,
        data_dir / "noise_floor.json",
        kinds=["entry", "exit"],
    )

    logger.info("Weekly retrain complete.")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="pulse_bot.db")
    ap.add_argument("--data-dir", default="data/ml")
    ap.add_argument("--history-dir", default="data/ml/history")
    args = ap.parse_args()
    run(args.db, Path(args.data_dir), Path(args.history_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
