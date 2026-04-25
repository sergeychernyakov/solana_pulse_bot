# scripts/enrich_backfilled_mc.py
"""Replay the pump.fun bonding curve to fill ``market_cap_sol`` and
``v_sol_in_bonding_curve`` for backfilled trade rows whose values are 0.

Why
---
``scripts/helius_backfill_graduated.py`` reconstructs the full pump.fun
trade history for graduated mints from the Helius parsed-tx API. That API
returns ``sol_amount``, ``token_amount``, ``tx_type``, ``wallet`` and
``timestamp`` — **but not bonding-curve state**. As a result, every
backfilled row lands with ``market_cap_sol = v_sol_in_bonding_curve = 0``,
which corrupts every feature that reads cap/curve at a point in time
(curve_progress_at_t30/60/90, sol_to_graduation, time-aware deltas,
exit-stack TP/SL anchoring against bonding-curve mc, etc.).

The pump.fun bonding curve is a constant-product AMM with fixed initial
virtual reserves that are identical across all pump.fun tokens:

    V_SOL_INIT  = 30 SOL
    V_TOK_INIT  = 1_073_000_000 tokens
    K           = V_SOL_INIT * V_TOK_INIT

Per WS-observed live trade we know: trader send-side ``sol_amount`` and
trader receive-side ``token_amount`` (post-fee). The virtual reserves
themselves move on the ``sol_amount`` delta only:

    buy:    v_sol += sol_amount
    sell:   v_sol -= sol_amount
    v_tok = K / v_sol           # invariant
    mc    = (v_sol / v_tok) * 1_000_000_000

Reported ``token_amount`` is the trader's *received* amount, which is
post 1 % fee — it is NOT equal to the virtual-reserve delta — so we
re-derive ``v_tok`` from the invariant rather than accumulating
``token_amount``.

How it runs
-----------
1. ``--verify`` mode: pick mints that have at least N rows with
   ``market_cap_sol > 0`` (live-collected ground truth), replay from
   initial reserves on the *full* trade stream (deduped), and report
   the average / max % drift between computed and stored mc on the
   live rows. We do not write anything.
2. ``--enrich`` mode: process only mints listed in
   ``data/backfill_state.json``'s ``completed_mints`` (so we never
   touch a mint that the backfill is still streaming into). For each
   such mint we replay from initial reserves and ``UPDATE`` only the
   rows that currently have ``market_cap_sol = 0``. Live rows are left
   untouched. The script is idempotent (re-running yields the same
   values) and resumable (per-mint progress in
   ``data/enrich_mc_state.json``).

Safety
------
* The bot (PID 39189) and the backfill (PID 465) both write to the
  same database. We never lock rows that the backfill might still be
  inserting into: we only operate on mints that the backfill itself
  has marked complete in ``backfill_state.json``.
* All writes happen in a per-mint transaction; on error we roll back.
* Default mode is verification — enrichment requires ``--enrich``.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pulse_bot.db import _resolve_dsn  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("enrich_mc")

# ── pump.fun bonding curve constants ──────────────────────────────────
V_SOL_INIT: float = 30.0
V_TOK_INIT: float = 1_073_000_000.0
K_INVARIANT: float = V_SOL_INIT * V_TOK_INIT
TOTAL_SUPPLY: float = 1_000_000_000.0

BACKFILL_STATE_PATH = REPO_ROOT / "data" / "backfill_state.json"
ENRICH_STATE_PATH = REPO_ROOT / "data" / "enrich_mc_state.json"


# ──────────────────────────────────────────────────────────────────────
# Replay arithmetic (pure; covered by unit tests)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplayedRow:
    """One trade after replay — what we'd write back into DB."""

    trade_id: int
    v_sol: float
    market_cap_sol: float


def replay_curve(
    trades: list[dict],
    *,
    v_sol_init: float = V_SOL_INIT,
    k_invariant: float = K_INVARIANT,
    total_supply: float = TOTAL_SUPPLY,
) -> list[ReplayedRow]:
    """Replay the pump.fun bonding curve over a chronological trade stream.

    Args:
        trades: List of dicts ordered by (``timestamp``, ``id``). Each
            dict must contain ``id``, ``tx_type`` (``buy`` / ``sell``)
            and ``sol_amount``.
        v_sol_init: Initial virtual SOL reserve.
        k_invariant: Constant product ``v_sol_init * v_tok_init``.
        total_supply: Token total supply used for mc calculation.

    Returns:
        One ``ReplayedRow`` per input trade (same order).
    """
    out: list[ReplayedRow] = []
    v_sol = float(v_sol_init)
    for t in trades:
        sol = float(t.get("sol_amount") or 0.0)
        if (t.get("tx_type") or "").lower() == "buy":
            v_sol += sol
        else:
            v_sol -= sol
        # Guard against pathological negative reserves from upstream noise.
        if v_sol <= 0:
            v_sol = 1e-9
        v_tok = k_invariant / v_sol
        mc = (v_sol / v_tok) * total_supply
        out.append(ReplayedRow(trade_id=int(t["id"]), v_sol=v_sol, market_cap_sol=mc))
    return out


