# pulse_bot/analyze_sensitivity.py
"""Sensitivity analysis of optimizer runs.

For each parameter in the grid, group runs by the parameter's value and
report:
  - mean / median / 10%-trimmed mean of the target metric
  - spread (max_group_mean - min_group_mean)
  - one-way ANOVA F-statistic (coarse ranking heuristic only — with 300k
    in-sample runs, F is inflated; focus on *effect size* instead)
  - survival rate: P(total_trades >= min_trades | value) — exposes when
    a value's runs are disproportionately filtered out
  - top-tail enrichment: lift in the top-1% and top-5% PnL runs vs. overall

Also performs conditional analysis for gated parameters:
  - exit_trailing_stop_{activation,distance}_pct → only when trailing_stop_enabled=True
  - fast_score_threshold → only when entry_mode in {'fast','both'}

Usage:
    python -m pulse_bot.analyze_sensitivity                     # latest session
    python -m pulse_bot.analyze_sensitivity <session_id>
    python -m pulse_bot.analyze_sensitivity --min-trades=5
    python -m pulse_bot.analyze_sensitivity --metric=roi_pct    # or total_pnl_sol / profit_factor
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
from collections import defaultdict
from typing import Any

import psycopg2
import psycopg2.extras

from pulse_bot.config import get_config
from pulse_bot.db import _resolve_dsn


def _pg_conn(db_path: str):
    """Legacy path → PG DSN."""
    return psycopg2.connect(_resolve_dsn(db_path))


logger = logging.getLogger(__name__)


def _sort_key(v: Any) -> tuple[int, Any]:
    """Sort numeric values numerically; leave strings/bools lexicographic."""
    if isinstance(v, bool):
        return (2, str(v))
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v))


def _latest_session(db_path: str) -> str | None:
    conn = _pg_conn(db_path)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT optimizer_session FROM optimization_runs "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _load_all_runs(db_path: str, session: str) -> list[dict[str, Any]]:
    """Load ALL runs for a session (no filtering). Filtering is applied later."""
    conn = _pg_conn(db_path)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT params, total_trades, total_pnl_sol, win_rate, "
                "profit_factor, roi_pct FROM optimization_runs "
                "WHERE optimizer_session = %s",
                (session,),
            )
            rows = cur.fetchall()
        runs: list[dict[str, Any]] = []
        for r in rows:
            try:
                params = json.loads(r["params"])
            except (json.JSONDecodeError, TypeError):
                continue
            runs.append(
                {
                    "params": params,
                    "total_trades": r["total_trades"] or 0,
                    "total_pnl_sol": r["total_pnl_sol"] or 0.0,
                    "win_rate": r["win_rate"] or 0.0,
                    "profit_factor": r["profit_factor"] or 0.0,
                    "roi_pct": r["roi_pct"] or 0.0,
                }
            )
        return runs
    finally:
        conn.close()


def _one_way_anova_f(groups: list[list[float]]) -> float:
    """Classic one-way ANOVA F-statistic (heuristic only, not inferential)."""
    groups = [g for g in groups if g]
    if len(groups) < 2:
        return 0.0
    all_vals = [v for g in groups for v in g]
    n_total = len(all_vals)
    k = len(groups)
    if n_total <= k:
        return 0.0
    grand_mean = sum(all_vals) / n_total
    group_means = [sum(g) / len(g) for g in groups]
    ss_between = sum(
        len(g) * (gm - grand_mean) ** 2 for g, gm in zip(groups, group_means)
    )
    ss_within = sum((v - gm) ** 2 for g, gm in zip(groups, group_means) for v in g)
    df_between = k - 1
    df_within = n_total - k
    if df_within <= 0 or ss_within <= 0:
        return 0.0
    return (ss_between / df_between) / (ss_within / df_within)


def _trimmed_mean(vals: list[float], trim: float = 0.1) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = int(len(s) * trim)
    s = s[k : len(s) - k] if len(s) - 2 * k > 0 else s
    return sum(s) / len(s)


def _analyze_param(
    runs_all: list[dict[str, Any]],
    runs_pass: list[dict[str, Any]],
    pname: str,
    metric: str,
    tail_cutoff: float,
) -> dict[str, Any]:
    """Per-value stats + spread + F + survival rate + top-tail enrichment."""
    # Top-tail set: runs with metric >= tail_cutoff. For PnL metrics this is
    # the winners; for a minimisation metric the caller would pass negative.
    top_tail = [r for r in runs_pass if r[metric] >= tail_cutoff]
    top_total = max(len(top_tail), 1)
    pass_total = max(len(runs_pass), 1)

    buckets_all: dict[Any, int] = defaultdict(int)
    buckets_pass: dict[Any, list[float]] = defaultdict(list)
    buckets_top: dict[Any, int] = defaultdict(int)

    for r in runs_all:
        if pname not in r["params"]:
            continue
        buckets_all[r["params"][pname]] += 1
    for r in runs_pass:
        if pname not in r["params"]:
            continue
        buckets_pass[r["params"][pname]].append(r[metric])
    for r in top_tail:
        if pname not in r["params"]:
            continue
        buckets_top[r["params"][pname]] += 1

    if len(buckets_pass) < 2:
        return {"param": pname, "skip": True}

    per_value: list[dict[str, Any]] = []
    means: list[float] = []
    for val in sorted(buckets_pass.keys(), key=_sort_key):
        vals = buckets_pass[val]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        median = statistics.median(vals)
        tmean = _trimmed_mean(vals, 0.1)
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        n_all = buckets_all.get(val, len(vals))
        survival = len(vals) / n_all if n_all else 0.0
        # Top-tail enrichment (lift = observed share / expected share).
        expected = len(vals) / pass_total
        observed = buckets_top.get(val, 0) / top_total
        lift = observed / expected if expected > 0 else 0.0
        per_value.append(
            {
                "value": val,
                "n": len(vals),
                "mean": mean,
                "median": median,
                "tmean": tmean,
                "std": std,
                "survival": survival,
                "top_lift": lift,
                "top_count": buckets_top.get(val, 0),
            }
        )
        means.append(mean)
    spread = max(means) - min(means) if means else 0.0
    f_stat = _one_way_anova_f(list(buckets_pass.values()))
    return {
        "param": pname,
        "skip": False,
        "spread": spread,
        "f_stat": f_stat,
        "values": per_value,
    }


def _conditional_runs(runs: list[dict[str, Any]], **conds: Any) -> list[dict[str, Any]]:
    """Return runs matching all key=value conditions."""

    def match(p: dict[str, Any]) -> bool:
        for k, cond in conds.items():
            v = p.get(k)
            if callable(cond):
                if not cond(v):
                    return False
            elif v != cond:
                return False
        return True

    return [r for r in runs if match(r["params"])]


def _print_param_row(v: dict[str, Any]) -> None:
    vals_str = " | ".join(
        f"{x['value']}→μ={x['mean']:+.4f} med={x['median']:+.4f} "
        f"surv={x['survival'] * 100:.0f}% lift={x['top_lift']:.2f}×"
        for x in v["values"]
    )
    print(
        f"  {v['param']:<36s} spread={v['spread']:+8.5f}  F={v['f_stat']:8.1f}  {vals_str}"
    )


def _print_main_report(
    runs_all: list[dict[str, Any]],
    runs_pass: list[dict[str, Any]],
    session: str,
    metric: str,
    min_trades: int,
) -> None:
    # Top-tail threshold: 99th percentile of metric across passing runs.
    vals = sorted(r[metric] for r in runs_pass)
    top_cutoff = vals[int(len(vals) * 0.99)] if vals else 0.0

    print("\n" + "=" * 130)
    print(
        f"  SENSITIVITY REPORT  session={session}  "
        f"metric={metric}  all={len(runs_all)}  pass={len(runs_pass)} "
        f"(min_trades>={min_trades})  top1%_cutoff={top_cutoff:.4f}"
    )
    print("=" * 130)
    print("  Unconditional analysis (one-way, averaged over all other params):\n")

    param_names: set[str] = set()
    for r in runs_all:
        param_names.update(r["params"].keys())

    rows = []
    for p in sorted(param_names):
        res = _analyze_param(runs_all, runs_pass, p, metric, top_cutoff)
        if not res.get("skip"):
            rows.append(res)
    rows.sort(key=lambda x: (x["spread"], x["f_stat"]), reverse=True)
    for r in rows:
        _print_param_row(r)

    print("\n  Conditional analysis (gated parameters, filtered contexts):\n")

    # Trailing-stop subparams — only meaningful when enabled=True.
    cond_pass = _conditional_runs(runs_pass, exit_trailing_stop_enabled=True)
    cond_all = _conditional_runs(runs_all, exit_trailing_stop_enabled=True)
    print(f"  [trailing_stop_enabled=True] all={len(cond_all)} pass={len(cond_pass)}")
    for p in ("exit_trailing_stop_activation_pct", "exit_trailing_stop_distance_pct"):
        res = _analyze_param(cond_all, cond_pass, p, metric, top_cutoff)
        if not res.get("skip"):
            _print_param_row(res)

    # fast_score_threshold — inert for entry_mode='full'
    cond_pass = _conditional_runs(runs_pass, entry_mode=lambda v: v in ("fast", "both"))
    cond_all = _conditional_runs(runs_all, entry_mode=lambda v: v in ("fast", "both"))
    print(f"\n  [entry_mode in fast,both] all={len(cond_all)} pass={len(cond_pass)}")
    res = _analyze_param(
        cond_all, cond_pass, "fast_score_threshold", metric, top_cutoff
    )
    if not res.get("skip"):
        _print_param_row(res)

    # (min,max)_entry_buyer pair analysis — 29 valid joint combos are the real axis.
    print("\n  Joint analysis: (min_entry, max_entry) pairs — top/bottom by mean:\n")
    pair_buckets: dict[tuple[Any, Any], list[float]] = defaultdict(list)
    for r in runs_pass:
        p = r["params"]
        pair_buckets[
            (p.get("min_entry_buyer_number"), p.get("max_entry_buyer_number"))
        ].append(r[metric])
    pair_stats = [
        (pair, sum(v) / len(v), len(v)) for pair, v in pair_buckets.items() if v
    ]
    pair_stats.sort(key=lambda x: x[1], reverse=True)
    print("    Top 5 pairs (best mean):")
    for (mn, mx), m, n in pair_stats[:5]:
        print(f"      min={mn:>2} max={mx:>2}  μ={m:+.5f}  n={n}")
    print("    Bottom 5 pairs (worst mean):")
    for (mn, mx), m, n in pair_stats[-5:]:
        print(f"      min={mn:>2} max={mx:>2}  μ={m:+.5f}  n={n}")

    print("\n" + "=" * 130)
    print(
        "Interpretation:\n"
        "  - Focus on spread (effect size in metric units), not F-stat — with 300k\n"
        "    in-sample runs, F is inflated even for trivial effects.\n"
        "  - survival% = P(total_trades>=min_trades | value). Very uneven numbers\n"
        "    indicate the value gates trade frequency, not profitability directly.\n"
        "  - lift>1 means that value is over-represented in the top 1% of runs;\n"
        "    a flat mean but high lift means the value matters at the best configs.\n"
        "  - Conditional rows are the ones to trust for gated params.\n"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = get_config()
    db_path = cfg.optimizer_db_path

    session: str | None = None
    min_trades = 5
    metric = "total_pnl_sol"
    for arg in sys.argv[1:]:
        if arg.startswith("--min-trades="):
            min_trades = int(arg.split("=")[1])
        elif arg.startswith("--metric="):
            metric = arg.split("=")[1]
            if metric == "pnl_sol":
                metric = "total_pnl_sol"
        elif arg.startswith("--"):
            continue
        else:
            session = arg

    if session is None:
        session = _latest_session(db_path)
        if session is None:
            print("No optimizer runs found in", db_path)
            sys.exit(1)
        print(f"Using latest session: {session}")

    runs_all = _load_all_runs(db_path, session)
    if not runs_all:
        print(f"No runs for session={session}")
        sys.exit(1)
    runs_pass = [r for r in runs_all if r["total_trades"] >= min_trades]

    _print_main_report(runs_all, runs_pass, session, metric, min_trades)


if __name__ == "__main__":
    main()
