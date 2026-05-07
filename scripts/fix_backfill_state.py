"""Cleanup ``data/backfill_state.json`` — keep only mints that actually have
backfill trades in the DB (``trades.market_cap_sol = 0`` is the backfill marker
since live PumpPortal events always carry a real market_cap).

Usage:
    python scripts/fix_backfill_state.py             # dry-run
    python scripts/fix_backfill_state.py --apply     # rewrite file (with .bak)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


async def main(apply: bool) -> int:
    state_path = REPO_ROOT / "data" / "backfill_state.json"
    if not state_path.exists():
        print(f"[!] state file not found: {state_path}", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text())
    mints = state.get("completed_mints") or []
    print(f"current completed_mints: {len(mints)}")

    dsn = os.environ.get("PULSE_PG_DSN")
    if not dsn:
        print("[!] PULSE_PG_DSN not set", file=sys.stderr)
        return 1
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT mint
            FROM trades
            WHERE market_cap_sol = 0
              AND mint = ANY($1::text[])
            """,
            mints,
        )
    finally:
        await conn.close()

    real = {r["mint"] for r in rows}
    keep = [m for m in mints if m in real]
    drop = [m for m in mints if m not in real]
    print(f"  keep (have backfill trades): {len(keep)}")
    print(f"  drop (false-completed):      {len(drop)}")
    print(f"  example dropped: {drop[:3]}")

    if not apply:
        print("\n(dry-run; pass --apply to rewrite)")
        return 0

    backup = state_path.with_suffix(
        state_path.suffix + f".bak.{int(time.time())}"
    )
    shutil.copyfile(state_path, backup)
    print(f"  backup written: {backup}")

    state["completed_mints"] = sorted(keep)
    state["cleanup_at"] = time.time()
    state["cleanup_dropped"] = len(drop)
    state_path.write_text(json.dumps(state, indent=2))
    print(f"  state file rewritten: {state_path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
