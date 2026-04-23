# scripts/diagnose_verify_mismatch.py
"""Diagnostic tool: compare live vs backtest rows in token_scores and show
which specific columns differ, for every mismatched mint.

Usage:
    .venv/bin/python scripts/diagnose_verify_mismatch.py [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EXCLUDE_COLS = {
    "id",
    "scored_at",
    "source",
    "created_at",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="verify300_scratch.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cols = [r[1] for r in conn.execute("PRAGMA table_info(token_scores)").fetchall()]
    compare_cols = [c for c in cols if c not in EXCLUDE_COLS]

    live = {r["mint"]: dict(r) for r in conn.execute(
        "SELECT * FROM token_scores WHERE source='live'"
    ).fetchall()}
    bt = {r["mint"]: dict(r) for r in conn.execute(
        "SELECT * FROM token_scores WHERE source='backtest'"
    ).fetchall()}
    conn.close()

    common = sorted(set(live) & set(bt))
    mismatches = []
    for mint in common:
        lv, bv = live[mint], bt[mint]
        diffs = {}
        for c in compare_cols:
            lvv, bvv = lv.get(c), bv.get(c)
            if lvv != bvv:
                diffs[c] = (lvv, bvv)
        if diffs:
            mismatches.append((mint, lv.get("symbol") or "", diffs))

    print(f"Total common tokens: {len(common)}")
    print(f"Mismatches:          {len(mismatches)}")
    print()
    for mint, sym, diffs in mismatches:
        print(f"── {sym:<14} {mint[:12]} ──")
        for col, (lvv, bvv) in sorted(diffs.items()):
            ls = str(lvv)[:50]
            bs = str(bvv)[:50]
            print(f"  {col:<30} live={ls!r:<52} bt={bs!r}")
        print()


if __name__ == "__main__":
    main()