# ──────────────────────────────────────────────────────────────────────
# Trade loader (with dedup of live⇄backfill duplicates)
# ──────────────────────────────────────────────────────────────────────


def load_trades_for_mint(
    conn: "psycopg2.extensions.connection", mint: str
) -> list[dict]:
    """Return chronologically ordered trades for ``mint``.

    The Helius backfill emits second-precision timestamps while the live
    WS collector emits sub-second floats. The backfill's dedup logic
    (``ts + sol_amount + wallet``) therefore mis-matches when the WS
    timestamp doesn't round to the Helius integer second, leaving every
    live row duplicated by a backfilled twin. We collapse those pairs
    by ``(tx_type, wallet, sol_amount, token_amount)`` within a 5 s
    window, preferring the row that already has ``market_cap_sol > 0``.

    Args:
        conn: Open psycopg2 connection.
        mint: Mint address.

    Returns:
        Deduped list of trade dicts, sorted by ``(timestamp, id)``.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT id, tx_type, wallet, sol_amount, token_amount,
                      market_cap_sol, v_sol_in_bonding_curve, timestamp
               FROM trades
               WHERE mint = %s
               ORDER BY timestamp ASC, id ASC""",
            (mint,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    # Group by structural key. Within each group, if any row has live data
    # (market_cap_sol > 0) we keep that one; otherwise we keep the earliest.
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (
            (r.get("tx_type") or "").lower(),
            r.get("wallet") or "",
            round(float(r.get("sol_amount") or 0.0), 9),
            round(float(r.get("token_amount") or 0.0), 3),
            int(float(r["timestamp"])),  # second-precision bucket
        )
        groups.setdefault(key, []).append(r)

    deduped: list[dict] = []
    for items in groups.values():
        live = [it for it in items if (it.get("market_cap_sol") or 0.0) > 0.0]
        chosen = live[0] if live else items[0]
        deduped.append(chosen)
    deduped.sort(key=lambda r: (float(r["timestamp"]), int(r["id"])))
    return deduped


# ──────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VerifyResult:
    """Verification outcome for a single mint."""

    mint: str
    n_total: int
    n_live: int
    avg_mc_pct_diff: float
    max_mc_pct_diff: float
    avg_vsol_pct_diff: float


def verify_mint(conn: "psycopg2.extensions.connection", mint: str) -> VerifyResult:
    """Replay a mint's trade stream and compare to the rows that already
    have stored ``market_cap_sol > 0`` (the live-collected ground truth).

    Args:
        conn: Open psycopg2 connection.
        mint: Mint address.

    Returns:
        Aggregate drift statistics. ``avg_mc_pct_diff`` should be a
        few percent for a healthy formula.
    """
    trades = load_trades_for_mint(conn, mint)
    replayed = replay_curve(trades)
    diffs_mc, diffs_vsol = [], []
    n_live = 0
    for trade, rep in zip(trades, replayed):
        stored_mc = float(trade.get("market_cap_sol") or 0.0)
        stored_vsol = float(trade.get("v_sol_in_bonding_curve") or 0.0)
        if stored_mc <= 0.0 or stored_vsol <= 0.0:
            continue
        n_live += 1
        diffs_mc.append(abs(rep.market_cap_sol - stored_mc) / stored_mc * 100.0)
        diffs_vsol.append(abs(rep.v_sol - stored_vsol) / stored_vsol * 100.0)

    if not diffs_mc:
        return VerifyResult(mint, len(trades), 0, 0.0, 0.0, 0.0)
    return VerifyResult(
        mint=mint,
        n_total=len(trades),
        n_live=n_live,
        avg_mc_pct_diff=sum(diffs_mc) / len(diffs_mc),
        max_mc_pct_diff=max(diffs_mc),
        avg_vsol_pct_diff=sum(diffs_vsol) / len(diffs_vsol),
    )


# ──────────────────────────────────────────────────────────────────────
# Enrichment writer (idempotent, per-mint transactional)
# ──────────────────────────────────────────────────────────────────────


def enrich_mint(conn: "psycopg2.extensions.connection", mint: str) -> int:
    """Replay ``mint`` and update zero-mc rows. Returns rows updated."""
    trades = load_trades_for_mint(conn, mint)
    replayed = replay_curve(trades)
    updates: list[tuple[float, float, int]] = []
    for trade, rep in zip(trades, replayed):
        stored_mc = float(trade.get("market_cap_sol") or 0.0)
        stored_vsol = float(trade.get("v_sol_in_bonding_curve") or 0.0)
        if stored_mc > 0.0 or stored_vsol > 0.0:
            continue  # don't overwrite live-collected truth
        updates.append((rep.market_cap_sol, rep.v_sol, rep.trade_id))
    if not updates:
        return 0
    with conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                "UPDATE trades SET market_cap_sol = %s, v_sol_in_bonding_curve = %s "
                "WHERE id = %s",
                updates,
                page_size=500,
            )
    return len(updates)


