# pulse_bot/ml/entry_timing.py
"""Phase 5 — entry-timing classifier (per-snapshot WAIT_MORE/BUY_NOW/SKIP).

Today's entry decision is binary at exactly T+90s. This module defines the
machinery for a *per-snapshot* classifier that, every 15s starting at
T+15s, returns one of three actions:

    0 = WAIT_MORE   — outcome positive but not time-sensitive yet, wait
                       another 15s for cleaner signal.
    1 = BUY_NOW     — positive outcome AND entering 15s later would be
                       materially worse → buy now or miss the move.
    2 = SKIP        — clearly bad outcome, abandon this token.

This solves the "21st buyer" problem: when signal matures at T+45s the
bot doesn't have to wait until T+90s to act.

DESIGN NOTES
------------

* **Supervised, not RL.** Per-snapshot labels are generated post-hoc by
  re-running :func:`pulse_bot.ml.simulate_exit.simulate_exit` with
  ``entry_ts=t`` for each checkpoint ``t ∈ {15,30,45,60,75,90}``. The
  exit logic is the same one the live bot uses, so labels are honest.

* **Three-class label heuristic.** ``simulate_exit`` returns a continuous
  ``pnl_pct``. We threshold:

  - ``pnl_pct < neg_threshold_pct`` → SKIP.
  - ``pnl_pct > pos_threshold_pct`` AND outcome at next snapshot t+15s is
    materially worse (drop > ``urgency_drop_pct``) → BUY_NOW.
  - ``pnl_pct > pos_threshold_pct`` AND outcome at t+15s ≥ now → WAIT_MORE.
  - ``pos_threshold_pct ≥ pnl_pct ≥ neg_threshold_pct`` (ambiguous) →
    WAIT_MORE.
  - Last snapshot (no t+15s): BUY_NOW if positive, SKIP if negative.

* **Features observable up to time t only.** This module deliberately
  does NOT reuse ``ENTRY_FEATURE_ORDER`` (that schema is anchored at
  T+90s with Helius @T+30/@T+120 snapshots and post-hoc rollups). For
  Phase 5 we use a small, self-contained set of cumulative trade-stream
  features that are well-defined at any ``t``. Once the classifier
  proves out, future iterations can splice in time-aware Helius/creator
  features.

* **Not yet integrated into the live pipeline.** This file ships the
  label builder, training, and inference. Wiring into
  ``Pipeline._observe_token`` is a separate Phase 5 deployment task.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from pulse_bot.config import PulseBotConfig, get_config
from pulse_bot.ml.simulate_exit import simulate_exit
from pulse_bot.models import Trade

logger = logging.getLogger(__name__)


# ── Schema ──────────────────────────────────────────────────────────

#: Snapshot offsets (seconds since token creation) at which the timing
#: classifier emits a decision. 15s grid keeps cadence high enough that
#: a token maturing between two checkpoints wastes at most one tick.
DEFAULT_SNAPSHOT_TIMES_SEC: tuple[float, ...] = (15.0, 30.0, 45.0, 60.0, 75.0, 90.0)

#: Class index → human label. Used by :func:`predict_entry_timing` for
#: easy logging and dashboard display.
CLASS_NAMES: tuple[str, str, str] = ("WAIT_MORE", "BUY_NOW", "SKIP")
CLASS_WAIT_MORE: int = 0
CLASS_BUY_NOW: int = 1
CLASS_SKIP: int = 2

#: Canonical feature order. Bump :data:`TIMING_SCHEMA_VERSION` on any
#: change and retrain — saved meta.json compares this string at load
#: time and refuses mismatched models.
TIMING_FEATURE_ORDER: tuple[str, ...] = (
    "snapshot_t",
    "unique_buyers",
    "buy_count",
    "sell_count",
    "buy_volume_sol",
    "sell_volume_sol",
    "buy_rate",
    "sell_pressure",
    "buy_to_sell_count_ratio",
    "mc_at_t",
    "mc_growth_pct",
    "time_since_first_buy",
    "first_buy_sol",
    "max_buy_sol",
    "avg_buy_sol",
    "creator_sold",
)

TIMING_SCHEMA_VERSION: str = "entry_timing_v2_nan_20260426"

# Label threshold defaults — exposed on EntryTimingLabelBuilder so a
# caller can sweep them without forking the module.
_POS_PNL_THRESHOLD_PCT: float = 5.0
_NEG_PNL_THRESHOLD_PCT: float = -5.0
_URGENCY_DROP_PCT: float = 10.0


# ── Feature extraction (per snapshot) ───────────────────────────────


def _trade_price(trade: Trade) -> float:
    """Best-effort per-trade price (SOL per token).

    Live ``simulate_exit`` derives ``current_price`` from
    ``v_sol_in_bonding_curve / v_tokens_in_bonding_curve``. For label
    timing we only need a *consistent* anchor — so we re-use the same
    formula. Falls back to ``market_cap_sol / 1e9`` (pump.fun's constant
    1e9 supply) when curve fields are zero/missing, and finally to
    ``market_cap_sol`` itself so price is always strictly positive when
    the token exists.
    """
    sol = float(getattr(trade, "v_sol_in_bonding_curve", 0.0) or 0.0)
    toks = float(getattr(trade, "v_tokens_in_bonding_curve", 0.0) or 0.0)
    if sol > 0.0 and toks > 0.0:
        return sol / toks
    mc = float(getattr(trade, "market_cap_sol", 0.0) or 0.0)
    if mc > 0.0:
        return mc / 1e9
    return 1e-9


def extract_snapshot_features(
    trades: Sequence[Trade],
    snapshot_t: float,
    token_created_at: float,
) -> dict[str, float]:
    """Compute the timing-classifier feature dict at observation time ``t``.

    Args:
        trades: Full trade stream for the token (any timestamp). Need
            not be sorted — we filter by absolute timestamp ≤
            ``token_created_at + snapshot_t``. Trades after that
            checkpoint are invisible to the classifier (it can only act
            on what it has seen so far).
        snapshot_t: Seconds since token creation; e.g. 30.0 means "use
            the first 30s of trade history".
        token_created_at: Absolute UNIX timestamp of token creation —
            anchor for converting ``trade.timestamp`` to relative time.

    Returns:
        Feature dict with exactly the keys in :data:`TIMING_FEATURE_ORDER`.
        Defaults are nuanced (codex 2026-04-26 finding #3):
          * COUNT/SUM features (unique_buyers, buy_count, sell_count,
            buy_volume_sol, sell_volume_sol, creator_sold) default to
            ``0.0`` — absence is observable.
          * DERIVED / observation features (mc_at_t, mc_growth_pct,
            buy_rate, sell_pressure, *_ratio, time_since_first_buy,
            first_buy_sol, max_buy_sol, avg_buy_sol) default to
            ``NaN`` — undefined when there's no underlying datum.
        Schema bumped to ``entry_timing_v2_nan_20260426`` so the trees
        learn the missing-branch split.
    """
    cutoff_ts = token_created_at + snapshot_t
    visible = [t for t in trades if t.timestamp <= cutoff_ts]

    _ZERO_DEFAULT_KEYS = {
        "unique_buyers",
        "buy_count",
        "sell_count",
        "buy_volume_sol",
        "sell_volume_sol",
        "creator_sold",
    }
    feats: dict[str, float] = {
        k: 0.0 if k in _ZERO_DEFAULT_KEYS else float("nan")
        for k in TIMING_FEATURE_ORDER
    }
    feats["snapshot_t"] = float(snapshot_t)

    if not visible:
        return feats

    buys = [t for t in visible if t.tx_type == "buy"]
    sells = [t for t in visible if t.tx_type == "sell"]

    feats["unique_buyers"] = float(len({t.wallet for t in buys}))
    feats["buy_count"] = float(len(buys))
    feats["sell_count"] = float(len(sells))
    feats["buy_volume_sol"] = float(sum(t.sol_amount for t in buys))
    feats["sell_volume_sol"] = float(sum(t.sol_amount for t in sells))

    # Rates anchored on snapshot_t — avoids divide-by-zero at t=0 and
    # gives "trades per second so far".
    if snapshot_t > 0.0:
        feats["buy_rate"] = feats["buy_count"] / snapshot_t

    # Sell pressure — symmetric ratio in [0, 1]. Codex convention from
    # the entry feature set.
    bvol = feats["buy_volume_sol"]
    svol = feats["sell_volume_sol"]
    total = bvol + svol
    feats["sell_pressure"] = (svol / total) if total > 0.0 else 0.0
    feats["buy_to_sell_count_ratio"] = feats["buy_count"] / (feats["sell_count"] + 1.0)

    # Market-cap features — last observed MC and growth vs first trade.
    last_trade = visible[-1]
    feats["mc_at_t"] = float(getattr(last_trade, "market_cap_sol", 0.0) or 0.0)
    first_buy_mc = next(
        (
            float(getattr(t, "market_cap_sol", 0.0) or 0.0)
            for t in buys
            if (getattr(t, "market_cap_sol", 0.0) or 0.0) > 0.0
        ),
        0.0,
    )
    if first_buy_mc > 0.0:
        feats["mc_growth_pct"] = (
            (feats["mc_at_t"] - first_buy_mc) / first_buy_mc * 100.0
        )

    if buys:
        first_buy = buys[0]
        feats["time_since_first_buy"] = max(0.0, cutoff_ts - first_buy.timestamp)
        feats["first_buy_sol"] = float(first_buy.sol_amount)
        feats["max_buy_sol"] = float(max(t.sol_amount for t in buys))
        feats["avg_buy_sol"] = float(feats["buy_volume_sol"] / max(len(buys), 1))

    feats["creator_sold"] = float(any(getattr(t, "is_creator", False) for t in sells))
    return feats


# ── Label builder ───────────────────────────────────────────────────


@dataclass
class TimingSnapshot:
    """One labelled checkpoint for a single token."""

    mint: str
    snapshot_t: float
    features: dict[str, float]
    label: int  # 0=WAIT_MORE, 1=BUY_NOW, 2=SKIP
    pnl_pct_at_t: float
    pnl_pct_at_next: float | None  # None if last snapshot


@dataclass
class EntryTimingLabelBuilder:
    """Generate labelled per-snapshot rows for the timing classifier.

    For each token we replay :func:`simulate_exit` once per checkpoint,
    pretending the bot entered at ``token_created_at + t`` with a price
    equal to the bonding-curve price observed at the last visible trade
    before that moment. Each replay uses the live ``PulseBotConfig`` so
    label semantics match the exit logic that will actually run.

    The 3-class label decision is described in the module docstring;
    thresholds are exposed as fields so callers can sweep without
    monkey-patching.
    """

    config: PulseBotConfig = field(default_factory=get_config)
    snapshot_times_sec: tuple[float, ...] = DEFAULT_SNAPSHOT_TIMES_SEC
    pos_pnl_threshold_pct: float = _POS_PNL_THRESHOLD_PCT
    neg_pnl_threshold_pct: float = _NEG_PNL_THRESHOLD_PCT
    urgency_drop_pct: float = _URGENCY_DROP_PCT

    def build_for_token(
        self,
        mint: str,
        trades: Sequence[Trade],
        token_created_at: float,
    ) -> list[TimingSnapshot]:
        """Return one :class:`TimingSnapshot` per checkpoint (in order).

        If ``trades`` is empty the token is treated as DOA and every
        snapshot is labelled SKIP with zero PnL. This matches the
        behaviour of :func:`simulate_exit` on an empty stream.
        """
        snaps: list[TimingSnapshot] = []
        sorted_trades = sorted(trades, key=lambda t: t.timestamp)

        # Pre-compute pnl at every snapshot time so the next-step look-
        # ahead in label assignment is trivial. Each replay is O(N)
        # over post-entry trades, so this stays O(K * N).
        pnls_at_t: list[float] = []
        for t in self.snapshot_times_sec:
            pnls_at_t.append(self._simulate_at(sorted_trades, t, token_created_at))

        for idx, t in enumerate(self.snapshot_times_sec):
            feats = extract_snapshot_features(sorted_trades, t, token_created_at)
            pnl_now = pnls_at_t[idx]
            pnl_next = pnls_at_t[idx + 1] if idx + 1 < len(pnls_at_t) else None
            label = self._assign_label(pnl_now, pnl_next)
            snaps.append(
                TimingSnapshot(
                    mint=mint,
                    snapshot_t=t,
                    features=feats,
                    label=label,
                    pnl_pct_at_t=pnl_now,
                    pnl_pct_at_next=pnl_next,
                )
            )
        return snaps

    #: Sentinel returned when the bot couldn't even enter at this
    #: snapshot (no buys observed yet ⇒ no entry price). Forces the
    #: ``_assign_label`` branch into SKIP unconditionally — a "DOA"
    #: snapshot is structurally bad regardless of pnl thresholds.
    _NO_ENTRY_SENTINEL: float = -1e9

    def _simulate_at(
        self,
        sorted_trades: Sequence[Trade],
        snapshot_t: float,
        token_created_at: float,
    ) -> float:
        """Run ``simulate_exit`` as if the bot entered at ``token_created_at + t``.

        Entry price is taken from the most recent trade strictly before
        the cutoff (consistent with extract_snapshot_features visibility
        rules). Returns ``MonitorResult.pnl_pct`` of the simulated exit.
        When no trades are visible by the snapshot (no entry price ⇒ bot
        couldn't have entered), returns :data:`_NO_ENTRY_SENTINEL` so
        ``_assign_label`` always picks SKIP — the only honest action when
        the token shows no signs of life.
        """
        cutoff_ts = token_created_at + snapshot_t
        visible = [t for t in sorted_trades if t.timestamp <= cutoff_ts]
        future = [t for t in sorted_trades if t.timestamp > cutoff_ts]
        if not visible:
            return self._NO_ENTRY_SENTINEL
        entry_price = _trade_price(visible[-1])
        if entry_price <= 0.0:
            return self._NO_ENTRY_SENTINEL
        result = simulate_exit(
            self.config,
            future,
            entry_ts=cutoff_ts,
            entry_price=entry_price,
        )
        return float(result.pnl_pct)

    def _assign_label(self, pnl_now: float, pnl_next: float | None) -> int:
        """Apply the 3-class heuristic.

        See module docstring for full semantics.
        """
        if pnl_now < self.neg_pnl_threshold_pct:
            return CLASS_SKIP
        if pnl_now <= self.pos_pnl_threshold_pct:
            # Ambiguous middle band — defer.
            return CLASS_WAIT_MORE
        # pnl_now is clearly positive.
        if pnl_next is None:
            return CLASS_BUY_NOW
        if pnl_now - pnl_next >= self.urgency_drop_pct:
            # Materially worse if we wait one more snapshot → buy now.
            return CLASS_BUY_NOW
        return CLASS_WAIT_MORE

    def build_for_corpus(
        self,
        corpus: Iterable[tuple[str, Sequence[Trade], float]],
    ) -> list[TimingSnapshot]:
        """Convenience: ``[(mint, trades, created_at), ...]`` → flat list."""
        out: list[TimingSnapshot] = []
        for mint, trades, created_at in corpus:
            out.extend(self.build_for_token(mint, trades, created_at))
        return out


# ── Train / save ────────────────────────────────────────────────────


def _snapshots_to_arrays(
    snaps: Sequence[TimingSnapshot],
) -> tuple[np.ndarray, np.ndarray]:
    """Materialize features + labels into XGBoost-ready ndarrays."""
    if not snaps:
        raise ValueError("Cannot train on empty snapshot list")
    X = np.asarray(
        [[s.features[k] for k in TIMING_FEATURE_ORDER] for s in snaps],
        dtype=np.float32,
    )
    y = np.asarray([s.label for s in snaps], dtype=np.int64)
    return X, y


def train_entry_timing(
    snapshots: Sequence[TimingSnapshot],
    model_out: Path,
    *,
    n_estimators: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    random_state: int = 42,
) -> dict:
    """Fit a 3-class XGBClassifier on per-snapshot rows and persist.

    Args:
        snapshots: Output of :meth:`EntryTimingLabelBuilder.build_for_corpus`
            (or any concatenation of ``build_for_token`` results). Must
            contain at least one row per class — XGBoost's ``multi:softprob``
            objective fails on a single-class set.
        model_out: Destination ``.ubj`` file. Sister ``.meta.json`` is
            written alongside with feature order + schema version.
        n_estimators / max_depth / learning_rate: Conservative defaults
            tuned for the small N we expect early on (≤200 tokens × 6
            snapshots = 1200 rows). Sweep at higher N.
        random_state: Seed for reproducibility.

    Returns:
        Metrics dict ``{"n_rows", "class_counts", "schema_version", ...}``.
        Model is saved to ``model_out``; sibling meta.json captures
        feature list + schema version + class names.

    Raises:
        ValueError: snapshot list empty or missing classes.
    """
    import xgboost as xgb

    X, y = _snapshots_to_arrays(snapshots)
    classes_present = set(int(c) for c in np.unique(y))
    if len(classes_present) < 2:
        raise ValueError(
            f"train_entry_timing needs ≥2 distinct classes, got {classes_present}"
        )

    counts = {int(c): int((y == c).sum()) for c in (0, 1, 2)}
    logger.info(
        "Training entry-timing on %d rows: WAIT_MORE=%d BUY_NOW=%d SKIP=%d",
        len(y),
        counts.get(0, 0),
        counts.get(1, 0),
        counts.get(2, 0),
    )

    # We use the low-level Booster API rather than XGBClassifier here:
    # the sklearn wrapper bakes ``self.n_classes_`` from observed y,
    # which crashes when the training set is missing one of the three
    # classes (common at small N — synthetic tests, very early data).
    # The Booster path simply respects ``num_class=3`` regardless and
    # outputs zero-probability columns for unseen classes. Inference
    # then loads it identically via ``xgb.Booster().load_model``.
    #
    # 2026-04-28 fix: per-row sample_weight = 1 / class_count[y_i]
    # (inverse-frequency balancing). Without this the model collapses
    # to "predict SKIP for everything" — SKIP class is 75-80% of rows
    # so plain log-loss minimization just learns the prior. Live
    # observation: p_skip=1.00 on virtually all live tokens. Balanced
    # weighting forces the model to actually distinguish the rare
    # BUY/WAIT classes.
    import numpy as _np
    n_total = len(y)
    class_counts_arr = _np.bincount(y, minlength=3).astype(float)
    # Inverse frequency, normalized so total weight = n_total (keeps
    # gradient magnitudes comparable to the unbalanced run).
    weights_per_class = _np.where(
        class_counts_arr > 0, n_total / (3.0 * class_counts_arr), 1.0
    )
    sample_w = weights_per_class[y]
    logger.info(
        "Class weights (inverse freq): WAIT=%.3f BUY=%.3f SKIP=%.3f",
        weights_per_class[0],
        weights_per_class[1],
        weights_per_class[2],
    )
    # 2026-05-01 (codex review): emit per-class metrics + AUC alongside
    # raw counts. Held-out 20% test split (random; no time order in
    # snapshots — they're per-checkpoint observations not chronological
    # mints). Metrics let downstream calibration / activation gates
    # compare retrains objectively.
    rng = _np.random.default_rng(random_state)
    test_mask = rng.random(n_total) < 0.2
    train_mask = ~test_mask
    if train_mask.sum() < 100 or test_mask.sum() < 50:
        # Safety: tiny dataset (synth tests) → train on all, no test split.
        train_mask = _np.ones(n_total, dtype=bool)
        test_mask = _np.zeros(n_total, dtype=bool)

    dtrain = xgb.DMatrix(X[train_mask], label=y[train_mask], weight=sample_w[train_mask])
    params = {
        "objective": "multi:softprob",
        "num_class": 3,
        "eval_metric": "mlogloss",
        "max_depth": max_depth,
        "learning_rate": learning_rate,
        "tree_method": "hist",
        "seed": random_state,
        "verbosity": 0,
    }
    booster = xgb.train(params, dtrain, num_boost_round=n_estimators)

    # Per-class metrics on test split.
    per_class: dict[str, dict[str, float]] = {}
    overall_auc: float | None = None
    if int(test_mask.sum()) >= 50:
        from sklearn.metrics import (
            precision_recall_fscore_support,
            roc_auc_score,
        )

        X_test = X[test_mask]
        y_test = y[test_mask]
        dtest = xgb.DMatrix(X_test)
        proba = booster.predict(dtest)  # shape (N, 3)
        y_pred = proba.argmax(axis=1)
        precision, recall, f1, support = precision_recall_fscore_support(
            y_test, y_pred, labels=[0, 1, 2], zero_division=0
        )
        for i, name in enumerate(CLASS_NAMES):
            per_class[name] = {
                "precision": float(precision[i]),
                "recall": float(recall[i]),
                "f1": float(f1[i]),
                "support": int(support[i]),
            }
        # One-vs-rest AUC if we have all 3 classes in test set.
        try:
            overall_auc = float(
                roc_auc_score(y_test, proba, multi_class="ovr", labels=[0, 1, 2])
            )
        except ValueError:
            overall_auc = None

    model_out = Path(model_out)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(model_out)

    meta = {
        "schema_version": TIMING_SCHEMA_VERSION,
        "features": list(TIMING_FEATURE_ORDER),
        "class_names": list(CLASS_NAMES),
        "n_rows": int(len(y)),
        "train_rows": int(train_mask.sum()),
        "test_rows": int(test_mask.sum()),
        "class_counts": counts,
        "class_weights": {
            CLASS_NAMES[i]: float(weights_per_class[i]) for i in range(3)
        },
        "per_class_metrics": per_class,
        "auc_ovr": overall_auc,
        "snapshot_times_sec": list(DEFAULT_SNAPSHOT_TIMES_SEC),
        "hparams": {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "learning_rate": learning_rate,
            "random_state": random_state,
        },
    }
    meta_out = model_out.with_suffix(".meta.json")
    meta_out.write_text(json.dumps(meta, indent=2, sort_keys=True))
    logger.info("Saved entry-timing model to %s (meta %s)", model_out, meta_out)
    return meta


# ── Inference ───────────────────────────────────────────────────────


@dataclass
class TimingPrediction:
    """Result of one inference call."""

    proba_wait_more: float
    proba_buy_now: float
    proba_skip: float
    decision: str  # one of CLASS_NAMES

    def as_vector(self) -> tuple[float, float, float]:
        """Return ``(p_wait_more, p_buy_now, p_skip)`` in canonical order."""
        return (self.proba_wait_more, self.proba_buy_now, self.proba_skip)


def _load_model_and_meta(model_path: Path) -> tuple[object, dict]:
    """Load Booster + meta.json with schema-version guard.

    Mismatched ``schema_version`` is a hard error: silently inferring
    against a model trained on a different feature shape is exactly the
    train/serve skew this whole package is built to prevent.
    """
    import xgboost as xgb

    meta_path = Path(model_path).with_suffix(".meta.json")
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing meta sidecar: {meta_path}")
    meta = json.loads(meta_path.read_text())
    if meta.get("schema_version") != TIMING_SCHEMA_VERSION:
        raise ValueError(
            f"entry_timing schema mismatch: model={meta.get('schema_version')!r} "
            f"runtime={TIMING_SCHEMA_VERSION!r}. Retrain before loading."
        )
    booster = xgb.Booster()
    booster.load_model(str(model_path))
    return booster, meta


def predict_entry_timing(
    features: Mapping[str, float],
    model_path: Path,
) -> TimingPrediction:
    """Score one snapshot and return per-class probabilities.

    Args:
        features: Output of :func:`extract_snapshot_features`. Missing
            keys → 0.0 (matches builder behaviour); extra keys ignored.
        model_path: Path to the ``.ubj`` saved by
            :func:`train_entry_timing`. Sibling ``.meta.json`` must
            agree on :data:`TIMING_SCHEMA_VERSION` or load fails fast.

    Returns:
        :class:`TimingPrediction` with calibrated-ish (XGBoost softmax,
        not Platt-scaled — that's a future iteration) probabilities and
        the argmax class name.
    """
    import xgboost as xgb

    booster, _ = _load_model_and_meta(Path(model_path))
    raw_vec = [float(features.get(k, float("nan"))) for k in TIMING_FEATURE_ORDER]
    # Train/serve skew guard. Only fires when there IS visible activity
    # (buy_count > 0) but features are still mostly NaN — that pattern
    # signals a real extractor bug, not a DOA token. DOA tokens (0
    # trades visible) legitimately have all-NaN features and shouldn't
    # spam logs.
    buy_count = features.get("buy_count")
    has_activity = (
        buy_count is not None and not (buy_count != buy_count) and buy_count > 0
    )
    if has_activity:
        finite = sum(1 for v in raw_vec if not (v != v))
        if len(raw_vec) > 0 and finite / len(raw_vec) < 0.5:
            logger.warning(
                "entry_timing predict: only %d/%d features non-NaN with "
                "buy_count=%s — train/serve skew? snapshot_t=%s",
                finite,
                len(raw_vec),
                buy_count,
                features.get("snapshot_t"),
            )
    row = np.asarray([raw_vec], dtype=np.float32)
    dmatrix = xgb.DMatrix(row)
    proba = booster.predict(dmatrix)[0]
    # ``multi:softprob`` outputs class probabilities in label order
    # 0..num_class-1 — i.e. WAIT_MORE, BUY_NOW, SKIP.
    p_wait, p_buy, p_skip = float(proba[0]), float(proba[1]), float(proba[2])
    decision = CLASS_NAMES[int(np.argmax(proba))]
    return TimingPrediction(
        proba_wait_more=p_wait,
        proba_buy_now=p_buy,
        proba_skip=p_skip,
        decision=decision,
    )
