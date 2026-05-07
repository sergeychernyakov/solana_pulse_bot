# pulse_bot/ml/features.py
"""Single source of truth for ML feature names, order, and extraction.

Both training (``build_dataset.py``) and live inference (future
``MLPolicy``) import from here. A divergence in column names, order, or
NaN handling between the two paths is the #1 source of silent
train/serving skew bugs — centralising them is the mitigation.

Feature schema is explicitly versioned. Bump ``FEATURE_SCHEMA_VERSION``
when the set or order changes, and retrain. Models are invalidated
cross-schema.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# ── Feature schema v1 ───────────────────────────────────────────────

# Scorer-derived features (stored directly in ``token_scores`` columns).
#
# ``fast_score`` and ``total_score`` REMOVED 2026-04-22 per codex:
# they are outputs of the rule-based scorer ML is meant to replace —
# circular dependency that's a deployment hazard (any rule tweak
# silently shifts two feature distributions). Ablation confirmed cost
# is −1.5% AUC, well inside bootstrap SE 0.039. Drop was approved.
SCORER_FEATURES: list[str] = [
    "unique_buyers",
    "unique_sellers",  # Phase A1 2026-04-24 add
    "buy_count",
    "sell_count",
    "buy_volume_sol",
    "sell_volume_sol",
    "buy_diversity",
    "max_buy_sol",
    "avg_buy_sol",
    "median_buy_sol",
    "sell_pressure",
    "top3_buyer_pct",
    "repeat_buyer_count",
    "first_buy_sol",
    "buy_velocity_trend",
    "buy_size_trend",
    # 2026-04-23 v10 cleanup: removed first_half_buy_rate,
    # second_half_buy_rate, avg_first_half_buy_sol,
    # avg_second_half_buy_sol — stable-dead in 5-seed × 2-schema
    # stability runs (baseline 55-feature + expanded 64-feature).
    # Data still computed by metrics.py and stored on token_scores;
    # only the ML feature schema drops them. Reintroducing = just put
    # names back into this list and bump FEATURE_SCHEMA_VERSION.
    "time_to_first_buy",
    "buys_per_unique",
    "curve_velocity",
    "curve_acceleration",
    # 2026-04-27 v19 cleanup: removed creator_tokens_today + fast_sell_ratio
    # — stable-dead in TWO sequential 5-seed runs (Apr 25 + Apr 26).
    # Data still computed by metrics.py and stored on token_scores;
    # only the ML feature schema drops them.
    "fast_buy_count",
    "fast_unique_buyers",
    "fast_volume_sol",
    "fast_buy_rate",
    # 2026-04-23 v10 cleanup: removed has_uri (zero variance on
    # pump.fun — 99% of mints carry metadata).
    "tokens_last_5min",
    "concurrent_observations",
    # H7 name features removed 2026-04-23 — user rejected, not signal-
    # bearing on this dataset. Replaced with temporal-split features
    # from the live pipeline that were already captured but unused:
    #
    # * pnl_at_fast_entry_pct: direct trend indicator (MC at T+5s
    #   fast-entry vs T+90s full-obs end).
    # * fast_trade_count / full_trade_count: splits total activity into
    #   early-burst vs full-window, showing temporal profile shape that
    #   raw buy_count hides.
    "pnl_at_fast_entry_pct",
    "fast_trade_count",
    "full_trade_count",
    # 2026-04-23 v10 cleanup: removed time_gap_median_first20,
    # buy_volume_first10s, unique_buyers_first30s, unique_buyers_last30s,
    # curve_progress_at_t30/t60/t90 — stable-dead in stability runs
    # (likely colinear with full-window analogues). Data still available
    # on token_scores for future re-evaluation at higher N.
    # v9 additions — all already populated in ScoringResult and written
    # to token_scores. No wiring-bug risk: direct attr lookup through
    # ``_get(scoring_result, name)`` matches dataclass field names
    # literally.
    # market_cap_sol, sol_to_graduation, token_price_sol REMOVED 2026-04-25:
    # heavy momentum bias. token_price_sol = market_cap_sol / total_supply on
    # pump.fun (constant 1e9 tokens) — perfectly correlated after market_cap_sol
    # removal it jumped to #2 gain, same leakage. All three in KNOWN_LEAK_FEATURES.
    "gap_create_to_first_trade",
    # Phase A1 2026-04-24: SOL market regime. 56% coverage on the
    # current dataset (captured since 2026-04-22). Missing values → NaN
    # so XGBoost learns a "no regime data" split path separately from
    # "SOL was at $X".
    "sol_price_usd",
    # 2026-04-29 — creator_self_buy_position WAS added here (v22) but
    # rolled back 2026-04-30: bumping schema before retraining the model
    # caused a feature-list mismatch on bot restart and forced rules-only
    # fallback. Field still computed in MetricsCalculator + ScoringResult,
    # just not in the ML schema. Re-add on the NEXT retrain after fresh
    # EXTENDED_OBSERVE labels accumulate.
    # TODO(Phase A2): market-context features (pumpfun_tokens_per_min,
    # graduated_last_hour, creator_active_tokens_now,
    # days_since_creator_last_token). Require scorer.py changes to
    # compute at live scoring time — otherwise train/serve skew
    # (trained on aggregated SQL values, live sees 0/NaN). Park until
    # scorer.py has market-snapshot plumbing.
]

# Derived features — computed at extraction time from other fields.
# Train/serve parity is automatic because the derivation lives in
# ``extract_entry_features``; both dataset build and live inference
# call the same function.
DERIVED_FEATURES: list[str] = [
    "hour_sin",
    "hour_cos",
    # Phase B (derived, 2026-04-24): concentration slices and volume
    # ratios. All from existing SCORER + HELIUS fields — safe to add
    # without scorer.py changes.
    "top5_minus_top1_120",  # Mid-tier (holders #2-5) concentration
    "top10_minus_top5_120",  # Outer tier (holders #6-10) — HIGHEST gain
    "buy_vol_to_sell_vol_ratio",  # Imbalance on SOL volume
    "buy_count_to_sell_count_ratio",  # Imbalance on trade count
    "hc_growth_ratio",  # hc_120 / (hc_30 + 1) — growth factor
    # Phase C (derived, 2026-04-24): ratios.
    "buy_size_growth",  # max_buy_sol / (avg_buy_sol + 0.001)
    # `repeat_buyer_fraction` DROPPED: UNSTABLE nz=2/5 on first run.
    # `log_market_cap` REMOVED 2026-04-25: derivative of market_cap_sol
    # (KNOWN_LEAK). Became #1 gain and crowded out behavioural signal.
    "fast_to_full_volume_ratio",  # fast_volume_sol / (buy_volume_sol + 0.01)
    "fast_buy_rate_to_full",  # fast_buy_rate / (buy_count / 90 + 0.001)
]

# Helius holder snapshot features. Captured post-discovery at T+30 and
# T+120, joined at training time by mint. Missing snapshot = NaN in
# training, filled to 0.0 in live (see NaN policy below).
#
# ``helius_snapshot_complete`` added 2026-04-22 per codex: distinguishes
# "data present, concentration was 0" from "fetch failed, no data".
# Value 1 when the snapshot row exists (top1_30 is not NaN pre-fill);
# 0 otherwise. Allows trees to split on missingness explicitly instead
# of inferring from the hc_30=0 co-occurrence.
HELIUS_FEATURES: list[str] = [
    "top1_30",
    "top5_30",
    "hc_30",
    "top1_delta",
    "top5_delta",
    # 2026-04-23 v10 cleanup: removed helius_snapshot_complete — stable
    # dead in both stability runs. Kept computed in build_dataset as a
    # data-quality signal for humans, just not fed to the ML model.
    # v9 additions — raw T+120 columns. They were merged in build_dataset
    # only for delta computation; exposing them raw lets trees split
    # directly on "did concentration stay high at T+120" without the
    # sign-dependence of a delta.
    "top1_120",
    "top5_120",
    "hc_120",
    # Phase A1 2026-04-24: broader concentration. top10 is already
    # captured by the Helius snapshotter (100% coverage on
    # token_holders_snapshots). Trees can split on "top10 vs top1"
    # jointly to detect "broad retail" vs "narrow whale" patterns.
    "top10_30",
    "top10_120",
    "top10_delta",
    # Holder-count velocity. Positive = healthy growth between T+30 and
    # T+120. Negative = exit. Zero = stagnant.
    "hc_velocity",
]

# Creator snapshot features — PROVISIONAL (codex 2026-04-22).
# First naïve test at N=640 showed ΔAUC = −0.011 (0.28 bootstrap SE),
# i.e. within noise. Re-add as PROVISIONAL per the
# project_provisional_vs_closed memory — tree ensemble with
# regularization handles weak features, and rejection on <2×SE
# single-holdout evidence is statistically invalid. Excluded fields:
# `graduation_rate` (3 unique values across 2000 sampled creators,
# basically zero-variance), `rug_rate` + `avg_ttl_sec` (pipeline still
# stores constant 0 — value computation is broken or no rugs in data).
#
# 2026-04-23: `creator_balance_sol` added. 5165/11580 train-eligible
# tokens have non-zero balance (45% coverage) from HeliusSnapshotSource
# live-capture. Distribution: 0.001–2872 SOL, avg 11.8. LocalSnapshot
# writes 0.0 (cannot reconstruct historical point-in-time balance);
# XGBoost handles mixed real/0 natively via split learning.
CREATOR_FEATURES: list[str] = [
    "creator_age_days",
    "creator_median_peak_mc_sol",
    "creator_inter_token_interval_sec",
    "creator_balance_sol",
    # 2026-04-24 v13 cleanup: removed `creator_rug_count` — STABLE_DEAD
    # in TWO sequential schema versions (entry_v11 and entry_v12 runs).
    # 2026-04-27 v19 cleanup: removed `creator_total_prior_tokens` and
    # `creator_graduated_count` — STABLE_DEAD in TWO sequential 5-seed
    # runs (Apr 25 v18 + Apr 26 v18 with extended_observe corpus).
    # Data still in creator_snapshots; only ML schema drops them.
]

# Phase E (2026-04-24): top-3 buyer prior-activity features. Computed
# by joining wallet_activity on the top-3 buyers by SOL volume in the
# current mint's trade window, with point-in-time filter (no future
# leak). NaN policy: NaN when a wallet has no prior history / no closed
# positions; XGBoost learns the missingness split directly.
#
# Expected lift (pre-commit): Prec@top10% +8pp (35→43). Kill criterion:
# if <+3pp after 2 stability runs, revert the phase.
WALLET_FEATURES: list[str] = [
    "top3_buyer_prior_mint_count_sum",  # activity across top-3 (summed)
    "top3_buyer_prior_total_pnl_sol",  # sum realized PnL from closed positions
    "top3_buyer_prior_avg_wr",  # avg WR across top-3 that have any closed
    "top3_buyer_max_prior_pnl_sol",  # best prior winner among top-3
    "top3_buyer_wallet_age_days_avg",  # mean days since first seen
    # v20 (2026-04-27) — codex review minimal wallet additions:
    "top10_buyer_prior_avg_wr",  # extends top-3 prior-WR to top-10 buyers
    "top10_buyer_prior_total_pnl_sol",  # sum of priors over top-10 buyers
    "n_buyers_first_5s",  # sniper proxy: count of buys with age<5s post-mint
    # v21 (2026-04-28) — wallet_classifications JOIN (4 new features):
    "n_snipers_in_top10",  # is_sniper=1 in top-10 buyers (bot-cluster signal)
    "n_smart_money_in_top10",  # is_smart_money=1 (>40% WR on graduated)
    "n_bots_in_top10",  # is_bot=1 (strict subset, n_buys_30d≥500)
    "n_in_small_cluster_top10",  # in cluster of size 3-50 (real wash group)
]

# Phase 2.5 (2026-04-25): time-aware snapshots in the MAIN entry model.
# Instead of training separate @T+30 / @T+45 / @T+90 heads (Phase 3),
# expand the main model's feature vector with sub-window snapshots so a
# single classifier can learn token "evolution" between 30 and 90 sec of
# token life. Raw values come from ``MetricsCalculator._stats_up_to`` —
# bit-identical between live ScoringResult and the build_dataset.py
# re-aggregation path (verified in test_time_aware_features.py).
#
# Parity invariant: when ``observation_seconds`` == 90s the @90 columns
# equal the existing full-window analogues bit-for-bit (e.g.
# ``unique_buyers_at_90`` == ``unique_buyers``).
#
# Note: Phase 2.5 does NOT replace Phase 3's @T+30 model — Phase 3
# delivers EARLY decisions (BUY/SKIP at T+30s, before T+90 snapshot is
# available). Phase 2.5 still waits for T+90 to score; it only enriches
# what the T+90 model knows about the trajectory leading up to T+90.
TIME_AWARE_FEATURES: list[str] = [
    "unique_buyers_at_30",
    "unique_buyers_at_60",
    "unique_buyers_at_90",
    "buy_rate_at_30",  # buys/sec across [0, 30s]
    "buy_rate_at_60",  # buys/sec across [0, 60s]
    "buy_rate_at_90",  # buys/sec across [0, 90s]
    "buy_volume_sol_at_30",
    "buy_volume_sol_at_60",
    "buy_volume_sol_at_90",
]

# Phase 2.5 derived deltas — acceleration / deceleration signals on the
# time-aware metrics. Computed in ``extract_entry_features`` from
# TIME_AWARE_FEATURES (and HELIUS_FEATURES for top1 interpolation) so
# train/serve parity is automatic.
#
# ``top1_at_60`` is LINEARLY INTERPOLATED between Helius @30 and @120
# snapshots — Helius does not capture a T+60 holder snapshot natively.
# So the value is approximate; it serves as a coarse "concentration was
# rising / falling between 30s and 120s" signal. Open question for
# Phase 3 infra: capture top1 at T+60 directly (would let us drop the
# interpolation).
TIME_AWARE_DERIVED_FEATURES: list[str] = [
    "top1_at_60",  # linear interp(top1_30, top1_120) at age=60s
    "delta_top1_30_to_60",  # top1_at_60 - top1_30
    "delta_buy_rate_60_to_90",  # buy_rate_at_90 - buy_rate_at_60
    "delta_unique_buyers_30_to_60",  # unique_buyers_at_60 - unique_buyers_at_30
]

# Canonical feature order — this is what the model was trained against.
# DO NOT re-order without bumping FEATURE_SCHEMA_VERSION and retraining.
# Phase 2.5 (2026-04-25): TIME_AWARE_* groups appended at the END so
# existing column ordering stays unchanged — minimises diff vs older
# models when bisecting feature-induced regressions.
ENTRY_FEATURE_ORDER: list[str] = [
    *SCORER_FEATURES,
    *DERIVED_FEATURES,
    *HELIUS_FEATURES,
    *CREATOR_FEATURES,
    *WALLET_FEATURES,
    *TIME_AWARE_FEATURES,
    *TIME_AWARE_DERIVED_FEATURES,
]

# Bumped on any schema change. Prediction path refuses to load models
# whose meta.json reports a different version.
FEATURE_SCHEMA_VERSION: str = "entry_v21_20260428_classifications"
# v20 (2026-04-27): codex-reviewed minimal wallet behavior additions —
# top10_buyer_prior_avg_wr, top10_buyer_prior_total_pnl_sol (extend
# Phase E top-3 to top-10 buyers; reuses leak-safe wallet_prior_stats
# point-in-time path) and n_buyers_first_5s (sniper-bot proxy: count
# of buyers in [0, 5s] post-mint, computed from trade timestamps).
# All point-in-time at cutoff_ts = mint.created_at; no future leakage.
#
# v19 (2026-04-27): Removed 4 stable_dead features (creator_tokens_today,
# fast_sell_ratio, creator_total_prior_tokens, creator_graduated_count).
# v18 (2026-04-25): Phase 2.5 time-aware features — 9 raw snapshot
# metrics (unique_buyers / buy_rate / buy_volume_sol @30/@60/@90) + 4
# derived deltas (top1_at_60 interpolated, delta_top1_30_to_60,
# delta_buy_rate_60_to_90, delta_unique_buyers_30_to_60). Retrain
# required. Total feature count grows by 13.
#
# v17 (2026-04-25): Remove token_price_sol — perfectly correlated with
# market_cap_sol / 1e9 on pump.fun (constant supply); absorbed v16's
# leakage signal at gain rank #2. Added to KNOWN_LEAK_FEATURES.


# ── Entry @T+30 schema (Phase 3 dual-snapshot) ──────────────────────
#
# Goal: a SECOND classifier that scores tokens at T+30s instead of T+90s.
# Decisions: proba >= 0.75 → BUY immediately, proba < 0.15 → SKIP, else
# wait for the main T+90 model. Halves latency for clear cases and frees
# slots for clear losers earlier.
#
# Feature space is a STRICT SUBSET of the T+90 features — only those
# physically observable by 30s of token life. Trained labels stay the
# same (existing simulate_exit-based labels — outcome is fixed regardless
# of when we score; only the FEATURE snapshot moves earlier).
#
# Excluded vs ENTRY_FEATURE_ORDER:
#   * top1_120 / top5_120 / top10_120 / hc_120 — captured at T+120 by Helius
#   * top{1,5,10}_delta, hc_velocity — derived from T+30 minus T+120
#   * top5_minus_top1_120, top10_minus_top5_120, hc_growth_ratio — derived
#     from T+120 columns
#
# Included (subset known good by T+30):
#   * All FAST_* primitives (fast_buy_count, fast_unique_buyers,
#     fast_volume_sol, fast_buy_rate, fast_sell_ratio,
#     pnl_at_fast_entry_pct) — all computed from trades up to T+30s
#   * Aggregate scorer features that do not depend on a 90s window for
#     correctness (e.g. unique_buyers, buy_count). At T+30 these are
#     simply "scorer values truncated at 30s of observation". Train and
#     serve must both use the truncated window — see build_dataset_t30.
#   * Helius @T+30 holder snapshot (top1_30/top5_30/top10_30/hc_30) plus
#     a derived hc_velocity_to_30 = hc_30 / 30
#   * Creator snapshot — known at T+0
#   * Wallet prior stats from top-3 buyers up to T+30s
#   * sol_price_usd, hour_sin, hour_cos
#
# Not included anywhere in v1: time_to_first_buy (already captured), but
# `gap_create_to_first_trade` IS included — both are anchored at T<=30s
# behaviour.

SCORER_FEATURES_T30: list[str] = [
    "unique_buyers",
    "unique_sellers",
    "buy_count",
    "sell_count",
    "buy_volume_sol",
    "sell_volume_sol",
    "buy_diversity",
    "max_buy_sol",
    "avg_buy_sol",
    "median_buy_sol",
    "sell_pressure",
    "top3_buyer_pct",
    "repeat_buyer_count",
    "first_buy_sol",
    "buy_velocity_trend",
    "buy_size_trend",
    "time_to_first_buy",
    "buys_per_unique",
    "curve_velocity",
    "curve_acceleration",
    # v19 cleanup applied to SCORER_FEATURES_T30 too (same pattern).
    "fast_buy_count",
    "fast_unique_buyers",
    "fast_volume_sol",
    "fast_buy_rate",
    "tokens_last_5min",
    "concurrent_observations",
    "pnl_at_fast_entry_pct",
    "fast_trade_count",
    "full_trade_count",
    "gap_create_to_first_trade",
    "sol_price_usd",
]

DERIVED_FEATURES_T30: list[str] = [
    "hour_sin",
    "hour_cos",
    # T+30-only derived ratios (no T+120 dependency).
    "buy_vol_to_sell_vol_ratio",
    "buy_count_to_sell_count_ratio",
    "buy_size_growth",
    "fast_to_full_volume_ratio",
    "fast_buy_rate_to_full",
    # Holder-count velocity using ONLY the T+30 snapshot. Naïve estimate:
    # holders per second since token creation. Deliberately distinct from
    # the T+120 hc_velocity (90s window) — same name family but different
    # arithmetic, so XGBoost trains on a clean signal.
    "hc_velocity_to_30",
]

HELIUS_FEATURES_T30: list[str] = [
    "top1_30",
    "top5_30",
    "top10_30",
    "hc_30",
]

# Creator + wallet feature groups are known at T+0 (creator) / computable
# from trades-up-to-T+30 (wallet) — reuse the same lists as T+90 model.

ENTRY_T30_FEATURE_ORDER: list[str] = [
    *SCORER_FEATURES_T30,
    *DERIVED_FEATURES_T30,
    *HELIUS_FEATURES_T30,
    *CREATOR_FEATURES,
    *WALLET_FEATURES,
]

FEATURE_SCHEMA_VERSION_T30: str = "entry_t30_v4_20260428_classifications"


# ── Exit classifier ────────────────────────────────────────────────

# State-during-hold features. Live extractor pulls these from
# ExitManager state + PulseSnapshot; training computes them in
# build_exit_dataset() from a 10-second window of trades after entry.
# Feature name + order MUST match build_dataset output order or the
# loaded model sees shifted columns.
EXIT_FEATURE_ORDER: list[str] = [
    "hold_seconds",
    "current_pnl_pct",
    "peak_pnl_pct",
    "drawdown_from_peak",
    "buy_rate_recent",
    "sell_rate_recent",
    "unique_buyers_recent",
    "curve_progress_pct",
    "curve_velocity_recent",
    # TODO(reintroduce cross-model signal): entry_ml_proba was feature
    # #10 in exit_v2 (2026-04-23) but removed in v3. Rationale: at
    # N_entry=661 the regression entry head had spearman 0.28 — too
    # noisy as an exit input (exit AUC collapsed 0.62 → 0.48 when it
    # was included with regression entry). Re-add it when either
    #   (a) N_entry ≥ 2000 AND regression spearman ≥ 0.45, OR
    #   (b) the signal is carried via NaN+binary classifier only (no
    #       regression path until regression stabilises).
    # When re-adding: bump schema version, restore _precompute_entry_probas
    # in build_dataset, restore the param in extract_exit_features,
    # restore cross-model hash gate in ExitMLPolicy.from_path, and add
    # test_entry_proba_train_serve_parity.
]

EXIT_FEATURE_SCHEMA_VERSION: str = "exit_v3_20260423"


def extract_exit_features(
    state: Any,
    pulse: Any = None,
) -> dict[str, float]:
    """Extract the exit-model feature dict from a live or dict-shaped state.

    ``state`` must expose ``hold_seconds``, ``current_pnl_pct``,
    ``peak_pnl_pct``, ``drawdown_from_peak``. ``pulse`` (optional) exposes
    ``buy_rate``, ``sell_rate``, ``new_wallet_rate``, ``curve_progress_pct``.
    Any missing field → 0.0.
    """
    feats: dict[str, float] = {}
    feats["hold_seconds"] = _get(state, "hold_seconds")
    feats["current_pnl_pct"] = _get(state, "current_pnl_pct")
    feats["peak_pnl_pct"] = _get(state, "peak_pnl_pct")
    feats["drawdown_from_peak"] = _get(state, "drawdown_from_peak")
    feats["buy_rate_recent"] = _get(pulse, "buy_rate")
    feats["sell_rate_recent"] = _get(pulse, "sell_rate")
    feats["unique_buyers_recent"] = _get(pulse, "unique_buyers_recent") or _get(
        pulse, "new_wallet_rate"
    )
    feats["curve_progress_pct"] = _get(pulse, "curve_progress_pct")
    feats["curve_velocity_recent"] = _get(pulse, "curve_velocity") or _get(
        pulse, "curve_velocity_recent"
    )
    return feats


def extract_exit_vector(state: Any, pulse: Any = None) -> list[float]:
    feats = extract_exit_features(state, pulse)
    return [feats[k] for k in EXIT_FEATURE_ORDER]


# ── Extraction ──────────────────────────────────────────────────────


def _cyclical_hour(hour_utc: float | int | None) -> tuple[float, float]:
    """Convert UTC hour [0..23] into (sin, cos) for order-aware encoding."""
    h = float(hour_utc or 0)
    angle = 2.0 * math.pi * h / 24.0
    return math.sin(angle), math.cos(angle)


def _get(obj: Any, key: str, default: float = 0.0) -> float:
    """Read a field from a dict, pandas row, or dataclass/ScoringResult.

    Missing / None returns ``default``. Live Scorer emits 0.0 defaults,
    training historically read None from SQLite — unifying here keeps
    train/serve paths bit-identical.
    """
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        v = obj.get(key)
    else:
        v = getattr(obj, key, None)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_top3_buyer_wallets(trades: Any) -> list[str]:
    """Phase E — return top-3 buyer wallet addresses by SOL volume.

    Shared between live (``pipeline.py``) and backtest (``build_dataset.py``)
    for bit-identical feature values. Trades with tx_type != 'buy' ignored.
    Tiebreak: wallet string ASC (deterministic). Returns <=3 addresses.

    ``trades`` can be any iterable of Trade dataclasses, dicts, or rows
    (pd.Series / sqlite3.Row) — field access is duck-typed via _get.
    """
    return compute_topN_buyer_wallets(trades, n=3)


def compute_topN_buyer_wallets(trades: Any, n: int = 10) -> list[str]:
    """Generalized top-N buyer wallet ranker. Same SOL-volume ordering and
    deterministic tiebreak as compute_top3_buyer_wallets. Used for v20+
    where we extract features over top-10 in addition to legacy top-3.
    """
    vol: dict[str, float] = {}
    for t in trades:
        if isinstance(t, Mapping):
            tx = t.get("tx_type")
            wallet = t.get("wallet")
            amount = t.get("sol_amount")
        else:
            tx = getattr(t, "tx_type", None)
            wallet = getattr(t, "wallet", None)
            amount = getattr(t, "sol_amount", None)
        if tx != "buy" or not wallet:
            continue
        try:
            vol[wallet] = vol.get(wallet, 0.0) + float(amount or 0.0)
        except (TypeError, ValueError):
            continue
    ranked = sorted(vol.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, _ in ranked[:n]]


def compute_n_buyers_first_5s(trades: Any, mint_created_at: float) -> float:
    """v20 sniper-proxy: distinct wallets that bought within 5 seconds of
    token creation. Computed point-in-time from token's own trades — no
    leakage. Returns float for NaN handling consistency in extractor;
    integer values are exact.

    Pump.fun mints aren't pre-announced, so sub-5s entry implies an
    automated WS-listener bot (per codex review thresholds).
    """
    if not mint_created_at:
        return float("nan")
    seen: set[str] = set()
    for t in trades:
        if isinstance(t, Mapping):
            tx = t.get("tx_type")
            wallet = t.get("wallet")
            ts = t.get("timestamp")
        else:
            tx = getattr(t, "tx_type", None)
            wallet = getattr(t, "wallet", None)
            ts = getattr(t, "timestamp", None)
        if tx != "buy" or not wallet or ts is None:
            continue
        try:
            age = float(ts) - float(mint_created_at)
        except (TypeError, ValueError):
            continue
        if 0.0 <= age < 5.0:
            seen.add(wallet)
    return float(len(seen))


def compute_wallet_classification_counts(
    top_n_wallets: list[str] | None,
    classifications: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, float]:
    """v21 (2026-04-28): per-token counts of wallet_classifications flags
    among the top-N buyers. Returns 4 floats keyed by feature name.

    ``classifications`` maps wallet → {is_sniper, is_smart_money,
    is_bot, cluster_id, cluster_size}. NaN when classification not
    available (caller passes None for wallets without history).
    """
    nan = float("nan")
    out = {
        "n_snipers_in_top10": nan,
        "n_smart_money_in_top10": nan,
        "n_bots_in_top10": nan,
        "n_in_small_cluster_top10": nan,
    }
    if not top_n_wallets or not classifications:
        return out
    n_sniper = 0
    n_smart = 0
    n_bot = 0
    n_cluster = 0
    matched = 0
    for w in top_n_wallets:
        cls = classifications.get(w)
        if cls is None:
            continue
        matched += 1
        if cls.get("is_sniper"):
            n_sniper += 1
        if cls.get("is_smart_money"):
            n_smart += 1
        if cls.get("is_bot"):
            n_bot += 1
        cluster_size = cls.get("cluster_size")
        if cluster_size is not None and 3 <= int(cluster_size) <= 50:
            n_cluster += 1
    if matched > 0:
        out["n_snipers_in_top10"] = float(n_sniper)
        out["n_smart_money_in_top10"] = float(n_smart)
        out["n_bots_in_top10"] = float(n_bot)
        out["n_in_small_cluster_top10"] = float(n_cluster)
    return out


def _extract_wallet_prior_features(
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None,
    top3_buyer_wallets: list[str] | None,
    cutoff_ts: float | None,
    wallet_classifications: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Phase E — aggregate top-3 buyer prior stats into 5 WALLET_FEATURES.

    NaN-first: when no wallet has prior activity, all features return NaN
    so XGBoost can split on missingness explicitly (same pattern as
    creator features + SOL price — "no signal" vs "zero signal").
    """
    nan = float("nan")
    out: dict[str, float] = {k: nan for k in WALLET_FEATURES}
    if not wallet_prior_stats or not top3_buyer_wallets:
        return out
    mint_counts: list[float] = []
    total_pnls: list[float] = []
    wrs: list[float] = []
    max_pnls: list[float] = []
    ages_days: list[float] = []
    for w in top3_buyer_wallets:
        s = wallet_prior_stats.get(w)
        if not s:
            continue
        mc = s.get("all_mint_count", 0) or 0
        if mc > 0:
            mint_counts.append(float(mc))
            total_pnls.append(float(s.get("total_pnl_sol", 0.0) or 0.0))
        wr = s.get("wr")
        if wr is not None and not (isinstance(wr, float) and math.isnan(wr)):
            wrs.append(float(wr))
        mp = s.get("max_pnl_sol")
        if mp is not None and not (isinstance(mp, float) and math.isnan(mp)):
            max_pnls.append(float(mp))
        fs = float(s.get("first_seen_ts", 0.0) or 0.0)
        if fs > 0 and cutoff_ts is not None and cutoff_ts > fs:
            ages_days.append((cutoff_ts - fs) / 86400.0)
    # Legacy top-3 aggregations (slice the leading 3 of the input list).
    top3_subset = top3_buyer_wallets[:3]
    mc3, tp3, wr3, mp3, ad3 = [], [], [], [], []
    for w in top3_subset:
        s = wallet_prior_stats.get(w)
        if not s:
            continue
        mc = s.get("all_mint_count", 0) or 0
        if mc > 0:
            mc3.append(float(mc))
            tp3.append(float(s.get("total_pnl_sol", 0.0) or 0.0))
        wr = s.get("wr")
        if wr is not None and not (isinstance(wr, float) and math.isnan(wr)):
            wr3.append(float(wr))
        mp = s.get("max_pnl_sol")
        if mp is not None and not (isinstance(mp, float) and math.isnan(mp)):
            mp3.append(float(mp))
        fs = float(s.get("first_seen_ts", 0.0) or 0.0)
        if fs > 0 and cutoff_ts is not None and cutoff_ts > fs:
            ad3.append((cutoff_ts - fs) / 86400.0)
    if mc3:
        out["top3_buyer_prior_mint_count_sum"] = float(sum(mc3))
        out["top3_buyer_prior_total_pnl_sol"] = float(sum(tp3))
    if wr3:
        out["top3_buyer_prior_avg_wr"] = float(sum(wr3) / len(wr3))
    if mp3:
        out["top3_buyer_max_prior_pnl_sol"] = float(max(mp3))
    if ad3:
        out["top3_buyer_wallet_age_days_avg"] = float(sum(ad3) / len(ad3))
    # v20 — top-10 aggregations only when caller passes >3 wallets
    # (i.e. v20 path with up to 10). Reverted 2026-04-27 19:15 after
    # the codex-suggested removal of this guard caused AUC 0.905 →
    # 0.891 and EV-search collapse. The NaN-when-≤3 semantics is
    # actually load-bearing: XGBoost uses missing-feature splits as
    # a "dead-mint" indicator. Populating top10_* for tokens with
    # 1-2 buyers introduces noise that degrades ranking signal.
    if len(top3_buyer_wallets) > 3:
        if wrs:
            out["top10_buyer_prior_avg_wr"] = float(sum(wrs) / len(wrs))
        if total_pnls:
            out["top10_buyer_prior_total_pnl_sol"] = float(sum(total_pnls))
    # v21 — wallet_classifications JOIN (sniper/smart/bot/wash counts).
    cls_counts = compute_wallet_classification_counts(
        top3_buyer_wallets, wallet_classifications
    )
    out.update(cls_counts)
    return out