# ──────────────────────────────────────────────────────────────────────
# State helpers
# ──────────────────────────────────────────────────────────────────────


def _read_completed_backfill_mints() -> list[str]:
    if not BACKFILL_STATE_PATH.exists():
        return []
    state = json.loads(BACKFILL_STATE_PATH.read_text())
    return list(state.get("completed_mints") or [])


def _load_enrich_state() -> dict:
    if not ENRICH_STATE_PATH.exists():
        return {"enriched_mints": [], "started_at": time.time()}
    try:
        return json.loads(ENRICH_STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"enriched_mints": [], "started_at": time.time()}


def _save_enrich_state(state: dict) -> None:
    ENRICH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENRICH_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(ENRICH_STATE_PATH)


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


def _pick_verify_mints(conn: "psycopg2.extensions.connection", n: int) -> list[str]:
    """Pick ``n`` mints whose history has been fully backfilled.

    We restrict to ``backfill_state.json``'s completed list because those
    are the only mints for which trade #0 is the dev buy at the actual
    initial reserves. Mints captured by the live WS subscription almost
    always missed the first few seconds of trades and therefore start at
    an unknown offset — replay from initial reserves there is structurally
    impossible to verify against stored values.
    """
    completed = _read_completed_backfill_mints()
    if not completed:
        return []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT mint FROM trades
               WHERE mint = ANY(%s) AND market_cap_sol > 0
               GROUP BY mint
               HAVING COUNT(*) >= 30
               ORDER BY random()
               LIMIT %s""",
            (completed, n),
        )
        return [r["mint"] for r in cur.fetchall()]


def _print_sample_trajectory(
    conn: "psycopg2.extensions.connection", mint: str, n: int = 5
) -> None:
    """Show first n enriched rows after a write — for human sanity."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT tx_type, sol_amount, market_cap_sol, v_sol_in_bonding_curve, timestamp
               FROM trades WHERE mint = %s
               ORDER BY timestamp ASC, id ASC LIMIT %s""",
            (mint, n),
        )
        for r in cur.fetchall():
            logger.info(
                "    %s sol=%.4f -> v_sol=%.4f mc=%.2f",
                r["tx_type"],
                r["sol_amount"] or 0.0,
                r["v_sol_in_bonding_curve"] or 0.0,
                r["market_cap_sol"] or 0.0,
            )


def main() -> int:
    """Entry point. Without ``--enrich`` we only verify."""
    args = sys.argv[1:]
    do_enrich = "--enrich" in args
    batch_limit = 50
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            batch_limit = int(args[i + 1])

    dsn = _resolve_dsn(None)
    conn = psycopg2.connect(dsn)

    # ── Verification on 3 mints ───────────────────────────────────────
    verify_mints = _pick_verify_mints(conn, 3)
    logger.info("Verification on %d mints", len(verify_mints))
    results = [verify_mint(conn, m) for m in verify_mints]
    for r in results:
        logger.info(
            "  %s n_total=%d n_live=%d mc_avg=%.2f%% mc_max=%.2f%% vsol_avg=%.2f%%",
            r.mint,
            r.n_total,
            r.n_live,
            r.avg_mc_pct_diff,
            r.max_mc_pct_diff,
            r.avg_vsol_pct_diff,
        )
    healthy = all(r.n_live == 0 or r.avg_mc_pct_diff < 5.0 for r in results)
    if not healthy:
        logger.error(
            "Formula drift > 5%% — REFUSING to enrich. Inspect duplicates / "
            "fee constants before re-running."
        )
        return 2

    if not do_enrich:
        logger.info("Verification only (no --enrich). Done.")
        return 0

    # ── Enrichment over completed backfill mints ──────────────────────
    completed = _read_completed_backfill_mints()
    state = _load_enrich_state()
    done = set(state.get("enriched_mints") or [])
    todo = [m for m in completed if m not in done][:batch_limit]
    logger.info(
        "Enriching %d mints (of %d completed by backfill, %d already done)",
        len(todo),
        len(completed),
        len(done),
    )
    total_updated = 0
    for mint in todo:
        try:
            n = enrich_mint(conn, mint)
        except Exception:  # pragma: no cover — defensive logging
            logger.exception("enrich failed for %s", mint)
            continue
        total_updated += n
        done.add(mint)
        state["enriched_mints"] = sorted(done)
        _save_enrich_state(state)
        logger.info("  %s rows_updated=%d", mint, n)

    logger.info("Total rows updated: %d", total_updated)
    if todo:
        _print_sample_trajectory(conn, todo[0])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
