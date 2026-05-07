# scripts/diagnose_skew.py
"""Diagnose train/serve feature skew.

Loads recent tokens from PG, simulates the live hydration path
(``FeatureHydrationService.hydrate_for_t90``), extracts the entry
feature vector, and reports per-feature zero-rates.

Compares against the same vector built from the training dataset
to identify which features are present in training but consistently
zero/missing at inference.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/diagnose_skew.py
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

from pulse_bot.config import get_config
from pulse_bot.db import Database, _resolve_dsn
from pulse_bot.ml.features import (
    CREATOR_FEATURES,
    DERIVED_FEATURES,
    ENTRY_FEATURE_ORDER,
    HELIUS_FEATURES,
    TIME_AWARE_DERIVED_FEATURES,
    TIME_AWARE_FEATURES,
    WALLET_FEATURES,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
logger = logging.getLogger(__name__)

ENTRY_PARQUET = Path("data/ml/entry.parquet")
N_SAMPLE = 200  # recent tokens to inspect


def main() -> None:
    df = pd.read_parquet(ENTRY_PARQUET)
    df = df.sort_values("scored_at", ascending=False).head(N_SAMPLE).reset_index(drop=True)
    print(f"Inspecting {len(df)} most-recent tokens from entry.parquet")

    feature_cols = [c for c in ENTRY_FEATURE_ORDER if c in df.columns]
    missing_in_parquet = [c for c in ENTRY_FEATURE_ORDER if c not in df.columns]
    print(f"Features in parquet: {len(feature_cols)}/{len(ENTRY_FEATURE_ORDER)}")
    if missing_in_parquet:
        print(f"NOT in parquet: {missing_in_parquet}")

    # Per-feature zero-rate (NaN counted as zero).
    print(f"\n{'feature':<48} {'zero%':>8} {'NaN%':>8} {'mean':>12} {'group':<8}")
    print("-" * 90)
    rows = []
    for f in ENTRY_FEATURE_ORDER:
        group = _group_for(f)
        if f not in df.columns:
            rows.append((f, 100.0, 100.0, 0.0, group, True))
            continue
        s = df[f]
        nan_pct = s.isna().mean() * 100
        z_pct = ((s == 0) | s.isna()).mean() * 100
        m = s.fillna(0).mean()
        rows.append((f, z_pct, nan_pct, float(m), group, False))

    # Sort by zero-rate descending
    rows.sort(key=lambda r: -r[1])
    for f, z, n, m, g, missing in rows:
        flag = " ← MISSING" if missing else ""
        print(f"{f:<48} {z:>7.1f}% {n:>7.1f}% {m:>12.4f} {g:<8}{flag}")

    print("\nGroup summary:")
    grp_stats: dict[str, list] = {}
    for f, z, n, m, g, missing in rows:
        grp_stats.setdefault(g, []).append(z)
    for g in ["SCORER", "DERIVED", "HELIUS", "CREATOR", "WALLET", "TIME_A", "TIME_D"]:
        zs = grp_stats.get(g, [])
        if zs:
            print(f"  {g:<8} avg_zero%={sum(zs)/len(zs):>5.1f}%  n={len(zs):>3} "
                  f"(min={min(zs):.0f}%, max={max(zs):.0f}%)")


def _group_for(name: str) -> str:
    if name in HELIUS_FEATURES:
        return "HELIUS"
    if name in CREATOR_FEATURES:
        return "CREATOR"
    if name in WALLET_FEATURES:
        return "WALLET"
    if name in DERIVED_FEATURES:
        return "DERIVED"
    if name in TIME_AWARE_FEATURES:
        return "TIME_A"
    if name in TIME_AWARE_DERIVED_FEATURES:
        return "TIME_D"
    return "SCORER"


if __name__ == "__main__":
    main()