def extract_entry_features(
    scoring_result: Any,
    holder_snapshot: Mapping[str, Any] | None = None,
    creator_snapshot: Mapping[str, Any] | None = None,
    *,
    hour_utc: float | int | None = None,
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
    top3_buyer_wallets: list[str] | None = None,
    cutoff_ts: float | None = None,
    n_buyers_first_5s: float | None = None,
    wallet_classifications: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Return the feature dict the entry model expects, in canonical order.

    ``scoring_result`` can be:
        * a live ``ScoringResult`` dataclass,
        * a pandas row / dict fetched from ``token_scores``,
        * any object with the expected attributes.

    ``holder_snapshot`` → keys: ``top1_30``, ``top5_30``, ``hc_30``,
    ``top1_delta``, ``top5_delta``. ``creator_snapshot`` → keys prefixed
    with ``creator_*`` in CREATOR_FEATURES. Either None → zero-fill.
    ``hour_utc`` override is for cases where scoring_result doesn't
    carry it.

    ``wallet_prior_stats`` (Phase E): dict {wallet_addr: {all_mint_count,
    closed_mint_count, wr, total_pnl_sol, max_pnl_sol, first_seen_ts}}
    — pre-queried for the top-3 buyer wallets at ``cutoff_ts``.
    ``top3_buyer_wallets``: addresses in rank order.
    ``cutoff_ts``: scored_at anchor; used to compute wallet_age_days.
    All three None → WALLET_FEATURES all NaN.
    """
    feats: dict[str, float] = {}
    for name in SCORER_FEATURES:
        feats[name] = _get(scoring_result, name)
    h = hour_utc if hour_utc is not None else _get(scoring_result, "hour_utc")
    feats["hour_sin"], feats["hour_cos"] = _cyclical_hour(h)
    for name in HELIUS_FEATURES:
        feats[name] = _get(holder_snapshot, name) if holder_snapshot else 0.0
    # Phase B derived features — pure functions of scorer + helius fields.
    # Small epsilon on denominators to avoid ZeroDivisionError; XGBoost
    # sees the inflated value near zero as a distinct split signal.
    feats["top5_minus_top1_120"] = feats["top5_120"] - feats["top1_120"]
    feats["top10_minus_top5_120"] = feats["top10_120"] - feats["top5_120"]
    feats["buy_vol_to_sell_vol_ratio"] = feats["buy_volume_sol"] / (
        feats["sell_volume_sol"] + 0.01
    )
    feats["buy_count_to_sell_count_ratio"] = feats["buy_count"] / (
        feats["sell_count"] + 1.0
    )
    feats["hc_growth_ratio"] = feats["hc_120"] / (feats["hc_30"] + 1.0)
    # Phase C derived — ratios & log-scale.
    feats["buy_size_growth"] = feats["max_buy_sol"] / (feats["avg_buy_sol"] + 0.001)
    feats["fast_to_full_volume_ratio"] = feats["fast_volume_sol"] / (
        feats["buy_volume_sol"] + 0.01
    )
    # log_market_cap removed v16-17: market_cap_sol not in SCORER_FEATURES
    # fast_buy_rate vs full-window average rate (buy_count / 90s)
    full_rate = feats["buy_count"] / 90.0
    feats["fast_buy_rate_to_full"] = feats["fast_buy_rate"] / (full_rate + 0.001)
    for name in CREATOR_FEATURES:
        # CreatorStats + creator_snapshots use INCONSISTENT naming:
        # some fields keep the ``creator_`` prefix (``creator_age_days``,
        # ``creator_balance_sol``), others drop it (``median_peak_mc_sol``,
        # ``inter_token_interval_sec``, ``snapshot_prior_tokens``). Build
        # datasets also alias columns via SQL (``total_prior_tokens AS
        # creator_total_prior_tokens``). To make live/train lookup work
        # without depending on naming luck, try three keys in order:
        # 1) the full feature name (``creator_age_days``),
        # 2) the stripped form (``age_days``),
        # 3) for ``creator_total_prior_tokens``, the CreatorStats alias
        #    ``snapshot_prior_tokens``.
        # This closes a long-standing train/serve skew bug where 2 of 4
        # creator features were silently 0 at live inference while the
        # model had trained on real column values.
        feats[name] = _get_creator_feat(creator_snapshot, name)
    # ── Skew guard ─────────────────────────────────────────────────
    # Post-2026-04-23: scream if we got passed a non-None snapshot but
    # every feature we pulled from it resolved to 0.0. That is the
    # literal fingerprint of the bug we just fixed, and we will see it
    # again if someone renames a CreatorStats attribute without updating
    # _get_creator_feat. A missing snapshot (None) is legitimate and
    # silent; a non-empty snapshot that produced only zeros is not.
    if creator_snapshot is not None and CREATOR_FEATURES:
        if all(feats[k] == 0.0 for k in CREATOR_FEATURES):
            logger.warning(
                "extract_entry_features: creator_snapshot passed (%s) "
                "but all %d CREATOR_FEATURES resolved to 0.0 — possible "
                "naming-convention regression (creator skew bug signature). "
                "Check CreatorStats field names vs _get_creator_feat "
                "candidate keys.",
                type(creator_snapshot).__name__,
                len(CREATOR_FEATURES),
            )
    # Phase E — top-3 buyer prior stats. Both paths (pipeline.py and
    # build_dataset.py) compute `top3_buyer_wallets` with the same helper
    # and query the same wallet_activity table. NaN-fill when no buyers
    # or no history so XGBoost splits on missingness.
    feats.update(
        _extract_wallet_prior_features(
            wallet_prior_stats, top3_buyer_wallets, cutoff_ts,
            wallet_classifications=wallet_classifications,
        )
    )
    # v20 sniper proxy — caller pre-computes from raw trades; NaN when
    # not provided (legacy callers, tests).
    feats["n_buyers_first_5s"] = (
        float(n_buyers_first_5s)
        if n_buyers_first_5s is not None
        else float("nan")
    )
    # Phase 2.5 (2026-04-25) — time-aware snapshots. Raw values come
    # straight off the ScoringResult / token_scores row (computed by
    # MetricsCalculator._stats_up_to in the live path; re-aggregated
    # in build_dataset.py for older rows). Default 0.0 when missing —
    # legitimate for tokens with no buys in the window.
    for name in TIME_AWARE_FEATURES:
        feats[name] = _get(scoring_result, name)
    # Derived deltas + interpolation. ``top1_at_60`` is a coarse linear
    # interp between the Helius @30 and @120 holder snapshots — Helius
    # itself does not capture an @60 snapshot. When either side is
    # zero-filled (snapshot missing → 0.0 NaN policy) the interpolated
    # value reduces to a midpoint of available data; XGBoost can split
    # on the (zero, non-zero) co-occurrence to detect missingness.
    top1_30 = feats.get("top1_30", 0.0)
    top1_120 = feats.get("top1_120", 0.0)
    # 60s lies 1/3 of the way from 30s to 120s on the (30, 120) interval.
    feats["top1_at_60"] = top1_30 + (top1_120 - top1_30) * (60.0 - 30.0) / (
        120.0 - 30.0
    )
    feats["delta_top1_30_to_60"] = feats["top1_at_60"] - top1_30
    feats["delta_buy_rate_60_to_90"] = feats["buy_rate_at_90"] - feats["buy_rate_at_60"]
    feats["delta_unique_buyers_30_to_60"] = (
        feats["unique_buyers_at_60"] - feats["unique_buyers_at_30"]
    )
    return feats


# Creator features that are **mathematically undefined** when the creator
# has < 2 prior tokens (solo creator, this token only). median(empty) = 0
# is *not* meaningful "low MC", and interval(1 token) = 0 is *not*
# meaningful "fast cadence" — both are missing-data signals XGBoost should
# split on natively via NaN. About 64% of creators in the live stream are
# solo (per 2026-05-05 audit), so leaving these as 0 silently shifts the
# distribution at serve time vs the mostly-veteran-creator training data.
_CREATOR_FEATS_DEGENERATE_FOR_SOLO: frozenset[str] = frozenset({
    "creator_median_peak_mc_sol",
    "creator_inter_token_interval_sec",
})


def _creator_priors_count(snapshot: Any) -> float | None:
    """Read ``snapshot_prior_tokens`` (or ``total_prior_tokens``) off a
    CreatorStats / row. Returns None if neither attribute exists."""
    for key in ("snapshot_prior_tokens", "total_prior_tokens"):
        if isinstance(snapshot, Mapping):
            v = snapshot.get(key)
        else:
            v = getattr(snapshot, key, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _get_creator_feat(snapshot: Any, feat_name: str) -> float:
    """Robust lookup for creator features under mixed naming conventions.

    Returns NaN when:
      * ``snapshot`` is None (no creator data at all), OR
      * the feature is degenerate-for-solo (median_peak_mc / inter_token_interval)
        AND the creator has < 2 prior tokens.

    Returns 0.0 when every candidate key is legitimately missing on a
    non-None object — that is the creator-skew bug fingerprint and we WARN.
    """
    if snapshot is None:
        return float("nan")
    if feat_name in _CREATOR_FEATS_DEGENERATE_FOR_SOLO:
        priors = _creator_priors_count(snapshot)
        if priors is not None and priors < 2:
            return float("nan")
    candidates: list[str] = [feat_name]
    if feat_name.startswith("creator_"):
        candidates.append(feat_name[len("creator_") :])
    # ``creator_total_prior_tokens`` is stored on CreatorStats as
    # ``snapshot_prior_tokens`` — try that alias too.
    if feat_name == "creator_total_prior_tokens":
        candidates.append("snapshot_prior_tokens")
    sentinel = object()
    keys_seen: list[str] = []
    for key in candidates:
        if isinstance(snapshot, Mapping):
            keys_seen.append(key)
            v = snapshot.get(key, sentinel)
        else:
            keys_seen.append(key)
            v = getattr(snapshot, key, sentinel)
        if v is sentinel or v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            logger.warning(
                "_get_creator_feat: %r on snapshot %s is non-numeric "
                "(%r) — coerced to 0.0. Check snapshot source.",
                key,
                type(snapshot).__name__,
                v,
            )
            continue
    logger.warning(
        "_get_creator_feat: none of %s resolved on %s for feature %r — "
        "defaulting to 0.0. This is the creator-skew fingerprint; "
        "check that CreatorStats / snapshot rows still expose one of "
        "these attributes.",
        keys_seen,
        type(snapshot).__name__,
        feat_name,
    )
    return 0.0


def extract_entry_vector(
    scoring_result: Any,
    holder_snapshot: Mapping[str, Any] | None = None,
    creator_snapshot: Mapping[str, Any] | None = None,
    *,
    hour_utc: float | int | None = None,
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
    top3_buyer_wallets: list[str] | None = None,
    cutoff_ts: float | None = None,
    n_buyers_first_5s: float | None = None,
    wallet_classifications: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[float]:
    """Positional list in ENTRY_FEATURE_ORDER — shape predict_proba expects."""
    feats = extract_entry_features(
        scoring_result,
        holder_snapshot,
        creator_snapshot,
        hour_utc=hour_utc,
        wallet_prior_stats=wallet_prior_stats,
        top3_buyer_wallets=top3_buyer_wallets,
        cutoff_ts=cutoff_ts,
        n_buyers_first_5s=n_buyers_first_5s,
        wallet_classifications=wallet_classifications,
    )
    return [feats[k] for k in ENTRY_FEATURE_ORDER]


# ── Entry @T+30 extractor (Phase 3) ─────────────────────────────────


def extract_entry_features_t30(
    scoring_result_partial: Any,
    holder_snapshot_t30: Mapping[str, Any] | None = None,
    creator_snapshot: Mapping[str, Any] | None = None,
    *,
    hour_utc: float | int | None = None,
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
    top3_buyer_wallets: list[str] | None = None,
    cutoff_ts: float | None = None,
    n_buyers_first_5s: float | None = None,
    wallet_classifications: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, float]:
    """Return the @T+30 feature dict in canonical ``ENTRY_T30_FEATURE_ORDER``.

    ``scoring_result_partial`` is the live ScoringResult truncated at
    T+30 (or, in training, the same scorer values pre-aggregated against
    a 30-second window). Caller is responsible for ensuring the values
    really were observed by T+30 — extract_entry_features_t30 cannot
    police that and naïvely zero-fills missing keys.

    ``holder_snapshot_t30`` exposes ``top1_30``, ``top5_30``, ``top10_30``,
    ``hc_30``. ``creator_snapshot`` reuses the same shape as the T+90
    model. ``wallet_prior_stats`` + ``top3_buyer_wallets`` + ``cutoff_ts``
    feed the wallet group; pass cutoff = scored_at (=T+30 epoch).
    """
    feats: dict[str, float] = {}
    for name in SCORER_FEATURES_T30:
        feats[name] = _get(scoring_result_partial, name)
    h = hour_utc if hour_utc is not None else _get(scoring_result_partial, "hour_utc")
    feats["hour_sin"], feats["hour_cos"] = _cyclical_hour(h)
    for name in HELIUS_FEATURES_T30:
        feats[name] = _get(holder_snapshot_t30, name) if holder_snapshot_t30 else 0.0
    # Derived ratios — same arithmetic as T+90 model where shared.
    feats["buy_vol_to_sell_vol_ratio"] = feats["buy_volume_sol"] / (
        feats["sell_volume_sol"] + 0.01
    )
    feats["buy_count_to_sell_count_ratio"] = feats["buy_count"] / (
        feats["sell_count"] + 1.0
    )
    feats["buy_size_growth"] = feats["max_buy_sol"] / (feats["avg_buy_sol"] + 0.001)
    feats["fast_to_full_volume_ratio"] = feats["fast_volume_sol"] / (
        feats["buy_volume_sol"] + 0.01
    )
    # At T+30 the "full" window is the same 30s — divide by 30 not 90.
    full_rate_30 = feats["buy_count"] / 30.0
    feats["fast_buy_rate_to_full"] = feats["fast_buy_rate"] / (full_rate_30 + 0.001)
    # Holder-count velocity from T+30 snapshot only. Defaults to 0 if the
    # snapshot is missing (consistent with HELIUS zero-fill above).
    feats["hc_velocity_to_30"] = feats["hc_30"] / 30.0
    for name in CREATOR_FEATURES:
        feats[name] = _get_creator_feat(creator_snapshot, name)
    if creator_snapshot is not None and CREATOR_FEATURES:
        if all(feats[k] == 0.0 for k in CREATOR_FEATURES):
            logger.warning(
                "extract_entry_features_t30: creator_snapshot passed (%s) "
                "but all %d CREATOR_FEATURES resolved to 0.0 — possible "
                "naming-convention regression (creator skew bug signature).",
                type(creator_snapshot).__name__,
                len(CREATOR_FEATURES),
            )
    feats.update(
        _extract_wallet_prior_features(
            wallet_prior_stats, top3_buyer_wallets, cutoff_ts,
            wallet_classifications=wallet_classifications,
        )
    )
    feats["n_buyers_first_5s"] = (
        float(n_buyers_first_5s)
        if n_buyers_first_5s is not None
        else float("nan")
    )
    return feats


def extract_entry_vector_t30(
    scoring_result_partial: Any,
    holder_snapshot_t30: Mapping[str, Any] | None = None,
    creator_snapshot: Mapping[str, Any] | None = None,
    *,
    hour_utc: float | int | None = None,
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] | None = None,
    top3_buyer_wallets: list[str] | None = None,
    cutoff_ts: float | None = None,
    n_buyers_first_5s: float | None = None,
    wallet_classifications: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[float]:
    """Positional list in ENTRY_T30_FEATURE_ORDER — shape predict_proba expects."""
    feats = extract_entry_features_t30(
        scoring_result_partial,
        holder_snapshot_t30,
        creator_snapshot,
        hour_utc=hour_utc,
        wallet_prior_stats=wallet_prior_stats,
        top3_buyer_wallets=top3_buyer_wallets,
        cutoff_ts=cutoff_ts,
        n_buyers_first_5s=n_buyers_first_5s,
        wallet_classifications=wallet_classifications,
    )
    return [feats[k] for k in ENTRY_T30_FEATURE_ORDER]
