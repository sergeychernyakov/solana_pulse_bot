# pulse_bot/optimizer.py
"""Optimizer — precompute metrics once, cheap re-score + replay exits for each combo.

Uses the SAME FastFilter, Scorer, ExitManager, PulseMonitor code as live/backtest.
Only the data loading is cached — algorithms are identical. verify300 stays 100%.

Architecture:
  Phase 1 (once):  Load all tokens/trades from DB into memory, compute TokenMetrics
  Phase 2 (per combo): Re-run FastFilter + Scorer rules → entry decisions → timeline replay
"""

from __future__ import annotations

import copy
import heapq
import itertools
import json
import logging
import multiprocessing as mp
import os
import random
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from pulse_bot.config import PUMPFUN_GRADUATION_SOL
from pulse_bot.filters.fast import FastFilter
from pulse_bot.filters.metrics import MetricsCalculator
from pulse_bot.filters.scorer import Scorer
from pulse_bot.models import CreatorStats, Token, Trade

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database

logger = logging.getLogger(__name__)

DEFAULT_MAX_COMBOS = 500
TOP_N_RESULTS = 10
MIN_TRADES_FOR_RANK = 20  # Combos with fewer trades are kept in DB but excluded
# from displayed leaderboard/holdout: 0-trade combos have pnl=0 which "beats"
# every negative-pnl combo, so sorting by pnl_sol surfaces inert settings as
# false winners. Codex Apr-2026 review SMELL #2.

# Exit reasons whose slot is considered freed at the SAME-timestamp boundary
# (<=) to mirror BacktestEngine, which closes inactivity/max-hold exits in
# ``_close_expired_positions`` BEFORE admitting same-timestamp entries.
# All other (ExitManager-driven) exits fire in ``_handle_position_trade``
# AFTER same-timestamp entries are admitted, so their slots are freed only
# strictly after the exit (<). See codex review SMELL #2 (Apr 2026).
_SLOT_FREE_AT_BOUNDARY: frozenset[str] = frozenset(
    {"dead_token", "timeout", "backtest_end"}
)


def _slot_kind(exit_reason: str) -> int:
    """Heap-ordering key: 0 = free at boundary, 1 = free strictly after."""
    return 0 if exit_reason in _SLOT_FREE_AT_BOUNDARY else 1


def _pop_freed_slots(heap: list[tuple[float, int, float]], entry_time: float) -> float:
    """Pop positions whose slot is freed by ``entry_time``; return cash credit.

    Boundary behaviour depends on the exit kind: inactivity-like exits
    free the slot at ``exit_time == entry_time`` (mirrors BT's
    ``_close_expired_positions`` running before entries at the same tick);
    ExitManager-driven exits do not (mirrors BT's ``_handle_position_trade``
    running after entries at the same tick). Returns the sum of
    ``sol_received`` for every popped trade so the caller can restore
    cash before admitting new entries.
    """
    credit = 0.0
    while heap:
        exit_time, kind, sol_received = heap[0]
        if exit_time < entry_time or (exit_time == entry_time and kind == 0):
            heapq.heappop(heap)
            credit += sol_received
        else:
            break
    return credit


DEFAULT_GRID: dict[str, list] = {
    "entry_mode": ["fast", "full", "both"],
    "fast_observe_seconds": [3, 5, 8],
    # Full-observation window — previously fixed at config default (45s).
    # Sweep to find optimum: shorter catches early pumps, longer filters noise.
    "observe_seconds": [20, 30, 45, 60, 90],
    "fast_score_threshold": [10, 15, 25],
    "score_threshold_buy": [15, 20, 30],
    # Joint (min, max) buyer-window axis. Codex v5 review: independent
    # ``min_entry_buyer_number`` / ``max_entry_buyer_number`` axes produce
    # degenerate windows like ``[15, 15]`` (single-buyer pin) that starve
    # the sweep of trades and pollute top-N with 0-trade pnl=0 combos.
    # ``_iter_combos`` expands each tuple into the two underlying config
    # fields, and rejects any combo where ``min >= max``.
    "entry_buyer_window": [
        (1, 10),
        (3, 15),
        (5, 20),
        (5, 30),
        (5, 50),
        (8, 30),
        (10, 50),
        (15, 80),
        (20, 100),
    ],
    # Hard rug-filter gates (#62 from review). 0 = disabled; sweep finds the
    # combination that filters rugs without over-rejecting real pumps.
    "entry_min_unique_buyers_hard": [0, 5, 10, 20],
    "entry_min_sol_volume_hard": [0.0, 0.5, 1.0, 3.0],
    "entry_min_velocity_accel": [0.0, 1.2, 1.5, 2.0],
    # Data-driven anti-parabola gates (April 2026 attribution):
    # hard_stop trades had mean curve_accel +0.25 vs win mean -0.04 — high
    # positive acceleration catches the top of a pump. 100 = disabled.
    "entry_max_curve_acceleration": [0.5, 1.0, 2.0, 100.0],
    # Paradoxically high fast_score correlates with rug (stop mean 25.5,
    # win mean 19.9). 1000 = disabled.
    "entry_max_fast_score": [30, 40, 60, 1000],
    # Creator spamming many tokens same day → more stops (8.8 vs 6.9).
    "creator_max_tokens_today": [5, 10, 20, 1000],
    # Hard minimums at FAST-phase to kill dead-on-arrival tokens (data:
    # 92% of fast=BUY went to full=SKIP). 0 = disabled; tighter = fewer
    # but higher-quality fast entries.
    "fast_hard_min_volume_sol": [0.0, 1.0, 2.0, 3.0, 5.0],
    "fast_hard_min_unique_buyers": [0, 3, 5, 7, 10],
    # Helius top-holder concentration hard caps. Preliminary live data
    # shows top1 ≥ 80% has ~61% death rate. 100 = disabled. Applied only
    # when T+30 snapshot available (backtest joins at replay time).
    "entry_max_top1_holder_pct": [40, 60, 80, 100.0],
    "entry_max_top5_holder_pct": [50, 70, 90, 100.0],
    # Delta axes: change in top1_pct from T+30 to T+120. Positive = dev
    # accumulating (rug signal); negative = distributing (organic). 100
    # / -100 = disabled. Codex v8 recommendation.
    "entry_max_top1_delta_pct": [0.0, 5.0, 10.0, 100.0],
    "entry_min_top1_delta_pct": [-100.0, -10.0, -5.0, 0.0],
    # Creator-snapshot gates (#58). 1.0/0.0 = disabled; tighter values
    # reject worse creators. Only active when snapshot has ≥2 priors.
    "creator_rug_rate_reject": [1.0, 0.8, 0.5],
    "creator_min_graduation_rate": [0.0, 0.05, 0.15],
    "creator_min_age_days": [0.0, 1.0, 7.0],
    "exit_hard_stop_loss_pct": [15, 20, 30, 50],
    "exit_inactivity_seconds": [30, 60, 120, 300],
    "exit_take_profit_pct": [50, 100, 200],
    "exit_on_whale": [True, False],
    # Whale-exit absolute SOL threshold: tune in grid so sweep finds
    # the size at which a single sell reliably signals dump.
    "pulse_whale_exit_sol": [0.5, 1.0, 2.0],
    "exit_trailing_stop_enabled": [True, False],
    "exit_trailing_stop_activation_pct": [30, 50, 80],
    # Trailing-from-peak distance (#10 from review): tight values tuned
    # for pump.fun's fast reversals — 50% of peak is lenient, 15% tight.
    "exit_trailing_stop_distance_pct": [15, 30, 50],
    "pulse_dead_buy_rate": [0.05, 0.10, 0.20],
    # Relative buy-rate fade (#7 from review): exit when buy_rate drops
    # to N× peak. 0.0 = rule disabled. Smaller = fire on bigger drops.
    "exit_peak_buy_rate_drop_ratio": [0.0, 0.3, 0.5],
    # Sell/buy pressure ratio (#8 from review): exit when sell_rate
    # exceeds buy_rate by this factor. Lower = fire on milder flips.
    "exit_sell_pressure_ratio": [0.7, 1.0, 1.5],
    # Hard max hold (#9 from review): pump.fun rarely delivers after
    # 90s — short timers cut losers that never get going. 7200 = "disabled".
    "exit_max_hold_seconds": [20.0, 45.0, 90.0, 7200.0],
    # ``buy_amount_sol`` is a risk-sizing knob, not an optimization target:
    # priority-fee math couples absolute PnL to the bet size non-linearly,
    # so letting the grid pick it maximizes absolute PnL under unenforced
    # leverage rather than best EV (codex Apr-2026 review BUG #1).
}


@dataclass
class CachedToken:
    """Retained data for exit simulation of one entered token."""

    token: Token
    monitor_trades: list[Trade]  # trades after the full-observe window
    creator_snapshot: CreatorStats | None
    creator_tokens_today: int
    entry_price: float  # price at end of full observation


@dataclass
class _TokenRecord:
    """Pre-loaded token data; shared read-only across worker processes via fork COW."""

    token: Token
    all_trades: list[Trade]
    full_trades: list[Trade]
    monitor_trades: list[Trade]
    creator_snapshot: CreatorStats | None
    creator_tokens_today: int
    entry_price: float
    holder_snapshot: dict | None = None  # T+30 snapshot from token_holders_snapshots


# ── Worker process state (populated by _worker_init via fork COW) ────
_WORKER_DATA: dict[str, Any] = {}


def _worker_init(dataset: list[_TokenRecord], base_cfg: PulseBotConfig) -> None:
    """Initialize worker-local globals. Called once per worker process."""
    _WORKER_DATA["dataset"] = dataset
    _WORKER_DATA["by_mint"] = {rec.token.mint: rec for rec in dataset}
    _WORKER_DATA["base_cfg"] = base_cfg


