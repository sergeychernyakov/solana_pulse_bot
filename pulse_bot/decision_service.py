# pulse_bot/decision_service.py
"""DecisionService — applies the chain of entry overrides.

Codex review 2026-04-28 (architecture phase B step 1): this is the part
of pipeline.py where the most production bugs concentrated this week:

* codex Issue #4 — ML / BUY_EARLY override left entry_buyer_number=0
  and entry_score=0 (looked like shadow-tracking rows).
* codex Issue #1 (related) — checkpoint SKIP_EARLY could override an
  ML BUY without surfacing why.
* "is_bot hard skip" pre-filter and ML override interaction was tangled
  with rules path.

Extracting it makes the contract explicit:

    rules said X
        ↓
    [bot-cluster pre-filter]    ← async DB lookup, can flip X to SKIP
        ↓
    [ML hybrid override]         ← pure: ml_action overrides X
        ↓
    [checkpoint override]        ← pure: T+30 / timing verdict overrides X
        ↓
    final EntryDecision

Each step takes/returns an ``EntryDecision`` so the chain is composable
and unit-testable. No model knowledge here — caller passes ml_action /
cp_verdict already computed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryDecision:
    """Immutable carrier of the rules+ML+checkpoint decision state.

    ``frozen=True`` so the override chain returns NEW instances rather
    than mutating in place. Catches accidental "I forgot to update
    entry_score after override" bugs at construction time.
    """

    should_enter: bool
    entry_type: str
    entry_score: int
    entry_buyer_num: int

    def with_(self, **kwargs: Any) -> "EntryDecision":
        """Return a copy with selected fields replaced."""
        from dataclasses import replace

        return replace(self, **kwargs)


class DecisionService:
    """Applies the override chain on top of a rules-decided entry.

    Args:
        db: ``Database`` with ``_sync_query`` — used only by the
            bot-cluster pre-filter (single ``COUNT(*)`` query).
        hard_skip_n_env: ``int(os.environ['PULSE_BOT_CLUSTER_HARD_SKIP'])``
            threshold; ``0`` disables the pre-filter.
    """

    def __init__(
        self,
        db: Any,
        hard_skip_n_env: int = 3,
        wash_cluster_skip_n: int = 0,
        wash_cluster_size_min: int = 5,
        wash_cluster_size_max: int = 50,
        reg_floor_pct: float | None = None,
        reg_ceiling_pct: float | None = None,
        config_id: str | None = None,
        p_cal_floor: float = 0.0,
    ) -> None:
        """Construct a DecisionService.

        ``reg_floor_pct`` / ``reg_ceiling_pct`` / ``config_id`` are new
        (2026-05-13) additions for the multi-config A/B framework. When
        ``reg_floor_pct`` / ``reg_ceiling_pct`` are ``None``, ``apply_ml_override``
        falls back to the legacy env-var read so single-config bots keep
        working unchanged. ``config_id`` (when set) is included in log
        messages so multi-config logs are disambiguated.
        """
        self._db = db
        self._hard_skip_n = int(hard_skip_n_env)
        # 2026-05-01 (codex review): wash-cluster hard gate.
        # Skip when ≥ ``wash_cluster_skip_n`` of top buyers in the first
        # 30s share the same ``cluster_id`` AND that cluster has size in
        # ``[wash_cluster_size_min, wash_cluster_size_max]`` (the wash-
        # suspect range — too small = noise, too large = legit communities).
        # 0 = disabled (default; ship gated behind env after sweep).
        self._wash_skip_n = int(wash_cluster_skip_n)
        self._wash_size_min = int(wash_cluster_size_min)
        self._wash_size_max = int(wash_cluster_size_max)
        # Multi-config framework: floor/ceiling per-config; env fallback
        # preserves the pre-2026-05-13 single-config behaviour.
        self._reg_floor_pct = reg_floor_pct
        self._reg_ceiling_pct = reg_ceiling_pct
        self._config_id = config_id
        # 2026-05-14: per-config floor on the classifier's calibrated
        # probability. Replaces the reg-floor A/B knob (reg head proven
        # degenerate). 0.0 = no extra floor.
        self._p_cal_floor = float(p_cal_floor)
        # Counters kept for /metrics + dashboard parity.
        self.ml_overrides_buy = 0
        self.ml_overrides_skip = 0
        self.bot_cluster_skips = 0
        self.wash_cluster_skips = 0
        self.creator_blacklist_skips = 0
        # 2026-04-28 (architecture phase H): mirror counters into
        # the global observability registry so /metrics endpoint
        # exposes them without further wiring.
        try:
            from pulse_bot.observability import metrics as _obs

            self._obs = _obs
        except Exception:  # observability optional
            self._obs = None

    @classmethod
    def from_entry_config(cls, db: Any, cfg: Any, obs: Any = None) -> "DecisionService":
        """Construct a DecisionService bound to an ``EntryConfig``.

        Lets ``Pipeline`` build one DecisionService instance per config
        without having to pull individual fields out by hand. ``cfg`` is
        an :class:`pulse_bot.entry_configs.EntryConfig`.
        """
        inst = cls(
            db=db,
            hard_skip_n_env=cfg.bot_cluster_hard_skip_n,
            wash_cluster_skip_n=cfg.wash_cluster_skip_n,
            wash_cluster_size_min=cfg.wash_cluster_size_min,
            wash_cluster_size_max=cfg.wash_cluster_size_max,
            reg_floor_pct=cfg.reg_floor_pct,
            reg_ceiling_pct=cfg.reg_ceiling_pct,
            config_id=cfg.config_id,
            p_cal_floor=getattr(cfg, "p_cal_floor", 0.0),
        )
        if obs is not None:
            inst._obs = obs
        return inst

    # ───────────────────────── bot-cluster pre-filter ──────────────

    async def filter_bot_cluster(
        self,
        token: Any,
        all_trades: list[Any],
        decision: EntryDecision,
        mint_short: str,
    ) -> EntryDecision:
        """If 3+ wallets in the first 30s of trades are flagged
        ``is_bot=1`` in ``wallet_classifications``, force SKIP. Runs
        BEFORE ML override — ML cannot second-guess this.

        Threshold tunable via PULSE_BOT_CLUSTER_HARD_SKIP. Set to 0
        to disable.

        Single COUNT(*) query, async via ``asyncio.to_thread`` so we
        don't block the event loop.
        """
        if self._hard_skip_n <= 0:
            return decision
        if not decision.should_enter:
            return decision
        if not (token.created_at or 0) > 0:
            return decision

        early_buyers: set[str] = set()
        for tr in all_trades:
            if tr.tx_type != "buy":
                continue
            age = float(tr.timestamp) - float(token.created_at)
            if 0.0 <= age < 30.0:
                early_buyers.add(tr.wallet)
        if not early_buyers:
            return decision

        try:
            placeholders = ",".join("%s" * len(early_buyers))
            n_bots_row = await asyncio.to_thread(
                self._db._sync_query,
                f"SELECT COUNT(*) AS n FROM wallet_classifications "
                f"WHERE wallet IN ({placeholders}) AND is_bot = 1",
                tuple(early_buyers),
            )
            n_bots = int(n_bots_row[0]["n"] if n_bots_row else 0)
            if n_bots >= self._hard_skip_n:
                logger.warning(
                    "BOT-CLUSTER HARD SKIP %s: %d known is_bot wallets in "
                    "first 30s (≥%d threshold) — skipping entry without "
                    "ML override",
                    mint_short,
                    n_bots,
                    self._hard_skip_n,
                )
                self.bot_cluster_skips += 1
                if self._obs is not None:
                    self._obs.paper_trades_closed.inc(reason="bot_cluster_skip")
                return decision.with_(should_enter=False)
        except Exception as exc:
            logger.debug("Bot-cluster check failed for %s: %s", mint_short, exc)
        return decision

    # ───────────────────────── creator blacklist gate ─────────────

    async def filter_creator_blacklist(
        self,
        token: Any,
        decision: EntryDecision,
        mint_short: str,
    ) -> EntryDecision:
        """Hard skip if ``creators.blacklisted = 1`` for this creator.

        2026-05-01: scorer uses leak-free as-of view that always sets
        ``blacklisted=False`` (see db.py:468). The legacy ``CreatorFilter``
        class in filters/creator.py reads the cumulative table directly
        but is never wired into the live pipeline. This DecisionService
        gate plugs that hole using the same async pattern as
        ``filter_bot_cluster``.

        ``blacklisted`` flag is maintained by
        ``scripts/backfill_creator_blacklist.py`` (hourly cron). Tier-2
        scammer definition: ≥20 tokens, grad_rate<2%, median_peak<28 SOL.
        """
        if not decision.should_enter:
            return decision
        creator = getattr(token, "creator", None)
        if not creator:
            return decision
        try:
            row = await asyncio.to_thread(
                self._db._sync_query,
                "SELECT blacklisted FROM creators WHERE wallet = %s LIMIT 1",
                (creator,),
                True,  # one=True
            )
            if row and int(row.get("blacklisted") or 0) == 1:
                logger.warning(
                    "CREATOR-BLACKLIST HARD SKIP %s: creator %s flagged "
                    "(Tier-2 scammer definition) — skipping entry",
                    mint_short,
                    creator[:12],
                )
                if not hasattr(self, "creator_blacklist_skips"):
                    self.creator_blacklist_skips = 0
                self.creator_blacklist_skips += 1
                if self._obs is not None:
                    self._obs.paper_trades_closed.inc(reason="creator_blacklist_skip")
                return decision.with_(should_enter=False)
        except Exception as exc:
            logger.debug("Creator blacklist check failed for %s: %s", mint_short, exc)
        return decision

    # ───────────────────────── wash-cluster pre-filter ─────────────

    async def filter_wash_cluster(
        self,
        token: Any,
        all_trades: list[Any],
        decision: EntryDecision,
        mint_short: str,
    ) -> EntryDecision:
        """If ``wash_cluster_skip_n`` or more wallets in the first 30s
        share the same cluster_id (and cluster size is in the wash-
        suspect range), force SKIP.

        Wash trading on pump.fun: clustered wallets co-occur in 30-second
        windows across multiple mints; they exit together post-graduation,
        which directly hits paper-trade WR. ``cluster_id`` is populated
        in ``wallet_classifications`` by ``wallet_indexer.py``.

        Threshold tunable via ``PULSE_WASH_CLUSTER_SKIP_N``. Default 0 =
        disabled. Cluster-size band tunable via
        ``PULSE_WASH_CLUSTER_SIZE_MIN`` / ``_MAX`` (default 5..50).

        Same async COUNT pattern as :meth:`filter_bot_cluster` — single
        DB query, runs BEFORE ML override.
        """
        if self._wash_skip_n <= 0:
            return decision
        if not decision.should_enter:
            return decision
        if not (token.created_at or 0) > 0:
            return decision

        early_buyers: set[str] = set()
        for tr in all_trades:
            if tr.tx_type != "buy":
                continue
            age = float(tr.timestamp) - float(token.created_at)
            if 0.0 <= age < 30.0:
                early_buyers.add(tr.wallet)
        if not early_buyers:
            return decision

        try:
            placeholders = ",".join("%s" * len(early_buyers))
            # Group buyers by cluster_id, count per cluster, return only
            # clusters whose total size lands in the wash-suspect band.
            sql = (
                f"WITH buyer_clusters AS ("
                f"  SELECT cluster_id "
                f"    FROM wallet_classifications "
                f"   WHERE wallet IN ({placeholders}) "
                f"     AND cluster_id IS NOT NULL"
                f"), "
                f"cluster_sizes AS ("
                f"  SELECT cluster_id, COUNT(*) AS sz "
                f"    FROM wallet_classifications "
                f"   GROUP BY cluster_id"
                f") "
                f"SELECT bc.cluster_id, COUNT(*) AS n_in_mint, cs.sz "
                f"  FROM buyer_clusters bc "
                f"  JOIN cluster_sizes cs USING (cluster_id) "
                f" WHERE cs.sz BETWEEN %s AND %s "
                f" GROUP BY bc.cluster_id, cs.sz "
                f" ORDER BY n_in_mint DESC LIMIT 1"
            )
            params = tuple(early_buyers) + (self._wash_size_min, self._wash_size_max)
            rows = await asyncio.to_thread(self._db._sync_query, sql, params)
            if rows:
                row = rows[0]
                n_in_mint = int(row["n_in_mint"])
                if n_in_mint >= self._wash_skip_n:
                    cluster_id = row["cluster_id"]
                    cluster_size = int(row["sz"])
                    logger.warning(
                        "WASH-CLUSTER HARD SKIP %s: %d buyers from cluster "
                        "%s (size=%d) in first 30s (≥%d threshold) — "
                        "skipping entry",
                        mint_short,
                        n_in_mint,
                        cluster_id,
                        cluster_size,
                        self._wash_skip_n,
                    )
                    self.wash_cluster_skips += 1
                    if self._obs is not None:
                        self._obs.paper_trades_closed.inc(reason="wash_cluster_skip")
                    return decision.with_(should_enter=False)
        except Exception as exc:
            logger.debug("Wash-cluster check failed for %s: %s", mint_short, exc)
        return decision

    # ───────────────────────── ML override ─────────────────────────

    def apply_ml_override(
        self,
        decision: EntryDecision,
        ml_action: str,
        ml_proba: float,
        ml_cal: float,
        result: Any,
        mint_short: str,
        reg_pnl_pct: float | None = None,
    ) -> EntryDecision:
        """ML hybrid override of the rules decision.

        * ``ml_action="BUY"`` and rules said SKIP → flip to BUY,
          recompute entry metadata from ``result.buy_count`` so the
          paper_trades row is distinguishable from shadow tracking.
        * ``ml_action="SKIP"`` and rules said BUY → flip to SKIP.
        * ``ml_action="RULES"`` (grey zone) → no change.

        ``reg_pnl_pct`` (optional) — predicted realized PnL %% from the
        regression head. When supplied, replaces the legacy
        ``int(ml_cal*100)`` heuristic in ``entry_score`` so downstream
        analytics see a forecasted-return number with sign + magnitude.
        Encoded as ``round(reg_pnl * 10) + 500`` (offset by 500 to keep
        the column non-negative for legacy DB schemas; subtract 500 at
        read-time to recover signed PnL %% × 10). Clamped to [1, 999].
        """
        if ml_action == "BUY" and not decision.should_enter:
            # 2026-05-14 p_cal-floor gate. Per-config minimum on the
            # entry classifier's calibrated probability — the multi-config
            # A/B knob that replaced reg_floor (reg head proven
            # degenerate). The classifier ranks winners at auc_sign≈0.92,
            # so a higher p_cal floor trades volume for selectivity.
            # 0.0 = disabled (LIVE/production default).
            if self._p_cal_floor > 0.0 and float(ml_cal) < self._p_cal_floor:
                logger.warning(
                    "ML OVERRIDE %s [%s]: rules=SKIP → ML=BUY BLOCKED by "
                    "p_cal-floor (p_cal=%.4f < floor=%.4f p_raw=%.3f)",
                    mint_short,
                    self._config_id or "LIVE",
                    float(ml_cal),
                    self._p_cal_floor,
                    ml_proba,
                )
                self.ml_overrides_skip += 1
                if self._obs is not None:
                    self._obs.ml_override.inc(action="p_cal_floor_block")
                return decision  # unchanged — rules SKIP stays
            # 2026-04-29 reg-floor soft gate. Live audit (48h, n=887)
            # showed bottom-quartile predicted PnL → realized -10.11%
            # vs top-quartile → -4.64%. Differential is small (5pp) but
            # reliably worse-end. Configurable kill-floor: if reg_pnl
            # forecast is below ``PULSE_ENTRY_REG_FLOOR_PCT`` (default
            # disabled = -100%), refuse the BUY override. Off by default
            # because live ρ ≈ 0.008 — operator opt-in only.
            if reg_pnl_pct is not None:
                # 2026-05-13: prefer per-config thresholds (set by
                # ``from_entry_config``) and fall back to env for the
                # legacy single-config path so nothing breaks for bots
                # that haven't migrated.
                if self._reg_floor_pct is not None:
                    reg_floor = float(self._reg_floor_pct)
                else:
                    import os as _os_reg

                    reg_floor = float(
                        _os_reg.environ.get("PULSE_ENTRY_REG_FLOOR_PCT", "-100.0")
                    )
                # 2026-05-06 — symmetric sanity ceiling. Live ρ ≈ 0.008
                # means individual reg predictions are weak signals; an
                # outlier predicting +50% PnL is far more likely to be
                # noise than a true winner. Block ML override when reg
                # forecasts an unrealistically bullish trade — same
                # asymmetric-default principle as the survival gate
                # (low-confidence destructive action → skip).
                if self._reg_ceiling_pct is not None:
                    reg_ceiling = float(self._reg_ceiling_pct)
                else:
                    import os as _os_reg

                    reg_ceiling = float(
                        _os_reg.environ.get("PULSE_ENTRY_REG_CEILING_PCT", "30.0")
                    )
                if float(reg_pnl_pct) < reg_floor:
                    logger.warning(
                        "ML OVERRIDE %s: rules=SKIP → ML=BUY "
                        "BLOCKED by reg-floor (reg_pnl=%+.2f%% < floor=%+.2f%% "
                        "p_raw=%.3f p_cal=%.3f)",
                        mint_short,
                        float(reg_pnl_pct),
                        reg_floor,
                        ml_proba,
                        ml_cal,
                    )
                    self.ml_overrides_skip += 1
                    if self._obs is not None:
                        self._obs.ml_override.inc(action="reg_floor_block")
                    return decision  # unchanged — rules SKIP stays
                if float(reg_pnl_pct) > reg_ceiling:
                    logger.warning(
                        "ML OVERRIDE %s: rules=SKIP → ML=BUY "
                        "BLOCKED by reg-ceiling (reg_pnl=%+.2f%% > ceiling=%+.2f%% "
                        "— suspect noise/calibration error; "
                        "p_raw=%.3f p_cal=%.3f)",
                        mint_short,
                        float(reg_pnl_pct),
                        reg_ceiling,
                        ml_proba,
                        ml_cal,
                    )
                    self.ml_overrides_skip += 1
                    if self._obs is not None:
                        self._obs.ml_override.inc(action="reg_ceiling_block")
                    return decision  # unchanged — rules SKIP stays
                logger.warning(
                    "ML OVERRIDE %s: rules=SKIP → ML=BUY "
                    "(p_raw=%.3f p_cal=%.3f reg_pnl=%+.2f%%)",
                    mint_short,
                    ml_proba,
                    ml_cal,
                    float(reg_pnl_pct),
                )
                # Encode signed PnL %% × 10 with offset 500 so negative
                # forecasts stay representable in the int entry_score
                # column. Range [-49.9%, +49.9%] maps to [1, 999].
                _enc = int(round(float(reg_pnl_pct) * 10.0)) + 500
                entry_score_val = max(1, min(999, _enc))
            else:
                logger.warning(
                    "ML OVERRIDE %s: rules=SKIP → ML=BUY (p_raw=%.3f p_cal=%.3f)",
                    mint_short,
                    ml_proba,
                    ml_cal,
                )
                entry_score_val = max(1, int(round(float(ml_cal) * 100.0)))
            full_buy_count = int(getattr(result, "buy_count", 0) or 0)
            self.ml_overrides_buy += 1
            if self._obs is not None:
                self._obs.ml_override.inc(action="buy")
            return decision.with_(
                should_enter=True,
                entry_type="ml_override",
                entry_buyer_num=full_buy_count + 1,
                entry_score=entry_score_val,
            )
        if ml_action == "SKIP" and decision.should_enter:
            logger.warning(
                "ML OVERRIDE %s: rules=BUY → ML=SKIP (p_raw=%.3f p_cal=%.3f)",
                mint_short,
                ml_proba,
                ml_cal,
            )
            self.ml_overrides_skip += 1
            if self._obs is not None:
                self._obs.ml_override.inc(action="skip")
            return decision.with_(should_enter=False)
        return decision

    # ───────────────────────── Checkpoint override ─────────────────

    def apply_checkpoint_override(
        self,
        decision: EntryDecision,
        cp_verdict: str | None,
        cp_proba: float | None,
        cp_source: str,
        all_trades: list[Any],
        mint_short: str,
    ) -> EntryDecision:
        """T+30 / timing checkpoint override.

        ``cp_verdict`` ∈ {None, "BUY_EARLY", "SKIP_EARLY"} from the
        observation_checkpoint_loop. Applied AFTER ML override so the
        early signal can still flip a "rules=BUY → ML=SKIP" decision
        back to BUY (or vice versa) — that's the design.
        """
        if cp_verdict is None:
            return decision
        if cp_verdict == "BUY_EARLY" and not decision.should_enter:
            logger.warning(
                "EARLY OVERRIDE %s: rules=SKIP → %s=BUY (proba=%.3f)",
                mint_short,
                cp_source,
                float(cp_proba) if cp_proba is not None else float("nan"),
            )
            early_buy_count = sum(1 for t in all_trades if t.tx_type == "buy")
            return decision.with_(
                should_enter=True,
                entry_type=cp_source,
                entry_buyer_num=max(1, early_buy_count + 1),
                entry_score=(
                    max(1, int(round(float(cp_proba) * 100.0)))
                    if cp_proba is not None
                    else 1
                ),
            )
        if cp_verdict == "BUY_EARLY":
            # rules already said BUY; just record the source.
            logger.info(
                "EARLY agree %s: %s=BUY confirms rules=BUY (proba=%.3f)",
                mint_short,
                cp_source,
                float(cp_proba) if cp_proba is not None else float("nan"),
            )
            return decision.with_(entry_type=cp_source)
        if cp_verdict == "SKIP_EARLY" and decision.should_enter:
            logger.warning(
                "EARLY OVERRIDE %s: rules=BUY → %s=SKIP (proba=%.3f)",
                mint_short,
                cp_source,
                float(cp_proba) if cp_proba is not None else float("nan"),
            )
            return decision.with_(should_enter=False)
        return decision
