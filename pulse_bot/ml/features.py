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
    "creator_tokens_today",
    "fast_buy_count",
    "fast_unique_buyers",
    "fast_volume_sol",
    "fast_buy_rate",
    "fast_sell_ratio",
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
    # literally. ``market_cap_sol`` and ``sol_to_graduation`` became
    # the #1 and #2 highest-gain features in the expanded model.
    "token_price_sol",
    "gap_create_to_first_trade",
    "market_cap_sol",
    "sol_to_graduation",
]

# Derived from ``hour_utc`` (cyclical encoding — see codex v9 P2 fix).
DERIVED_FEATURES: list[str] = [
    "hour_sin",
    "hour_cos",
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
    "creator_total_prior_tokens",
    "creator_balance_sol",
    # 2026-04-23 v9 additions — raw integer counts. Avoid the divide-by-N
    # smearing of rug_rate / graduation_rate: when total_prior_tokens=0,
    # the ratio collapses to 0 which masks "no data" as "clean record".
    # Raw counts encode both presence and magnitude unambiguously.
    "creator_rug_count",
    "creator_graduated_count",
]

# Canonical feature order — this is what the model was trained against.
# DO NOT re-order without bumping FEATURE_SCHEMA_VERSION and retraining.
ENTRY_FEATURE_ORDER: list[str] = [
    *SCORER_FEATURES,
    *DERIVED_FEATURES,
    *HELIUS_FEATURES,
    *CREATOR_FEATURES,
]

# Bumped on any schema change. Prediction path refuses to load models
# whose meta.json reports a different version.
FEATURE_SCHEMA_VERSION: str = "entry_v10_20260423"


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
    entry_ml_proba: float | None = None,
) -> dict[str, float]:
    """Extract the exit-model feature dict from a live or dict-shaped state.

    ``state`` must expose ``hold_seconds``, ``current_pnl_pct``,
    ``peak_pnl_pct``, ``drawdown_from_peak``. ``pulse`` (optional) exposes
    ``buy_rate``, ``sell_rate``, ``new_wallet_rate``, ``curve_progress_pct``.
    Any missing field → 0.0 (same NaN policy v1 as entry).

    ``entry_ml_proba`` (2026-04-23 v2) is the P(profitable) that the
    entry model assigned when BUY was decided. Carried through Position
    state. When entry was uncertain (grey zone for classifier, ±3% band
    for regression) OR the bot entered via rules-only path, the value
    is ``NaN`` so XGBoost's native missing-value handling can learn a
    separate split path for "no entry signal" — distinct from a low
    proba ("entry predicted loss") or 0% regression PnL.
    """
    import math

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
    if entry_ml_proba is None:
        # Try to read it off state; preserve None for "uncertain".
        if isinstance(state, Mapping):
            v = state.get("entry_ml_proba")
        elif state is not None:
            v = getattr(state, "entry_ml_proba", None)
        else:
            v = None
        entry_ml_proba = v
    if entry_ml_proba is None:
        feats["entry_ml_proba"] = float("nan")
    else:
        try:
            fv = float(entry_ml_proba)
            feats["entry_ml_proba"] = fv if not math.isnan(fv) else float("nan")
        except (TypeError, ValueError):
            feats["entry_ml_proba"] = float("nan")
    return feats


def extract_exit_vector(
    state: Any,
    pulse: Any = None,
    entry_ml_proba: float | None = None,
) -> list[float]:
    feats = extract_exit_features(state, pulse, entry_ml_proba=entry_ml_proba)
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


def extract_entry_features(
    scoring_result: Any,
    holder_snapshot: Mapping[str, Any] | None = None,
    creator_snapshot: Mapping[str, Any] | None = None,
    *,
    hour_utc: float | int | None = None,
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
    """
    feats: dict[str, float] = {}
    for name in SCORER_FEATURES:
        feats[name] = _get(scoring_result, name)
    h = hour_utc if hour_utc is not None else _get(scoring_result, "hour_utc")
    feats["hour_sin"], feats["hour_cos"] = _cyclical_hour(h)
    for name in HELIUS_FEATURES:
        feats[name] = _get(holder_snapshot, name) if holder_snapshot else 0.0
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
    return feats


def _get_creator_feat(snapshot: Any, feat_name: str) -> float:
    """Robust lookup for creator features under mixed naming conventions.

    Returns 0.0 when ``snapshot`` is None OR when every candidate key is
    legitimately missing on the object. BUT if the snapshot has *some*
    attributes set (i.e. it is a real CreatorStats/row) and none of our
    candidate keys match, that is a naming-convention regression and we
    WARN — silent zero-fill in that case is the exact pathology that let
    the 2026-04-23 skew bug live undetected for months.
    """
    if snapshot is None:
        return 0.0
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
) -> list[float]:
    """Positional list in ENTRY_FEATURE_ORDER — shape predict_proba expects."""
    feats = extract_entry_features(
        scoring_result,
        holder_snapshot,
        creator_snapshot,
        hour_utc=hour_utc,
    )
    return [feats[k] for k in ENTRY_FEATURE_ORDER]