def _worker_run_combo(task: tuple[str, dict]) -> tuple[str, dict, list[dict]]:
    """Run one combo end-to-end in a worker: Phase 1 eval + Phase 2 sim.

    Returns (run_id, params, closed_trades). Master builds the final
    optimizer-result dict and writes to DB — workers do no I/O.
    """
    from pulse_bot.core import decide_entry

    run_id, params = task
    base_cfg: PulseBotConfig = _WORKER_DATA["base_cfg"]
    dataset: list[_TokenRecord] = _WORKER_DATA["dataset"]
    by_mint: dict[str, _TokenRecord] = _WORKER_DATA["by_mint"]

    cfg = copy.copy(base_cfg)
    for k, v in params.items():
        setattr(cfg, k, v)
    fast_filter = FastFilter(cfg)
    scorer = Scorer(cfg, None)  # db unused: creator stats always pre-fetched

    candidates: list[tuple[float, str, str, int, float]] = []
    for rec in dataset:
        fast_end = rec.token.created_at + cfg.fast_observe_seconds
        fast_trades = [t for t in rec.all_trades if t.timestamp <= fast_end]
        fast_result = fast_filter.evaluate(rec.token, fast_trades)
        full_result = scorer.score(
            rec.token,
            rec.full_trades,
            creator_snapshot=rec.creator_snapshot,
            creator_tokens_today=rec.creator_tokens_today,
            holder_snapshot=rec.holder_snapshot,
        )
        should_enter, entry_type, score, _ = decide_entry(fast_result, full_result, cfg)
        if not should_enter or rec.entry_price <= 0:
            continue
        entry_time = (
            rec.full_trades[-1].timestamp
            if rec.full_trades
            else rec.token.created_at + cfg.observe_seconds
        )
        candidates.append(
            (entry_time, rec.token.mint, entry_type, score, rec.entry_price)
        )

    # Phase 2: portfolio-aware event-driven sim
    candidates.sort(key=lambda c: c[0])
    active_exit_heap: list[tuple[float, int, float]] = []
    closed: list[dict] = []
    cash = cfg.portfolio_initial_sol
    for entry_time, mint, entry_type, entry_score, entry_price in candidates:
        cash += _pop_freed_slots(active_exit_heap, entry_time)
        if len(active_exit_heap) >= cfg.portfolio_max_positions:
            continue
        if cash < cfg.buy_amount_sol:
            continue
        rec = by_mint.get(mint)
        if rec is None:
            continue
        cached = CachedToken(
            token=rec.token,
            monitor_trades=rec.monitor_trades,
            creator_snapshot=rec.creator_snapshot,
            creator_tokens_today=rec.creator_tokens_today,
            entry_price=rec.entry_price,
        )
        result = Optimizer._simulate_trade_from(
            cached,
            entry_type,
            entry_score,
            entry_price,
            entry_time,
            cfg,
        )
        closed.append(result)
        cash -= cfg.buy_amount_sol
        heapq.heappush(
            active_exit_heap,
            (
                result["exit_time"],
                _slot_kind(result["exit_reason"]),
                result["sol_received"],
            ),
        )
    return run_id, params, closed


class Optimizer:
    """Event-driven grid search.

    Runs the same FastFilter / Scorer / decide_entry / PaperTradeRunner as
    live — algorithms are identical; only data sourcing + orchestration differ.

    Workflow:
      1. Snapshot live DB via sqlite3 backup API (read-only, no contention).
      2. Stream tokens from snapshot in created_at order; per token, evaluate
         every combo and emit (entry_time, …) candidates. Only tokens entered
         by ≥1 combo are kept in RAM (for later exit replay).
      3. Per combo, sort candidates by entry_time and simulate with a
         min-heap of active exit_times to enforce portfolio_max_positions
         correctly.
      4. Stream-write each run to the optimizer DB; keep only a bounded
         top-N heap of summaries in memory.
    """

    def __init__(self, base_config: PulseBotConfig, db: Database) -> None:
        self._base_cfg = base_config
        self._db = db
        self._grid: dict[str, list] = {}
        self._session_id = f"opt_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self._metrics_calc = MetricsCalculator(graduation_sol=PUMPFUN_GRADUATION_SOL)
        self._snapshot_path: str | None = None
        # Walk-forward split threshold: tokens with ``created_at < split_ts``
        # are the training set used for grid search; tokens >= split_ts are
        # the holdout used to evaluate top-K combos out-of-sample. ``None``
        # disables the split (pre-walk-forward behaviour).
        self._split_ts: float | None = None
        # Filtered top-N snapshot produced by the last run: populated by
        # _run_serial / _run_parallel, consumed by _evaluate_holdout so the
        # holdout eval never sees 0-trade combos even though the raw return
        # value does (the raw list exists for parity tests).
        self._ranked_top_n: list[dict] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    def set_grid(self, grid: dict[str, list]) -> None:
        """Set custom parameter grid."""
        valid_fields = {f.name for f in fields(self._base_cfg)}
        for key in grid:
            if key not in valid_fields:
                logger.warning("Unknown config field in grid: %s (skipping)", key)
        self._grid = {k: v for k, v in grid.items() if k in valid_fields}

    def use_default_grid(self) -> None:
        self._grid = dict(DEFAULT_GRID)

    def estimate_runs(self) -> int:
        if not self._grid:
            return 0
        total = 1
        for v in self._grid.values():
            total *= len(v)
        return total

    def run(
        self,
        max_combos: int = DEFAULT_MAX_COMBOS,
        workers: int = 1,
        train_pct: float = 1.0,
    ) -> list[dict]:
        """Run optimizer.

        max_combos: 0 = full grid (materialised, warned if huge),
                    >0 = reservoir random sample without full cartesian in RAM.
        workers:    1 = serial (original path).
                    >1 = multiprocessing fork pool: each worker handles its
                         own subset of combos end-to-end over a shared
                         (fork-COW) pre-loaded token dataset.
        train_pct:  1.0 = no holdout (legacy behaviour).
                    <1.0 = chronological split — earliest ``train_pct`` of
                    tokens by ``created_at`` become the training set used for
                    grid search; the remainder is replayed against top-N
                    combos and printed as an out-of-sample report so we can
                    see whether the in-sample winners still win.

        Returns top-N summaries; each individual run is streamed to the
        optimizer DB as it finishes.
        """
        if not self._grid:
            self.use_default_grid()

        # Pre-flight: show signal-universe quality so the user knows upfront
        # whether fast/full/both modes actually have forward returns.
        self._print_signal_universe_diagnostic()

        # Build combos up front — per-combo contexts persist through Phases 1 & 2.
        if max_combos > 0:
            combos = self._sample_combos(max_combos)
            logger.info("Sampled %d combos", len(combos))
        else:
            combos = list(self._iter_combos())
            if len(combos) > 50_000:
                logger.warning(
                    "Full grid of %d combos materialised — this is memory-heavy; "
                    "consider --max-combos=N for random search",
                    len(combos),
                )
            logger.info("Full grid: %d combos", len(combos))
        total = len(combos)
        if total == 0:
            logger.warning("No valid combos to run.")
            return []

        self._snapshot_path = self._create_snapshot()
        try:
            if 0.0 < train_pct < 1.0:
                self._split_ts = self._compute_split_ts(train_pct)
                logger.info(
                    "Walk-forward: train=earliest %.0f%% of tokens, "
                    "holdout=latest %.0f%% (split_ts=%.0f)",
                    train_pct * 100,
                    (1 - train_pct) * 100,
                    self._split_ts or 0.0,
                )
            else:
                self._split_ts = None
            if workers > 1:
                top_results = self._run_parallel(combos, workers)
            else:
                top_results = self._run_serial(combos)
            # Marginal analysis runs against ALL sweep results (from DB).
            self._print_marginal_analysis()
            walk_forward_pairs: list[tuple[dict, dict]] | None = None
            if self._split_ts is not None:
                # Holdout consumes the filtered ranked set; raw top_results
                # is still returned to the caller (tests / parity checks).
                walk_forward_pairs = self._evaluate_holdout(self._ranked_top_n)
                # K-fold robustness: re-simulate top-N across 5 chronological
                # windows of the full dataset. A single holdout can get lucky
                # on one time slice; we want configs that survive multiple.
                self._print_kfold_robustness(self._ranked_top_n[:10], k=5)
            # Guardrail consumes the SAME ranked set as walk-forward — not
            # the raw top_heap (which can include 0-trade combos). Previously
            # passing top_results produced false "sample too small" alarms
            # when ranked combos actually had plenty of trades.
            self._print_overfit_guardrails(self._ranked_top_n, walk_forward_pairs)
            return top_results
        finally:
            self._cleanup_snapshot()

    def _compute_split_ts(self, train_pct: float) -> float:
        """Return the ``created_at`` threshold that puts ``train_pct`` of
        tokens strictly below it — used to split train/holdout chronologically.
        """
        assert self._snapshot_path is not None
        conn = sqlite3.connect(self._snapshot_path)
        try:
            rows = conn.execute(
                "SELECT created_at FROM tokens " "ORDER BY created_at ASC, mint ASC"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return 0.0
        idx = max(0, min(len(rows) - 1, int(len(rows) * train_pct)))
        return float(rows[idx][0])

    def _run_serial(self, combos: list[dict]) -> list[dict]:
        """Original single-process path. Untouched for verify-parity safety."""
        total = len(combos)
        combo_contexts: list[dict[str, Any]] = []
        for i, params in enumerate(combos):
            cfg = self._make_config(params)
            combo_contexts.append(
                {
                    "idx": i,
                    "params": params,
                    "cfg": cfg,
                    "fast_filter": FastFilter(cfg),
                    "scorer": Scorer(cfg, None),
                    "candidates": [],
                    "run_id": f"{self._session_id}_r{i:04d}",
                }
            )

        logger.info("Phase 1: streaming tokens from snapshot...")
        start = time.time()
        subset = "train" if self._split_ts is not None else "all"
        kept_tokens = self._stream_and_evaluate(combo_contexts, subset=subset)
        phase1 = time.time() - start
        candidate_count = sum(len(ctx["candidates"]) for ctx in combo_contexts)
        logger.info(
            "Phase 1 done in %.1fs: %d tokens entered by at least one combo, "
            "%d candidates across combos",
            phase1,
            len(kept_tokens),
            candidate_count,
        )

        logger.info("Phase 2: simulating %d combos...", total)
        # Two parallel heaps:
        #   top_heap     — raw (every combo eligible) so single-combo / parity
        #                  callers (tests) still see their result in the return.
        #   ranked_heap  — filtered at push time by MIN_TRADES_FOR_RANK so the
        #                  printed leaderboard / holdout eval cannot be
        #                  displaced by 0-trade "pnl=0" combos (codex v4 BUG).
        top_heap: list[tuple[float, int, dict]] = []
        ranked_heap: list[tuple[float, int, dict]] = []
        completed = 0
        start_wall = time.time()

        for ctx in combo_contexts:
            i = ctx["idx"]
            params = ctx["params"]
            try:
                closed = self._simulate_combo_event_driven(ctx, kept_tokens)
                run_data = self._build_result(ctx["run_id"], params, ctx["cfg"], closed)
                self._db.save_optimization_run(run_data)
                completed += 1

                pnl = run_data["total_pnl_sol"] or 0.0
                summary = {
                    "run_id": run_data["run_id"],
                    "params": run_data["params"],
                    "total_trades": run_data["total_trades"],
                    "win_rate": run_data["win_rate"],
                    "total_pnl_sol": run_data["total_pnl_sol"],
                    "profit_factor": run_data["profit_factor"],
                    "roi_pct": run_data["roi_pct"],
                }
                if len(top_heap) < TOP_N_RESULTS:
                    heapq.heappush(top_heap, (pnl, i, summary))
                elif pnl > top_heap[0][0]:
                    heapq.heapreplace(top_heap, (pnl, i, summary))
                if run_data["total_trades"] >= MIN_TRADES_FOR_RANK:
                    if len(ranked_heap) < TOP_N_RESULTS:
                        heapq.heappush(ranked_heap, (pnl, i, summary))
                    elif pnl > ranked_heap[0][0]:
                        heapq.heapreplace(ranked_heap, (pnl, i, summary))

                if (i + 1) % 10 == 0 or i == 0:
                    logger.info(
                        "[%d/%d] trades=%d wr=%.0f%% pnl=%+.6f SOL | %s",
                        i + 1,
                        total,
                        run_data["total_trades"],
                        run_data["win_rate"],
                        run_data["total_pnl_sol"],
                        " ".join(f"{k}={v}" for k, v in params.items()),
                    )
                # Release per-combo state ASAP.
                ctx["candidates"] = []
                del run_data
            except Exception:
                logger.exception("[%d/%d] Failed: %s", i + 1, total, params)

        elapsed = time.time() - start_wall
        top_results = [s for _, _, s in sorted(top_heap, reverse=True)]
        ranked_results = [s for _, _, s in sorted(ranked_heap, reverse=True)]
        self._ranked_top_n = ranked_results
        logger.info(
            "Optimizer done: %d/%d runs in %.1fs (phase1=%.1fs), session=%s",
            completed,
            total,
            elapsed,
            phase1,
            self._session_id,
        )
        self._print_top_results(ranked_results)
        return top_results

    def _run_parallel(self, combos: list[dict], workers: int) -> list[dict]:
        """Multiprocessing fork-pool path.

        Pre-loads the full token dataset once in the master process, then
        spawns ``workers`` processes via the fork context so children
        inherit the dataset read-only via copy-on-write. Each worker owns
        a subset of combos and runs Phase 1 eval + Phase 2 sim locally;
        master consumes results via ``imap_unordered``, writes to the
        optimizer DB, and maintains a top-N heap.
        """
        total = len(combos)

        logger.info("Preloading token dataset into memory...")
        t0 = time.time()
        subset = "train" if self._split_ts is not None else "all"
        dataset = self._preload_dataset(subset=subset)
        preload = time.time() - t0
        logger.info("Preload done in %.1fs (%d tokens)", preload, len(dataset))

        tasks = [
            (f"{self._session_id}_r{i:04d}", params) for i, params in enumerate(combos)
        ]
        _params_by_run = {t[0]: t[1] for t in tasks}  # noqa: F841

        # Raw + ranked heaps — see _run_serial for rationale.
        top_heap: list[tuple[float, int, dict]] = []
        ranked_heap: list[tuple[float, int, dict]] = []
        completed = 0
        start_wall = time.time()

        try:
            ctx = mp.get_context("fork")
        except ValueError:
            logger.warning("fork context unavailable, falling back to spawn")
            ctx = mp.get_context("spawn")

        logger.info(
            "Phase 1+2 parallel: %d combos across %d workers...",
            total,
            workers,
        )
        with ctx.Pool(
            processes=workers,
            initializer=_worker_init,
            initargs=(dataset, self._base_cfg),
        ) as pool:
            chunksize = max(1, total // (workers * 8))
            for run_id, params, closed in pool.imap_unordered(
                _worker_run_combo,
                tasks,
                chunksize=chunksize,
            ):
                try:
                    cfg = self._make_config(params)
                    run_data = self._build_result(run_id, params, cfg, closed)
                    self._db.save_optimization_run(run_data)
                    completed += 1

                    pnl = run_data["total_pnl_sol"] or 0.0
                    summary = {
                        "run_id": run_data["run_id"],
                        "params": run_data["params"],
                        "total_trades": run_data["total_trades"],
                        "win_rate": run_data["win_rate"],
                        "total_pnl_sol": run_data["total_pnl_sol"],
                        "profit_factor": run_data["profit_factor"],
                        "roi_pct": run_data["roi_pct"],
                    }
                    if len(top_heap) < TOP_N_RESULTS:
                        heapq.heappush(top_heap, (pnl, completed, summary))
                    elif pnl > top_heap[0][0]:
                        heapq.heapreplace(top_heap, (pnl, completed, summary))
                    if run_data["total_trades"] >= MIN_TRADES_FOR_RANK:
                        if len(ranked_heap) < TOP_N_RESULTS:
                            heapq.heappush(ranked_heap, (pnl, completed, summary))
                        elif pnl > ranked_heap[0][0]:
                            heapq.heapreplace(ranked_heap, (pnl, completed, summary))

                    if completed % 50 == 0 or completed == 1:
                        logger.info(
                            "[%d/%d] trades=%d wr=%.0f%% pnl=%+.6f SOL",
                            completed,
                            total,
                            run_data["total_trades"],
                            run_data["win_rate"],
                            run_data["total_pnl_sol"],
                        )
                except Exception:
                    logger.exception(
                        "Failed to consume worker result for %s",
                        run_id,
                    )

        elapsed = time.time() - start_wall
        top_results = [s for _, _, s in sorted(top_heap, reverse=True)]
        ranked_results = [s for _, _, s in sorted(ranked_heap, reverse=True)]
        self._ranked_top_n = ranked_results
        logger.info(
            "Optimizer done: %d/%d runs in %.1fs (preload=%.1fs, workers=%d), session=%s",
            completed,
            total,
            elapsed,
            preload,
            workers,
            self._session_id,
        )
        self._print_top_results(ranked_results)
        return top_results

    def _iter_combos(self) -> Iterator[dict]:
        """Lazy generator of valid combos.

        Expands the joint ``entry_buyer_window=(min, max)`` axis into the
        two underlying config fields and drops degenerate windows where
        ``min >= max`` (codex v5: ``[15, 15]``-style pins starve trades
        and pollute top-N with 0-trade combos). Also tolerates grids
        that still use the legacy independent
        ``min_entry_buyer_number`` / ``max_entry_buyer_number`` axes —
        those are subject to the same ``min >= max`` drop.

        Collapses no-op dimensions per ``entry_mode``: fast-window params
        (``fast_observe_seconds``, ``fast_score_threshold``) have zero effect
        in ``full`` runs because ``decide_entry`` ignores ``fast_result``
        there — canonicalising them prevents semantically identical combos
        from polluting top-N results (codex SMELL #1, Apr 2026).
        """
        param_names = list(self._grid.keys())
        param_values = list(self._grid.values())
        base_min = self._base_cfg.min_entry_buyer_number
        base_max = self._base_cfg.max_entry_buyer_number
        full_mode_noops = ("fast_observe_seconds", "fast_score_threshold")
        seen: set[tuple] = set()
        for combo in itertools.product(*param_values):
            params = dict(zip(param_names, combo))
            window = params.pop("entry_buyer_window", None)
            if window is not None:
                win_min, win_max = window
                params["min_entry_buyer_number"] = win_min
                params["max_entry_buyer_number"] = win_max
            win_min = params.get("min_entry_buyer_number", base_min)
            win_max = params.get("max_entry_buyer_number", base_max)
            if win_min >= win_max:
                continue
            if params.get("entry_mode") == "full":
                for key in full_mode_noops:
                    if key in self._grid:
                        params[key] = self._grid[key][0]
            fingerprint = tuple(sorted(params.items()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            yield params

    def _sample_combos(self, k: int) -> list[dict]:
        """Generate k unique combos by independent per-axis random sampling.

        The previous reservoir approach iterated the full Cartesian product,
        which is fine for small grids but becomes the bottleneck at hundreds
        of billions of combos — even the lazy generator can't keep up.
        This direct sampler skips enumeration entirely: draw one value per
        axis, canonicalise through the same filters as ``_iter_combos``
        (window expansion, degenerate-window drop, full-mode no-op collapse,
        fingerprint dedup), retry on duplicates until we have k.

        Fails fast if the feasible combo set is smaller than k (after
        ``max_attempts_multiplier * k`` tries without reaching the goal we
        return whatever we have, rather than looping forever).
        """
        param_names = list(self._grid.keys())
        param_values = list(self._grid.values())
        base_min = self._base_cfg.min_entry_buyer_number
        base_max = self._base_cfg.max_entry_buyer_number
        full_mode_noops = ("fast_observe_seconds", "fast_score_threshold")

        seen: set[tuple] = set()
        sampled: list[dict] = []
        max_attempts = k * 20
        attempts = 0
        while len(sampled) < k and attempts < max_attempts:
            attempts += 1
            combo = tuple(random.choice(vals) for vals in param_values)  # nosec B311
            params = dict(zip(param_names, combo))
            window = params.pop("entry_buyer_window", None)
            if window is not None:
                win_min, win_max = window
                params["min_entry_buyer_number"] = win_min
                params["max_entry_buyer_number"] = win_max
            win_min = params.get("min_entry_buyer_number", base_min)
            win_max = params.get("max_entry_buyer_number", base_max)
            if win_min >= win_max:
                continue
            if params.get("entry_mode") == "full":
                for key in full_mode_noops:
                    if key in self._grid:
                        params[key] = self._grid[key][0]
            fingerprint = tuple(sorted(params.items()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            sampled.append(params)
        return sampled

    # ── Snapshot helpers ─────────────────────────────────────

    def _create_snapshot(self) -> str:
        """Copy live DB into a temp snapshot via sqlite3 backup API.

        Reading from a snapshot decouples the optimizer from the live
        collector — no long-held read transactions on the writer's DB.
        """
        src = self._base_cfg.db_path
        fd, path = tempfile.mkstemp(prefix="opt_snapshot_", suffix=".db")
        os.close(fd)
        t0 = time.time()
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        logger.info("Snapshot ready in %.1fs (%s → %s)", time.time() - t0, src, path)
        return path

    def _cleanup_snapshot(self) -> None:
        path = self._snapshot_path
        if not path:
            return
        for ext in ("", "-wal", "-shm"):
            side = path + ext
            try:
                if os.path.exists(side):
                    os.unlink(side)
            except OSError as exc:
                logger.warning("Snapshot cleanup failed for %s: %s", side, exc)
        self._snapshot_path = None

    # ── Dataset preload (for parallel path) ──────────────────

    def _preload_dataset(self, subset: str = "all") -> list[_TokenRecord]:
        """Load every token + trades + creator stats into memory once.

        Used by the multiprocessing path so worker processes inherit the
        dataset via fork copy-on-write instead of each re-reading SQLite.
        ``subset`` filters tokens by the walk-forward split: ``"train"``
        keeps only ``created_at < _split_ts``, ``"holdout"`` keeps only
        ``created_at >= _split_ts``, ``"all"`` skips the filter.
        """
        assert self._snapshot_path is not None
        snap = self._snapshot_path
        full_sec = self._base_cfg.observe_seconds
        split_ts = self._split_ts

        records: list[_TokenRecord] = []
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            token_cursor = conn.execute(
                "SELECT mint, name, symbol, creator, created_at, uri "
                "FROM tokens ORDER BY created_at ASC, mint ASC"
            )
            processed = 0
            for trow in token_cursor:
                ts = trow["created_at"]
                if split_ts is not None:
                    # Burn-in: a train token whose full-observation window
                    # (ts .. ts + full_sec) crosses split_ts would be scored
                    # on a truncated observation — skip it entirely so only
                    # tokens with a complete in-sample decision horizon rank.
                    if subset == "train" and ts + full_sec >= split_ts:
                        continue
                    if subset == "holdout" and ts < split_ts:
                        continue
                token = Token(
                    mint=trow["mint"],
                    name=trow["name"] or "",
                    symbol=trow["symbol"] or "",
                    creator=trow["creator"] or "",
                    created_at=trow["created_at"],
                    uri=trow["uri"] or "",
                    launchpad="pumpfun",
                )
                trade_rows = conn.execute(
                    "SELECT wallet, tx_type, sol_amount, token_amount, "
                    "v_sol_in_bonding_curve, market_cap_sol, timestamp, is_creator "
                    "FROM trades WHERE mint = ? ORDER BY timestamp ASC",
                    (token.mint,),
                ).fetchall()
                processed += 1
                if not trade_rows:
                    continue

                all_trades = [
                    Trade(
                        mint=token.mint,
                        wallet=r["wallet"],
                        tx_type=r["tx_type"],
                        sol_amount=r["sol_amount"] or 0.0,
                        token_amount=(
                            r["token_amount"] if "token_amount" in r.keys() else 0.0
                        )
                        or 0.0,
                        new_token_balance=0.0,
                        bonding_curve_key="",
                        v_sol_in_bonding_curve=r["v_sol_in_bonding_curve"] or 0.0,
                        v_tokens_in_bonding_curve=0.0,
                        market_cap_sol=r["market_cap_sol"] or 0.0,
                        timestamp=r["timestamp"],
                        is_creator=bool(r["is_creator"]),
                    )
                    for r in trade_rows
                ]
                # Walk-forward: in train subset, drop any trade >= split_ts
                # so exit simulation cannot peek into holdout.
                if subset == "train" and split_ts is not None:
                    all_trades = [t for t in all_trades if t.timestamp < split_ts]
                    if not all_trades:
                        continue
                full_end = token.created_at + full_sec
                full_trades = [t for t in all_trades if t.timestamp <= full_end]
                monitor_trades = [t for t in all_trades if t.timestamp > full_end]

                creator_snapshot = self._db.get_creator_stats_as_of_sync(
                    token.creator,
                    token.mint,
                    source_db_path=snap,
                )
                creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                    token.creator,
                    token.mint,
                    source_db_path=snap,
                )

                entry_price = 0.0
                for t in reversed(full_trades):
                    if t.tx_type == "buy" and t.token_amount > 0 and t.sol_amount > 0:
                        entry_price = t.sol_amount / t.token_amount
                        break

                # Holder snapshots: pull T+30 and T+120 for delta signal
                # (codex v8 audit recommendation — derivative separates
                # "still concentrated" from "distributing").
                h30 = conn.execute(
                    """SELECT top1_pct, top5_pct, top10_pct, holder_count
                       FROM token_holders_snapshots
                       WHERE mint=? AND capture_at_age_sec=30.0
                         AND is_negative_row=0 AND top1_pct IS NOT NULL
                       LIMIT 1""",
                    (token.mint,),
                ).fetchone()
                h120 = conn.execute(
                    """SELECT top1_pct, top5_pct
                       FROM token_holders_snapshots
                       WHERE mint=? AND capture_at_age_sec=120.0
                         AND is_negative_row=0 AND top1_pct IS NOT NULL
                       LIMIT 1""",
                    (token.mint,),
                ).fetchone()
                holder_snap: dict | None = None
                if h30:
                    holder_snap = dict(h30)
                    if h120:
                        holder_snap["top1_delta_pct"] = (h120["top1_pct"] or 0.0) - (
                            h30["top1_pct"] or 0.0
                        )
                        holder_snap["top5_delta_pct"] = (h120["top5_pct"] or 0.0) - (
                            h30["top5_pct"] or 0.0
                        )

                records.append(
                    _TokenRecord(
                        token=token,
                        all_trades=all_trades,
                        full_trades=full_trades,
                        monitor_trades=monitor_trades,
                        creator_snapshot=creator_snapshot,
                        creator_tokens_today=creator_tokens_today,
                        entry_price=entry_price,
                        holder_snapshot=holder_snap,
                    )
                )
                if processed % 500 == 0:
                    logger.info(
                        "Preload progress: %d tokens scanned, %d loaded",
                        processed,
                        len(records),
                    )
        finally:
            conn.close()
        logger.info("Preload done: %d token records", len(records))
        return records

    # ── Phase 1: stream tokens + emit per-combo candidates ───

    def _stream_and_evaluate(
        self,
        combo_contexts: list[dict[str, Any]],
        subset: str = "all",
    ) -> dict[str, CachedToken]:
        """Walk the snapshot once, evaluating every combo per token.

        Tokens for which no combo decides to enter are dropped — we keep only
        the monitor-window trades needed for later exit replay. ``subset``
        applies the same walk-forward filter as ``_preload_dataset``.
        """
        assert self._snapshot_path is not None
        snap = self._snapshot_path
        full_sec = self._base_cfg.observe_seconds
        split_ts = self._split_ts
        from pulse_bot.core import decide_entry

        kept: dict[str, CachedToken] = {}
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        try:
            token_cursor = conn.execute(
                "SELECT mint, name, symbol, creator, created_at, uri "
                "FROM tokens ORDER BY created_at ASC, mint ASC"
            )
            processed = 0
            for trow in token_cursor:
                ts = trow["created_at"]
                if split_ts is not None:
                    # Burn-in: a train token whose full-observation window
                    # (ts .. ts + full_sec) crosses split_ts would be scored
                    # on a truncated observation — skip it entirely so only
                    # tokens with a complete in-sample decision horizon rank.
                    if subset == "train" and ts + full_sec >= split_ts:
                        continue
                    if subset == "holdout" and ts < split_ts:
                        continue
                token = Token(
                    mint=trow["mint"],
                    name=trow["name"] or "",
                    symbol=trow["symbol"] or "",
                    creator=trow["creator"] or "",
                    created_at=trow["created_at"],
                    uri=trow["uri"] or "",
                    launchpad="pumpfun",
                )
                trade_rows = conn.execute(
                    "SELECT wallet, tx_type, sol_amount, token_amount, "
                    "v_sol_in_bonding_curve, market_cap_sol, timestamp, is_creator "
                    "FROM trades WHERE mint = ? ORDER BY timestamp ASC",
                    (token.mint,),
                ).fetchall()
                processed += 1
                if not trade_rows:
                    continue

                all_trades = [
                    Trade(
                        mint=token.mint,
                        wallet=r["wallet"],
                        tx_type=r["tx_type"],
                        sol_amount=r["sol_amount"] or 0.0,
                        token_amount=(
                            r["token_amount"] if "token_amount" in r.keys() else 0.0
                        )
                        or 0.0,
                        new_token_balance=0.0,
                        bonding_curve_key="",
                        v_sol_in_bonding_curve=r["v_sol_in_bonding_curve"] or 0.0,
                        v_tokens_in_bonding_curve=0.0,
                        market_cap_sol=r["market_cap_sol"] or 0.0,
                        timestamp=r["timestamp"],
                        is_creator=bool(r["is_creator"]),
                    )
                    for r in trade_rows
                ]
                # Walk-forward: in train subset, drop any trade >= split_ts
                # so exit simulation cannot peek into holdout.
                if subset == "train" and split_ts is not None:
                    all_trades = [t for t in all_trades if t.timestamp < split_ts]
                    if not all_trades:
                        continue
                full_end = token.created_at + full_sec
                full_trades = [t for t in all_trades if t.timestamp <= full_end]
                monitor_trades = [t for t in all_trades if t.timestamp > full_end]

                creator_snapshot = self._db.get_creator_stats_as_of_sync(
                    token.creator,
                    token.mint,
                    source_db_path=snap,
                )
                creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                    token.creator,
                    token.mint,
                    source_db_path=snap,
                )

                # Entry price = last executed buy price inside the full window.
                entry_price = 0.0
                for t in reversed(full_trades):
                    if t.tx_type == "buy" and t.token_amount > 0 and t.sol_amount > 0:
                        entry_price = t.sol_amount / t.token_amount
                        break

                any_entered = False
                for ctx in combo_contexts:
                    cfg: PulseBotConfig = ctx["cfg"]
                    fast_end = token.created_at + cfg.fast_observe_seconds
                    fast_trades = [t for t in all_trades if t.timestamp <= fast_end]

                    fast_result = ctx["fast_filter"].evaluate(token, fast_trades)
                    full_result = ctx["scorer"].score(
                        token,
                        full_trades,
                        creator_snapshot=creator_snapshot,
                        creator_tokens_today=creator_tokens_today,
                    )
                    should_enter, entry_type, score, _ = decide_entry(
                        fast_result, full_result, cfg
                    )
                    if not should_enter or entry_price <= 0:
                        continue
                    # Entry happens after the FULL observe window in both live
                    # and replay — pipeline opens paper_trade only after full
                    # scoring. Use the last scoring trade's timestamp as the
                    # virtual entry clock so hold/slot math matches live.
                    entry_time = (
                        full_trades[-1].timestamp
                        if full_trades
                        else token.created_at + cfg.observe_seconds
                    )
                    ctx["candidates"].append(
                        (entry_time, token.mint, entry_type, score, entry_price)
                    )
                    any_entered = True

                if any_entered:
                    kept[token.mint] = CachedToken(
                        token=token,
                        monitor_trades=monitor_trades,
                        creator_snapshot=creator_snapshot,
                        creator_tokens_today=creator_tokens_today,
                        entry_price=entry_price,
                    )
                if processed % 500 == 0:
                    logger.info(
                        "Phase 1 progress: %d tokens scanned, %d kept",
                        processed,
                        len(kept),
                    )
        finally:
            conn.close()
        return kept

    # ── Phase 2: event-driven per-combo simulation ───────────

    def _simulate_combo_event_driven(
        self, ctx: dict[str, Any], kept_tokens: dict[str, CachedToken]
    ) -> list[dict]:
        """Replay trades in entry_time order; enforce portfolio_max_positions
        via a min-heap of active exit_times.
        """
        cfg: PulseBotConfig = ctx["cfg"]
        candidates = sorted(ctx["candidates"], key=lambda c: c[0])
        active_exit_heap: list[tuple[float, int, float]] = []
        closed: list[dict] = []
        cash = cfg.portfolio_initial_sol

        for entry_time, mint, entry_type, entry_score, entry_price in candidates:
            cash += _pop_freed_slots(active_exit_heap, entry_time)
            if len(active_exit_heap) >= cfg.portfolio_max_positions:
                continue
            if cash < cfg.buy_amount_sol:
                continue
            cached = kept_tokens.get(mint)
            if cached is None:
                continue
            # Tokens with no post-entry trades still occupy a slot in live —
            # exit as dead_token/timeout after inactivity window.
            result = self._simulate_trade_from(
                cached,
                entry_type,
                entry_score,
                entry_price,
                entry_time,
                cfg,
            )
            closed.append(result)
            cash -= cfg.buy_amount_sol
            heapq.heappush(
                active_exit_heap,
                (
                    result["exit_time"],
                    _slot_kind(result["exit_reason"]),
                    result["sol_received"],
                ),
            )
        return closed

    @staticmethod
    def _simulate_trade_from(
        cached: CachedToken,
        entry_type: str,
        entry_score: int,
        entry_price: float,
        entry_time: float,
        cfg: PulseBotConfig,
    ) -> dict:
        """Replay monitor_trades through PaperTradeRunner — same code as live."""
        from pulse_bot.core import PaperTradeRunner

        runner = PaperTradeRunner(cfg, entry_price)
        inactivity = max(cfg.exit_inactivity_seconds, 0)
        # Start the gap clock at entry_time so a monitor trade that arrives
        # later than ``entry_time + inactivity`` triggers dead_token at the
        # inactivity boundary — the same behaviour live/replay would see.
        last_trade_ts = entry_time
        exit_reason = "timeout"

        for trade in cached.monitor_trades:
            if cfg.exit_inactivity_seconds > 0:
                if trade.timestamp - last_trade_ts > cfg.exit_inactivity_seconds:
                    exit_reason = "dead_token"
                    last_trade_ts = last_trade_ts + inactivity
                    break
            last_trade_ts = trade.timestamp
            result = runner.process_trade(trade, entry_time)
            if result:
                return Optimizer._make_trade_result(
                    cached,
                    entry_type,
                    entry_score,
                    result.exit_price,
                    entry_price,
                    result.exit_reason,
                    trade.timestamp,
                    entry_time,
                    result.pnl_pct,
                    cfg,
                )

        timeout = runner.timeout_result()
        if not cached.monitor_trades:
            # No post-entry activity. If inactivity tracking is disabled we
            # still have to free the slot — use entry_time as exit (hold=0,
            # reason=timeout) so event-driven sim can advance.
            if cfg.exit_inactivity_seconds > 0:
                exit_reason = "dead_token"
                last_trade_ts = entry_time + inactivity
            else:
                exit_reason = "timeout"
                last_trade_ts = entry_time
        elif exit_reason == "timeout":
            # Stream ended with no exit signal and no gap trigger — mirror
            # live pipeline.py::_paper_trade (lines ~490-501): when inactivity
            # tracking is enabled, close as dead_token at last_event_ts +
            # inactivity so the portfolio slot isn't freed prematurely.
            if cfg.exit_inactivity_seconds > 0:
                exit_reason = "dead_token"
                last_trade_ts = last_trade_ts + inactivity
        return Optimizer._make_trade_result(
            cached,
            entry_type,
            entry_score,
            timeout.exit_price,
            entry_price,
            exit_reason,
            last_trade_ts,
            entry_time,
            timeout.pnl_pct,
            cfg,
        )

    @staticmethod
    def _make_trade_result(
        cached: CachedToken,
        entry_type: str,
        entry_score: int,
        exit_price: float,
        entry_price: float,
        exit_reason: str,
        exit_ts: float,
        entry_time: float,
        pnl_pct: float,
        cfg: PulseBotConfig,
    ) -> dict:
        """Build trade result dict."""
        buy_amt = cfg.buy_amount_sol
        pnl_sol = buy_amt * pnl_pct / 100
        hold = exit_ts - entry_time
        return {
            "mint": cached.token.mint,
            "symbol": cached.token.symbol,
            "entry_type": entry_type,
            "entry_score": entry_score,
            "exit_reason": exit_reason,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_time": entry_time,
            "exit_time": exit_ts,
            "sol_invested": buy_amt,
            "sol_received": buy_amt + pnl_sol,
            "pnl_sol": pnl_sol,
            "pnl_pct": pnl_pct,
            "hold_seconds": max(hold, 0),
            "partial_sells": 0,
        }

    # ── Helpers ──────────────────────────────────────────────

    def _make_config(self, params: dict) -> PulseBotConfig:
        cfg = copy.copy(self._base_cfg)
        for key, value in params.items():
            setattr(cfg, key, value)
        return cfg

    def _build_result(
        self, run_id: str, params: dict, cfg: PulseBotConfig, closed: list[dict]
    ) -> dict:
        """Build optimizer result dict from closed trades."""
        wins = [t for t in closed if (t.get("pnl_sol", 0) or 0) > 0]
        losses = [t for t in closed if (t.get("pnl_sol", 0) or 0) <= 0]
        total_pnl = sum(t.get("pnl_sol", 0) or 0 for t in closed)
        gross_profit = sum(t.get("pnl_sol", 0) or 0 for t in wins)
        gross_loss = abs(sum(t.get("pnl_sol", 0) or 0 for t in losses))

        exit_reasons: dict[str, int] = {}
        entry_types: dict[str, int] = {"fast": 0, "full": 0}
        for t in closed:
            r = t.get("exit_reason", "unknown")
            exit_reasons[r] = exit_reasons.get(r, 0) + 1
            et = t.get("entry_type", "full")
            entry_types[et] = entry_types.get(et, 0) + 1

        n = len(closed)
        return {
            "run_id": run_id,
            "session": self._session_id,
            "params": json.dumps(params),
            "entry_mode": cfg.entry_mode,
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / max(n, 1) * 100,
            "total_pnl_sol": total_pnl,
            "gross_profit_sol": gross_profit,
            "gross_loss_sol": gross_loss,
            "profit_factor": gross_profit / max(gross_loss, 0.0001),
            "avg_win_pct": sum(t.get("pnl_pct", 0) or 0 for t in wins)
            / max(len(wins), 1),
            "avg_loss_pct": sum(t.get("pnl_pct", 0) or 0 for t in losses)
            / max(len(losses), 1),
            "avg_win_sol": gross_profit / max(len(wins), 1),
            "avg_loss_sol": gross_loss / max(len(losses), 1),
            "max_drawdown_pct": 0.0,
            "initial_balance_sol": cfg.portfolio_initial_sol,
            "final_balance_sol": cfg.portfolio_initial_sol + total_pnl,
            "roi_pct": (
                (total_pnl / cfg.portfolio_initial_sol) * 100
                if cfg.portfolio_initial_sol > 0
                else 0
            ),
            "avg_hold_seconds": sum(t.get("hold_seconds", 0) or 0 for t in closed)
            / max(n, 1),
            "fast_buys": entry_types.get("fast", 0),
            "full_buys": entry_types.get("full", 0),
            "exit_reasons": json.dumps(exit_reasons),
            "trades_json": json.dumps(closed),
            "trades": closed,
            "created_at": time.time(),
        }

    def _evaluate_holdout(
        self, top_results: list[dict]
    ) -> list[tuple[dict, dict]] | None:
        """Re-simulate top-K combos on the holdout slice and print a
        train-vs-holdout comparison table.

        Uses the ``_worker_run_combo`` in-process by priming
        ``_WORKER_DATA`` with the holdout dataset — guarantees byte-for-byte
        identical Phase-1/Phase-2 code path as the training runs. Returns
        the (IS, OOS) pairs so callers (guardrails) can inspect overfit.
        """
        if not top_results:
            return None
        # Drop combos whose IS sample is too thin to warrant OOS eval.
        candidates = [
            r for r in top_results if r["total_trades"] >= MIN_TRADES_FOR_RANK
        ]
        suppressed = len(top_results) - len(candidates)
        if suppressed:
            logger.info(
                "Walk-forward: %d IS combos with <%d trades excluded from holdout.",
                suppressed,
                MIN_TRADES_FOR_RANK,
            )
        if not candidates:
            logger.warning(
                "No IS combo has >=%d trades — skipping holdout eval.",
                MIN_TRADES_FOR_RANK,
            )
            return None
        logger.info("Walk-forward: preloading holdout dataset...")
        holdout_records = self._preload_dataset(subset="holdout")
        logger.info(
            "Holdout tokens loaded: %d (train split_ts=%.0f)",
            len(holdout_records),
            self._split_ts or 0.0,
        )
        if not holdout_records:
            logger.warning("Holdout set is empty — skipping out-of-sample eval.")
            return None
        _worker_init(holdout_records, self._base_cfg)
        comparison: list[tuple[dict, dict]] = []
        for summary in candidates:
            raw = summary.get("params")
            params = json.loads(raw) if isinstance(raw, str) else dict(raw)
            run_id = f"{summary['run_id']}_holdout"
            _, _, closed = _worker_run_combo((run_id, params))
            cfg = self._make_config(params)
            holdout_data = self._build_result(run_id, params, cfg, closed)
            comparison.append((summary, holdout_data))
        self._print_holdout_comparison(comparison)
        return comparison

    @staticmethod
    def _print_holdout_comparison(rows: list[tuple[dict, dict]]) -> None:
        """Print a side-by-side table: in-sample vs out-of-sample."""
        print("\n" + "=" * 110)
        print("  WALK-FORWARD HOLDOUT — in-sample (train) vs out-of-sample (holdout)")
        print("=" * 110)
        print(
            f"  {'#':>3s}  "
            f"{'IS_Tr':>5s} {'IS_WR%':>6s} {'IS_PnL':>10s} {'IS_PF':>6s}   "
            f"{'OOS_Tr':>6s} {'OOS_WR%':>7s} {'OOS_PnL':>10s} {'OOS_PF':>6s}   "
            f"Params"
        )
        print("-" * 110)
        for i, (is_row, oos_row) in enumerate(rows):
            raw = is_row.get("params")
            params = json.loads(raw) if isinstance(raw, str) else dict(raw)
            params_str = " ".join(f"{k}={v}" for k, v in params.items())
            print(
                f"  {i + 1:3d}  "
                f"{is_row['total_trades']:5d} {is_row['win_rate']:6.1f} "
                f"{is_row['total_pnl_sol']:+10.6f} {is_row['profit_factor']:6.2f}   "
                f"{oos_row['total_trades']:6d} {oos_row['win_rate']:7.1f} "
                f"{oos_row['total_pnl_sol']:+10.6f} {oos_row['profit_factor']:6.2f}   "
                f"{params_str}"
            )
        # Aggregate overfitting summary.
        if rows:
            is_pnls = [r[0]["total_pnl_sol"] or 0.0 for r in rows]
            oos_pnls = [r[1]["total_pnl_sol"] or 0.0 for r in rows]
            median_is = sorted(is_pnls)[len(is_pnls) // 2]
            median_oos = sorted(oos_pnls)[len(oos_pnls) // 2]
            oos_positive = sum(1 for p in oos_pnls if p > 0)
            print("-" * 110)
            print(
                f"  Median: IS={median_is:+.6f} SOL  OOS={median_oos:+.6f} SOL  "
                f"Delta={median_oos - median_is:+.6f}  "
                f"OOS profitable={oos_positive}/{len(rows)}"
            )
        print("=" * 110 + "\n")

    @staticmethod
    def _print_top_results(results: list[dict]) -> None:
        """Print top 10 results. Filters combos with fewer than
        ``MIN_TRADES_FOR_RANK`` trades so 0-trade "pnl=0" entries do not beat
        every executed-but-losing combo in the sort.
        """
        ranked = [r for r in results if r["total_trades"] >= MIN_TRADES_FOR_RANK]
        suppressed = len(results) - len(ranked)
        print("\n" + "=" * 100)
        print(
            f"  OPTIMIZER TOP 10 RESULTS (by total P&L SOL; "
            f"{suppressed} combos with <{MIN_TRADES_FOR_RANK} trades suppressed)"
        )
        print("=" * 100)
        print(
            f"  {'#':>3s}  {'Trades':>6s}  {'WR%':>5s}  {'PnL SOL':>10s}  "
            f"{'PF':>5s}  {'ROI%':>7s}  Params"
        )
        print("-" * 100)
        for i, r in enumerate(ranked[:10]):
            params = json.loads(r["params"])
            params_str = " ".join(f"{k}={v}" for k, v in params.items())
            print(
                f"  {i + 1:3d}  {r['total_trades']:6d}  {r['win_rate']:5.1f}  "
                f"{r['total_pnl_sol']:+10.6f}  {r['profit_factor']:5.2f}  "
                f"{r['roi_pct']:+7.1f}  {params_str}"
            )
        print("=" * 100 + "\n")

    # ──────────────────────────────────────────────────────────────────────
    # Diagnostic reports (added April 2026). The optimizer is a dumb ranker;
    # without these, a human must eyeball logs to notice that (a) an entire
    # signal universe is noise, (b) one axis value is consistently bad, or
    # (c) top-N configs are overfit on a 7-trade sample. These three methods
    # surface those issues as printed reports with hard warnings.
    # ──────────────────────────────────────────────────────────────────────

    def _print_signal_universe_diagnostic(self) -> None:
        """Pre-flight check: using ``token_scores`` from the main DB, break
        down decisions into the 4 fast×full cross-cells and report forward
        return (``pnl_100th_pct``) per cell. Warn when one cell is noise.

        Runs once at start of ``run()``. Non-fatal — purely advisory.
        """
        try:
            conn = sqlite3.connect(self._base_cfg.db_path)
            rows = conn.execute(
                """SELECT fast_decision, decision,
                          COUNT(*) n,
                          AVG(pnl_100th_pct) avg_pnl,
                          SUM(CASE WHEN pnl_100th_pct > 0 THEN 1 ELSE 0 END) wins
                   FROM token_scores
                   WHERE source='live'
                     AND fast_decision IS NOT NULL AND decision IS NOT NULL
                   GROUP BY fast_decision, decision"""
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("signal diagnostic skipped: %s", exc)
            return

        if not rows:
            return
        print("\n" + "=" * 90)
        print(
            "  PRE-FLIGHT: SIGNAL UNIVERSE DIAGNOSTIC  (forward pnl@100 by decision cell)"
        )
        print("=" * 90)
        print(
            f"  {'fast':<10s} {'full':<10s} {'n':>6s}  {'fwd_WR%':>8s}  {'avg_pnl@100':>12s}"
        )
        print("-" * 90)
        warnings: list[str] = []
        for fd, dec, n, avg_pnl, wins in rows:
            wr = (wins or 0) * 100.0 / n if n else 0.0
            print(
                f"  {fd:<10s} {dec:<10s} {n:>6d}  {wr:>7.1f}%  {(avg_pnl or 0):>+10.2f}%"
            )
            if fd == "FAST_BUY" and dec == "SKIP" and n >= 500 and wr < 5.0:
                warnings.append(
                    f"{n} FAST_BUY/SKIP tokens have only {wr:.1f}% forward WR — "
                    "fast mode alone is noise. Prefer entry_mode=full."
                )
        for w in warnings:
            print(f"  ⚠  WARNING: {w}")
        print("=" * 90 + "\n")

    def _print_marginal_analysis(self) -> None:
        """Marginal per-axis report with codex-requested rigour:

        - **Per-trade PnL** (``total_pnl / total_trades``) instead of
          per-combo PnL. Kills the confound where an axis value that just
          happens to trade less (e.g. ``entry_mode=full``) looks "best"
          only because negative-EV damage scales with trade count.
        - **Bootstrap 95% CI** on the mean per-trade PnL per axis value
          (1000 resamples). Values whose CI overlaps the pooled mean are
          flagged as *not distinguishable*.
        - **Stratification by entry_mode**: when one entry_mode dominates
          trade-count distribution, grouped axes inherit that imbalance.
          We also print within-mode marginals so the reader can spot it.
        - **2D interaction heatmaps** for known-coupled axis pairs
          (``entry_mode × fast_score_threshold`` etc) — so structural
          correlations between grid axes surface directly.
        - Only combos with ``total_trades >= MIN_TRADES_FOR_RANK`` are
          included; low-sample combos add noise, not information.
        """
        import random as _rnd
        from collections import defaultdict

        try:
            conn = sqlite3.connect(self._base_cfg.optimizer_db_path)
            rows = conn.execute(
                """SELECT params, total_pnl_sol, total_trades
                   FROM optimization_runs WHERE optimizer_session = ?""",
                (self._session_id,),
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("marginal analysis skipped: %s", exc)
            return
        if not rows:
            return

        # Codex review #3: dropping MIN_TRADES_FOR_RANK filter here. The
        # filter systematically drops tight-gate combos (they produce fewer
        # trades → more likely below cutoff) and biases "disabled=best"
        # marginals. Instead we use **trade-weighted per-trade PnL**:
        # per-axis aggregate = sum(pnl over combos) / sum(trades over combos).
        # This naturally weights by information content without bias.
        combos: list[tuple[dict, float, int]] = []
        for params_json, pnl, trades in rows:
            t = trades or 0
            if t < 1:
                continue  # zero-trade combos add no information
            try:
                params = json.loads(params_json)
            except Exception:  # nosec B112 — skip malformed row, continue scan
                continue
            per_trade = (pnl or 0.0) / t
            combos.append((params, per_trade, t))

        total_combos = len(combos)
        if total_combos < 10:
            print(
                "\n  MARGINAL ANALYSIS — skipped: only "
                f"{total_combos} combos with >= 1 trade.\n"
            )
            return

        total_pnl = sum(c[1] * c[2] for c in combos)
        total_trades = sum(c[2] for c in combos)
        pooled_mean = total_pnl / max(total_trades, 1)

        def bootstrap_weighted_ci(
            pairs: list[tuple[float, int]],
            n_boot: int = 1000,
            percentile: tuple[float, float] = (0.025, 0.975),
        ) -> tuple[float, float, float]:
            """Trade-weighted mean + percentile bootstrap CI.

            ``pairs`` = list of (per_trade_pnl, trade_count). Weighted mean
            = sum(pnl_per_trade * trades) / sum(trades). Resample combos
            with replacement; compute weighted mean per resample.
            """
            if not pairs:
                return 0.0, 0.0, 0.0
            num = sum(p * t for p, t in pairs)
            den = sum(t for _, t in pairs)
            mean = num / max(den, 1)
            boots: list[float] = []
            n = len(pairs)
            for _ in range(n_boot):
                sample = [pairs[_rnd.randrange(n)] for _ in range(n)]  # nosec B311 — bootstrap, not crypto
                bn = sum(p * t for p, t in sample)
                bd = sum(t for _, t in sample) or 1
                boots.append(bn / bd)
            boots.sort()
            lo_p, hi_p = percentile
            lo = boots[int(lo_p * n_boot)]
            hi = boots[int(min(hi_p * n_boot, n_boot - 1))]
            return mean, lo, hi

        print("\n" + "=" * 108)
        print(
            "  MARGINAL ANALYSIS — trade-weighted per-trade PnL "
            f"({total_combos} combos, {total_trades} trades total)"
        )
        print(
            f"  Pooled per-trade PnL = {pooled_mean:+.5f} SOL. "
            "CI uses BH correction for axes with >3 values (99% CI there)."
        )
        print("=" * 108)

        # Axis marginals: store (per_trade, trade_count) per value
        by_axis: dict[str, dict[Any, list[tuple[float, int]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for params, per_trade, tr in combos:
            for axis, val in params.items():
                by_axis[axis][val].append((per_trade, tr))

        for axis in sorted(by_axis.keys()):
            buckets = by_axis[axis]
            if len(buckets) < 2:
                continue
            # Codex review #4: BH correction via widened CI when bin count
            # exceeds 3. With k values we'd do C(k,2) pairwise tests; simplest
            # correction is to use 99% CI (α=0.01) instead of 95%. Roughly
            # keeps family-wise α ≤ 5% for k up to 10.
            ci_pctl = (0.005, 0.995) if len(buckets) > 3 else (0.025, 0.975)
            ci_label = "99% CI" if len(buckets) > 3 else "95% CI"
            print(f"\n  Axis: {axis}  (tw=trade-weighted, cw=combo-weighted)")
            rows_summary = []
            for val, pairs in buckets.items():
                m, lo, hi = bootstrap_weighted_ci(pairs, percentile=ci_pctl)
                tot_tr = sum(t for _, t in pairs)
                # Codex v7 audit: also compute combo-weighted (equal per combo)
                # mean — if trade-weighted and combo-weighted disagree on
                # ranking, the "best" may be a volume-concentration artifact.
                combo_mean = sum(p for p, _ in pairs) / max(len(pairs), 1)
                rows_summary.append((val, len(pairs), tot_tr, m, lo, hi, combo_mean))
            rows_summary.sort(key=lambda r: -r[3])
            # Independent sort by combo-weighted mean for disagreement check.
            combo_sorted = sorted(rows_summary, key=lambda r: -r[6])
            tw_best_val = rows_summary[0][0]
            cw_best_val = combo_sorted[0][0]
            disagree = tw_best_val != cw_best_val
            print(
                f"    {'value':<22s} {'n_combos':>8s} {'trades':>8s}  "
                f"{'tw_PnL':>10s}  {'cw_PnL':>10s}  {ci_label:>20s}  verdict"
            )
            best_lo = rows_summary[0][4]
            for val, n, tot_tr, m, lo, hi, cw in rows_summary:
                tag = ""
                if hi < best_lo:
                    tag = "  ← worse (CI non-overlap)"
                elif m < pooled_mean and hi < pooled_mean:
                    tag = "  ← below pooled"
                elif val == tw_best_val:
                    tag = "  ← tw-best"
                    if val == cw_best_val:
                        tag = "  ← BEST (both weightings agree)"
                print(
                    f"    {str(val):<22s} {n:>8d} {tot_tr:>8d}  "
                    f"{m:>+9.5f}  {cw:>+9.5f}  "
                    f"[{lo:>+7.4f},{hi:>+7.4f}] {tag}"
                )
            if disagree:
                print(
                    f"    ⚠  WEIGHTING DISAGREEMENT: trade-weighted picks "
                    f"{tw_best_val!r}, combo-weighted picks {cw_best_val!r}. "
                    "Likely volume-concentration artifact."
                )

        # 2D interaction heatmaps for axes codex flagged as coupled.
        interactions = [
            ("entry_mode", "fast_observe_seconds"),
            ("entry_mode", "fast_score_threshold"),
            ("min_entry_buyer_number", "max_entry_buyer_number"),
        ]
        for a1, a2 in interactions:
            if a1 not in by_axis or a2 not in by_axis:
                continue
            # Store (per_trade, trades) so we weighted-mean the cell.
            cell: dict[tuple[Any, Any], list[tuple[float, int]]] = defaultdict(list)
            for params, per_trade, tr in combos:
                if a1 in params and a2 in params:
                    cell[(params[a1], params[a2])].append((per_trade, tr))
            if not cell:
                continue
            a1_vals = sorted(by_axis[a1].keys(), key=lambda v: str(v))
            a2_vals = sorted(by_axis[a2].keys(), key=lambda v: str(v))
            print(
                f"\n  INTERACTION: {a1} × {a2}  (trade-weighted per-trade PnL, trades in parens)"
            )
            header = f"    {a1 + '/' + a2:<20s} " + "".join(
                f"{str(v)[:10]:>14s}" for v in a2_vals
            )
            print(header)
            for v1 in a1_vals:
                row = f"    {str(v1):<20s} "
                for v2 in a2_vals:
                    pts = cell.get((v1, v2), [])
                    if not pts:
                        row += f"{'—':>14s}"
                    else:
                        # Trade-weighted mean for cell.
                        num = sum(p * t for p, t in pts)
                        den = sum(t for _, t in pts)
                        m = num / max(den, 1)
                        row += f"{m:+.4f}({den:>4d})".rjust(14)
                print(row)
        print("=" * 108 + "\n")

    @staticmethod
    def _print_overfit_guardrails(
        top_results: list[dict],
        walk_forward: list[tuple[dict, dict]] | None,
    ) -> None:
        """Hard post-sweep checks. Prints loud warnings when the sweep result
        is almost certainly noise or overfit.
        """
        problems: list[str] = []

        if top_results:
            trade_counts = [r["total_trades"] for r in top_results[:10]]
            median_tr = sorted(trade_counts)[len(trade_counts) // 2]
            if median_tr < 20:
                problems.append(
                    f"Top-10 median IS trades = {median_tr} (< 20) — sample too small; "
                    "rankings dominated by luck. Broaden grid or loosen entry filters."
                )

        if walk_forward:
            is_pnls = [r[0]["total_pnl_sol"] or 0.0 for r in walk_forward]
            oos_pnls = [r[1]["total_pnl_sol"] or 0.0 for r in walk_forward]
            oos_positive = sum(1 for p in oos_pnls if p > 0)
            if oos_positive == 0:
                problems.append(
                    f"0/{len(walk_forward)} top configs profitable out-of-sample. "
                    "No generalizable edge in this grid. STOP and diagnose signal "
                    "quality (pre-flight) before running another sweep."
                )
            overfit_count = sum(
                1
                for is_p, oos_p in zip(is_pnls, oos_pnls)
                if is_p > 0 and (is_p - oos_p) > 2 * abs(is_p)
            )
            if overfit_count >= len(walk_forward) * 0.8:
                problems.append(
                    f"{overfit_count}/{len(walk_forward)} top configs show severe "
                    "IS→OOS collapse (>2× loss). Overfit on noise."
                )

        if not problems:
            print("\n  ✓ GUARDRAILS: no critical issues detected.\n")
            return
        print("\n" + "!" * 90)
        print("  OPTIMIZER GUARDRAILS — critical issues detected")
        print("!" * 90)
        for i, p in enumerate(problems, 1):
            print(f"  ❌ {i}. {p}")
        print("!" * 90 + "\n")

    def _print_kfold_robustness(self, top_results: list[dict], k: int = 5) -> None:
        """K-fold time-series robustness check on top-N combos.

        The single-holdout walk-forward gives a binary pass/fail per combo on
        ONE time slice. A config that survived that window may have gotten
        lucky — if we'd split at a different date, a different "winner"
        would have emerged (per codex review). Here we re-simulate each
        top combo across K equal-width chronological chunks of the full
        dataset and report how many chunks it was profitable in. A combo
        that wins in ≥3/5 chunks is plausibly robust; ≤1/5 is noise.
        """
        if not top_results or self._snapshot_path is None:
            return
        try:
            conn = sqlite3.connect(self._snapshot_path)
            ts_rows = conn.execute(
                "SELECT created_at FROM tokens ORDER BY created_at ASC"
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("kfold robustness skipped: %s", exc)
            return
        if len(ts_rows) < k * 20:
            return  # Not enough tokens for k chunks of meaningful size.

        all_ts = [r[0] for r in ts_rows]
        # Burn-in: drop earliest 10% tokens — early records may have thin
        # creator-history aggregates (cold-start) and bias fold1 negative
        # for reasons unrelated to any config's edge (codex review #5).
        burn_in = max(1, len(all_ts) // 10)
        all_ts = all_ts[burn_in:]
        min_ts, max_ts = all_ts[0], all_ts[-1]
        duration = max_ts - min_ts

        # Codex review: chunk by TIME-DURATION, not by equal token count.
        # Equal-count chunks make fold5 (recent, high density) a short
        # wall-clock slice and fold1 a long early period — so "fold5 wins"
        # can reflect a regime shift, not stable edge. Equal-time chunks
        # give each fold the same calendar window length.
        fold_bounds: list[tuple[float, float]] = []
        for i in range(k):
            lo = min_ts + (duration * i) / k
            hi = min_ts + (duration * (i + 1)) / k
            fold_bounds.append((lo, hi))

        print("\n" + "=" * 110)
        print(
            f"  K-FOLD ROBUSTNESS — each top combo simulated on {k} time-duration windows"
        )
        print(
            f"  Burn-in: dropped earliest {burn_in} tokens. "
            f"Remaining window = {duration / 3600:.1f}h ({len(all_ts)} tokens)."
        )
        print("  A combo robust against time-variance should win in ≥3/5 folds.")
        print("=" * 110)

        # Load full dataset, bucket by (created_at) into time-duration folds.
        full_records = self._preload_dataset(subset="all")
        if not full_records:
            return
        fold_records: list[list[_TokenRecord]] = [[] for _ in range(k)]
        for rec in full_records:
            ts = rec.token.created_at
            if ts < min_ts:
                continue  # burn-in skip
            for i, (lo, hi) in enumerate(fold_bounds):
                # Last fold is inclusive on both ends; others half-open.
                if (i == k - 1 and lo <= ts <= hi) or (lo <= ts < hi):
                    fold_records[i].append(rec)
                    break

        # Print per-fold sizes for interpretability.
        print(
            "\n  Fold sizes: "
            + "  ".join(f"fold{i + 1}={len(fold_records[i])}" for i in range(k))
        )

        print(
            f"\n  {'#':>2s}  {'combo':<8s}  "
            + "  ".join(f"{'fold' + str(i + 1):>9s}" for i in range(k))
            + f"  {'survived':>10s}"
        )
        print("-" * 110)
        # Capture per-combo × per-fold results so we can do the per-fold
        # axis marginal analysis right after (codex v7 audit #3).
        combo_fold_pnls: list[tuple[dict, list[float]]] = []
        for idx, summary in enumerate(top_results, 1):
            raw = summary.get("params")
            params = json.loads(raw) if isinstance(raw, str) else dict(raw)
            fold_pnls: list[float] = []
            for i, recs in enumerate(fold_records):
                if not recs:
                    fold_pnls.append(0.0)
                    continue
                _worker_init(recs, self._base_cfg)
                run_id = f"{summary['run_id']}_kfold{i}"
                try:
                    _, _, closed = _worker_run_combo((run_id, params))
                    cfg = self._make_config(params)
                    fold_res = self._build_result(run_id, params, cfg, closed)
                    fold_pnls.append(fold_res.get("total_pnl_sol") or 0.0)
                except Exception as exc:
                    logger.debug("kfold fold=%d combo=%s failed: %s", i, idx, exc)
                    fold_pnls.append(0.0)
            wins = sum(1 for p in fold_pnls if p > 0)
            tag = "  ROBUST" if wins >= 3 else "  UNSTABLE" if wins == 0 else ""
            cells = "  ".join(f"{p:+8.4f}" for p in fold_pnls)
            print(
                f"  {idx:>2d}  combo{summary['run_id'][-4:]:<3s}  {cells}  {wins:>4d}/{k}{tag}"
            )
            combo_fold_pnls.append((params, fold_pnls))
        print("=" * 110 + "\n")

        # Per-fold axis marginal analysis (codex v7 audit): if an axis value
        # is best in some folds but worst in others, it's a regime-dependent
        # strategy — usable only with a regime detector. Helps decide what
        # additional features to build (e.g., holder concentration as regime
        # signal). Only runs on top-N combos (small sample) — directional, not
        # statistically rigorous.
        if len(combo_fold_pnls) >= 4:
            print("\n" + "=" * 110)
            print(
                "  PER-FOLD AXIS MARGINALS  (top-N combos only — directional, "
                "not statistically rigorous)"
            )
            print(
                "  For each axis, mean fold PnL per axis value. Different "
                "rankings across folds ⇒ regime-dependent."
            )
            print("=" * 110)
            # Collect axis values from all combos
            from collections import defaultdict as _dd

            axis_values: dict[str, set] = _dd(set)
            for params, _ in combo_fold_pnls:
                for axis, val in params.items():
                    axis_values[axis].add(val)
            # For each axis with ≥2 values in top-N, show per-fold means
            for axis in sorted(axis_values.keys()):
                vals = sorted(axis_values[axis], key=lambda v: str(v))
                if len(vals) < 2:
                    continue
                # Build per-fold mean per axis-value
                rankings: list[list[Any]] = []  # best→worst per fold
                print(f"\n  Axis: {axis}")
                header = f"    {'value':<20s} " + "".join(
                    f"{'fold' + str(i + 1):>10s}" for i in range(k)
                )
                print(header)
                rows = []
                for val in vals:
                    per_fold: list[float] = []
                    for fold_idx in range(k):
                        vals_here = [
                            fp[fold_idx]
                            for p, fp in combo_fold_pnls
                            if p.get(axis) == val
                        ]
                        per_fold.append(
                            sum(vals_here) / max(len(vals_here), 1)
                            if vals_here
                            else 0.0
                        )
                    rows.append((val, per_fold))
                # Determine best-per-fold to detect regime flip
                for fold_idx in range(k):
                    fold_best = max(rows, key=lambda r: r[1][fold_idx])[0]
                    rankings.append([fold_best])
                unique_best = set(str(r[0]) for r in rankings)
                flip_note = (
                    "  ⚠  DIFFERENT best across folds → regime-dependent"
                    if len(unique_best) > 1
                    else ""
                )
                for val, per_fold in rows:
                    cells = "  ".join(f"{p:>+8.4f}" for p in per_fold)
                    print(f"    {str(val):<20s} {cells}")
                if flip_note:
                    best_by_fold = " ".join(
                        f"fold{i + 1}={str(rankings[i][0])}" for i in range(k)
                    )
                    print(f"    best-per-fold: {best_by_fold}{flip_note}")
            print("=" * 110 + "\n")
