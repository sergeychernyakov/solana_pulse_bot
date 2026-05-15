# pulse_bot/pipeline.py
"""Two-phase pipeline: fast entry (5s) + full confirmation (45s)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database
    from pulse_bot.filters.fast import FastFilter
    from pulse_bot.filters.scorer import Scorer
    from pulse_bot.helius_creator import CreatorSnapshotService
    from pulse_bot.helius_holders import HeliusHolderClient
    from pulse_bot.helius_onchain import HeliusOnchainClient
    from pulse_bot.launchpads.base import Launchpad
    from pulse_bot.ml.policy import EntryMLPolicy, EntryT30Policy
    from pulse_bot.models import CreatorStats, Token, Trade

from pulse_bot.ml import shadow
from pulse_bot.ml.policy import (
    get_active_policy_name,
    load_entry_policy_if_available,
    load_entry_t30_policy_if_available,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """Two-phase async pipeline.

    Phase 1 (fast, 5s): Collect trades → FastFilter → FAST_BUY or WAIT
    Phase 2 (full, 45s): Continue collecting → Scorer → BUY/SKIP/BORDERLINE
    Both results stored per token for analysis.
    """

    # Phase 4B — survival hazard model is queried at most once every
    # ``_SURVIVAL_TICK_SECONDS`` per open paper trade. XGBoost call
    # latency is ~5 ms but log spam + DB writes add up at large open
    # position counts; 10 s is the floor recommended by the roadmap.
    _SURVIVAL_TICK_SECONDS: float = 10.0
    # Phase 5 — entry-timing checkpoint cadence. Runs every 15 s
    # starting from the first checkpoint at T+15.
    _TIMING_CHECKPOINT_SECONDS: float = 15.0
    _TIMING_FIRST_CHECKPOINT: float = 15.0
    _TIMING_CONFIDENCE_FLOOR: float = 0.6

    def __init__(
        self,
        config: PulseBotConfig,
        db: Database,
        launchpad: Launchpad,
        scorer: Scorer,
        fast_filter: FastFilter,
        creator_snap_service: CreatorSnapshotService | None = None,
        holder_client: "HeliusHolderClient | None" = None,
        onchain_client: "HeliusOnchainClient | None" = None,
    ) -> None:
        self._config = config
        self._db = db
        self._launchpad = launchpad
        self._scorer = scorer
        self._fast_filter = fast_filter
        self._creator_snap_service = creator_snap_service
        self._holder_client = holder_client
        # Optional on-chain state fetcher. Populates tokens.mint_authority_revoked
        # + tokens.freeze_authority_revoked. Data flows to DB only — these
        # are NOT in ENTRY_FEATURE_ORDER yet (zero-variance on pump.fun,
        # per codex 2026-04-22).
        self._onchain_client = onchain_client
        self._onchain_tasks: set[asyncio.Task] = set()
        # SOL/USD price cache — logs current price at scoring time to
        # token_scores.sol_price_usd. Same "capture now, feature later"
        # pattern as onchain state.
        from pulse_bot.market_context import SOLPriceCache

        self._sol_price = SOLPriceCache()
        # 2026-04-28 (architecture phase F): hydration moved out of
        # this god-object into FeatureHydrationService. Pipeline now
        # calls one method to get everything an ML model needs at
        # decision time.
        from pulse_bot.feature_hydration import FeatureHydrationService

        self._hydration = FeatureHydrationService(
            db=db,
            holder_fetcher=self._fetch_holder_snapshot_all,
        )
        # 2026-04-28 (architecture phase B step 1): entry-override
        # chain extracted into DecisionService. Pipeline now wires the
        # rules → bot-cluster → ML → checkpoint chain through it
        # instead of inlining ~150 lines of branchy logic.
        from pulse_bot.decision_service import DecisionService

        _hard_skip = int(os.environ.get("PULSE_BOT_CLUSTER_HARD_SKIP", "3"))
        # 2026-05-01 (codex review): wash-cluster gate. Default 0 = OFF;
        # ship behind env after optimizer-sweep determines threshold.
        _wash_skip = int(os.environ.get("PULSE_WASH_CLUSTER_SKIP_N", "0"))
        _wash_min = int(os.environ.get("PULSE_WASH_CLUSTER_SIZE_MIN", "5"))
        _wash_max = int(os.environ.get("PULSE_WASH_CLUSTER_SIZE_MAX", "50"))
        self._decision = DecisionService(
            db=db,
            hard_skip_n_env=_hard_skip,
            wash_cluster_skip_n=_wash_skip,
            wash_cluster_size_min=_wash_min,
            wash_cluster_size_max=_wash_max,
        )
        # ── Multi-config A/B framework (2026-05-13) ──────────────────
        # Load the EntryConfig registry; build one DecisionService per
        # config. Every scored token is evaluated through ALL configs in
        # parallel and each config's BUY decisions open paper_trades
        # tagged with config_id. The "live" config additionally drives
        # legacy single-config code paths (logging, resume, counters).
        #
        # Fails open: if the YAML is missing/broken, fall back to
        # single-config mode (only the env-driven ``self._decision``).
        self._config_registry = None
        self._decisions_by_config: dict[str, DecisionService] = {}
        try:
            from pulse_bot.entry_configs import (
                load_registry_from_yaml,
                upsert_registry_to_db,
            )

            self._config_registry = load_registry_from_yaml()
            upsert_registry_to_db(self._config_registry, db)
            for _cfg in self._config_registry.configs:
                self._decisions_by_config[_cfg.config_id] = (
                    DecisionService.from_entry_config(db, _cfg)
                )
            # Re-point the legacy single-config handle at the LIVE config
            # so existing code keeps making the production decision.
            self._decision = self._decisions_by_config[
                self._config_registry.live_config_id
            ]
            logger.info(
                "Multi-config A/B active: %d configs (live=%s)",
                len(self._config_registry.configs),
                self._config_registry.live_config_id,
            )
        except Exception as _mc_exc:
            logger.warning(
                "Multi-config registry load failed (%s) — single-config mode",
                _mc_exc,
                exc_info=True,
            )
            self._config_registry = None
            self._decisions_by_config = {}
        # Real on-chain simulation gate for paper trades. When the
        # config flag is set, every paper trade entry is preflighted
        # via simulateTransaction; failed sims (auth/graduation/etc.)
        # SKIP cleanly instead of contaminating the dataset with
        # phantom fills. Disabled by default — bootstrap returns a
        # no-op stub if PULSE_PAPER_USE_REAL_SIM is not "1".
        from pulse_bot.services.sim_executor import SimExecutor

        self._sim_executor = SimExecutor.bootstrap()
        if config.paper_use_real_sim and not self._sim_executor.enabled:
            logger.warning(
                "paper_use_real_sim=True in config but SimExecutor failed "
                "to bootstrap (env keys missing?) — falling back to "
                "math-based paper trades."
            )
        # Holder-snapshot capture tasks (non-blocking). Bounded via
        # ``_holder_sem`` (50 concurrent fetches max) to prevent
        # unbounded growth when many tokens are awaiting their T+30
        # capture slot (codex v8 audit).
        self._holder_tasks: set[asyncio.Task] = set()
        self._holder_sem: asyncio.Semaphore | None = None
        # Wire DB failure callback so RPC drops don't silently bias data.
        if holder_client is not None:

            def _on_fail(mint, target_age, err_type, detail):
                try:
                    self._db.save_holder_capture_failure(
                        mint, target_age, err_type, detail
                    )
                except Exception:
                    logger.debug("fail-log insert failed", exc_info=True)

            holder_client.on_failure = _on_fail
        self._semaphore = asyncio.Semaphore(config.max_concurrent_observations)
        self._running = False
        self._tokens_seen = 0
        self._tokens_scored = 0
        self._fast_buys = 0
        # Shadow-capture tasks we've kicked off; retained only so we can
        # log exceptions without blocking the main stream.
        self._shadow_tasks: set[asyncio.Task] = set()
        # Atomic slot reservation: _count_open_trades() reads from DB as the
        # source of truth on startup (_resume_open_trades seeds this), but at
        # runtime we reserve slots synchronously so concurrent _handle_token
        # coroutines cannot all pass the cap guard before the first one
        # persists its INSERT (portfolio_max_positions race).
        #
        # 2026-05-13 multi-config: slots are tracked PER config_id so each
        # parallel paper portfolio gets its own portfolio_max_positions
        # budget. ``_open_slots`` stays as a property aliasing the LIVE
        # config's count for legacy callers (resume / dashboards).
        self._open_slots_by_config: dict[str, int] = {}
        _cfg_ids = (
            [c.config_id for c in self._config_registry.configs]
            if self._config_registry is not None
            else ["LIVE"]
        )
        for _cid in _cfg_ids:
            self._open_slots_by_config[_cid] = 0
        # ML entry policy: loads entry model once at start, None if the
        # model file is missing (fresh clone before weekly_retrain).
        #
        # Two modes, controlled by ``PULSE_POLICY`` env var:
        #   * ``rules`` (default): ML proba + feature vector written to
        #     token_scores alongside rule-based decisions for post-hoc
        #     comparison. ML never drives live BUY/SELL.
        #   * ``hybrid``: ML's confidence-gated verdict OVERRIDES rules
        #     when it is confident (``BUY`` / ``SKIP``). Rules handle
        #     the grey zone. Every ML override is logged at WARN so
        #     regressions are visible immediately.
        # 2026-04-28 (architecture phase E): ModelRegistry boot summary.
        # Logs full status of every artifact (existence, schema_version,
        # AUC, health) so operators see the model ensemble at startup.
        from pulse_bot.ml.model_registry import ModelRegistry
        from pulse_bot.observability import metrics as _obs
        from pulse_bot.observability import start_http_server

        self._model_registry = ModelRegistry()
        self._model_registry.log_boot_summary()
        # 2026-05-01: filter health summary — surface dead gates (e.g.,
        # the creators.blacklisted gate that was silently 0-firings for
        # weeks before the 2026-05-01 cleanup pass found it).
        try:
            from pulse_bot.filter_health import log_filter_health_summary

            log_filter_health_summary()
        except Exception as exc:  # nosec B110
            # Reason: best-effort observability; never block startup.
            logger.debug("filter_health summary skipped: %s", exc)
        # Sync model_health gauge so /metrics shows current state.
        for _spec in self._model_registry.list_all():
            _obs.model_health.set(1 if _spec.healthy else 0, name=_spec.name)
        # 2026-04-28 (architecture phase H): expose Prometheus
        # exposition-format /metrics endpoint. Tunable via
        # PULSE_METRICS_PORT (default 9100, set 0 to disable).
        _metrics_port = int(os.environ.get("PULSE_METRICS_PORT", "9100"))
        start_http_server(_metrics_port)

        self._ml_entry_policy: "EntryMLPolicy | None" = load_entry_policy_if_available()
        self._policy_mode: str = get_active_policy_name()
        if self._ml_entry_policy is not None:
            logger.info(
                "ML entry policy loaded (mode=%s). model_hash=%s threshold=%.2f "
                "floor=%.3f ceiling=%.3f",
                self._policy_mode,
                self._ml_entry_policy.model_hash[:16],
                self._ml_entry_policy.threshold,
                self._ml_entry_policy.proba_floor,
                self._ml_entry_policy.proba_ceiling,
            )
        elif self._policy_mode == "hybrid":
            logger.warning(
                "PULSE_POLICY=hybrid but no entry model available — "
                "falling back to rules-only until model is trained.",
            )
            self._policy_mode = "rules"
        self._ml_overrides_buy: int = 0
        self._ml_overrides_skip: int = 0
        # Codex Q4 #1 — entry_model_reg ranking layer (2026-04-28).
        # Loaded as a sibling EntryMLPolicy in regression mode. Used at
        # ml_override decision time to enrich entry_score with predicted
        # PnL % (instead of the int(ml_cal*100) heuristic). No hard gate
        # — current rho ≈ 0.146 is too weak to reject BUYs; informative
        # only. Will become a kill-gate once retrained on better labels.
        self._ml_entry_reg_policy: "EntryMLPolicy | None" = None
        # 2026-05-13 multi-config: configs may point at different reg
        # models (e.g. ROLLBACK uses yesterday's .bak). Load EVERY
        # distinct reg_model_path the registry references, keyed by the
        # relative path string. ``_ml_entry_reg_policy`` stays as the
        # default (entry_model_reg.ubj) handle for legacy code.
        self._reg_policies_by_path: dict[str, "EntryMLPolicy"] = {}
        try:
            from pathlib import Path as _Path

            from pulse_bot.ml.policy._main import EntryMLPolicy as _EntryMLPolicy

            _reg_paths = {"entry_model_reg.ubj"}
            if self._config_registry is not None:
                _reg_paths |= self._config_registry.all_reg_model_paths()
            from pulse_bot.ml.model_registry import assess_skill as _assess_skill

            if self._ml_entry_policy is not None:
                for _rp in sorted(_reg_paths):
                    _full = _Path("data/ml") / _rp
                    if not _full.exists():
                        logger.warning(
                            "Entry reg model %s not found — configs using it "
                            "will fall back to default reg model.",
                            _rp,
                        )
                        continue
                    # Universal skill gate (2026-05-14): a reg head with
                    # no demonstrated rank/sign skill must not gate
                    # trades. Skip loading it — configs pointing at it
                    # then see reg_pnl_pct=None, so reg-floor/ceiling is
                    # bypassed and the (skilled) classifier decides alone.
                    try:
                        _meta = json.loads(_full.with_suffix(".meta.json").read_text())
                        _skilled, _status, _reason = _assess_skill(_meta)
                    except Exception:  # noqa: BLE001 — never block load
                        _skilled, _status, _reason = (
                            True,
                            "unmeasured",
                            "meta unreadable",
                        )
                    if not _skilled:
                        logger.warning(
                            "Entry reg model %s DISABLED — insufficient "
                            "skill (%s). Configs using it fall back to "
                            "classifier-only entry (no reg-floor/ceiling "
                            "gate).",
                            _rp,
                            _reason,
                        )
                        continue
                    _pol = _EntryMLPolicy.from_path(_full)
                    self._reg_policies_by_path[_rp] = _pol
                    logger.info(
                        "Entry regression head loaded: %s model_hash=%s " "(skill: %s)",
                        _rp,
                        _pol.model_hash[:16],
                        _reason,
                    )
            self._ml_entry_reg_policy = self._reg_policies_by_path.get(
                "entry_model_reg.ubj"
            )
        except Exception as exc:
            logger.warning(
                "Entry regression head load failed (%s) — falling back "
                "to int(ml_cal*100) entry_score.",
                exc,
            )
            self._ml_entry_reg_policy = None
            self._reg_policies_by_path = {}
        # Exit ML status log — confirms at boot whether the exit advisor
        # is loaded and whether it can actively escalate rule-based holds.
        from pulse_bot.ml.policy import load_exit_policy_if_available

        _exit_pol = load_exit_policy_if_available()
        if _exit_pol is None:
            logger.info("Exit ML: no model loaded (advisor disabled).")
        elif getattr(self._config, "exit_ml_active", False):
            logger.info(
                "Exit ML ACTIVE: model_hash=%s threshold=%.2f min_hold=%.0fs "
                "— will escalate hold→sell_all when proba>=threshold. "
                "Hard rules (creator_dump/hard_stop/timeout/etc.) remain "
                "immutable.",
                _exit_pol.model_hash[:16],
                self._config.exit_ml_sell_threshold,
                self._config.exit_ml_min_hold_seconds,
            )
        else:
            logger.info(
                "Exit ML shadow-only: model_hash=%s — proba logged to "
                "ExitSignal.ml_exit_proba but never overrides rules. "
                "Set PULSE_EXIT_ML_ACTIVE=1 to activate.",
                _exit_pol.model_hash[:16],
            )

        # ── Phase 3 / 4B / 5 deployment switches (default: OFF) ──────
        # All three integrations are opt-in. With env switches unset the
        # corresponding policy is never loaded and the new code paths are
        # short-circuited at the very top — bot behaviour is bit-for-bit
        # identical to pre-integration. Each switch checked once at boot
        # so a flip requires a restart (matches PULSE_POLICY semantics).
        self._entry_t30_active: bool = (
            os.environ.get("PULSE_ENTRY_T30_ACTIVE", "0") == "1"
        )
        # SKIP-only mode: T+30 model fires SKIP but never BUY. Enables
        # asymmetric activation since meta shows skip_wr≈0.012% (great
        # filter) while buy_wr ≈ 8.6% is still below break-even.
        self._entry_t30_skip_only_active: bool = (
            os.environ.get("PULSE_ENTRY_T30_SKIP_ACTIVE", "0") == "1"
        )
        self._survival_active: bool = (
            os.environ.get("PULSE_SURVIVAL_ACTIVE", "0") == "1"
        )
        self._timing_active: bool = os.environ.get("PULSE_TIMING_ACTIVE", "0") == "1"
        # 2026-05-01 (codex review CRITICAL): parse survival threshold ONCE
        # at startup, fail loudly. Previously parsed inside the per-tick
        # hook; a malformed value (e.g. inline-comment regression in .env)
        # raised ValueError on every survival tick, silently dropping
        # exit decisions.
        try:
            self._survival_threshold: float = float(
                os.environ.get("PULSE_SURVIVAL_THRESHOLD", "0.10")
            )
        except ValueError as exc:
            raise ValueError(
                f"PULSE_SURVIVAL_THRESHOLD must be a float, got: "
                f"{os.environ.get('PULSE_SURVIVAL_THRESHOLD')!r}"
            ) from exc
        if not 0.0 < self._survival_threshold <= 1.0:
            raise ValueError(
                f"PULSE_SURVIVAL_THRESHOLD must be in (0, 1], got: "
                f"{self._survival_threshold}"
            )
        # 2026-05-06 — confidence gate. PULSE_SURVIVAL_THRESHOLD is the
        # *calibration* parameter for the survival curve (where on the
        # decay curve we declare "death"). It does NOT measure how
        # confident the model is in that prediction. Without a separate
        # confidence gate, low-uncertainty predictions (mean hazard ≈ 0.5
        # → confidence ≈ 0) still trigger exits — which is exactly the
        # degeneracy we observed: 92% of trades killed by survival at
        # confidence 0.29. Default 0.50 = act only when hazards are
        # consistently far from 0.5 (clear death signal). Set to 0.0 to
        # bypass the gate (legacy behaviour).
        try:
            self._survival_min_confidence: float = float(
                os.environ.get("PULSE_SURVIVAL_MIN_CONFIDENCE", "0.50")
            )
        except ValueError as exc:
            raise ValueError(
                f"PULSE_SURVIVAL_MIN_CONFIDENCE must be a float, got: "
                f"{os.environ.get('PULSE_SURVIVAL_MIN_CONFIDENCE')!r}"
            ) from exc
        if not 0.0 <= self._survival_min_confidence <= 1.0:
            raise ValueError(
                f"PULSE_SURVIVAL_MIN_CONFIDENCE must be in [0, 1], got: "
                f"{self._survival_min_confidence}"
            )
        # 2026-04-30: skip-only switch mirrors entry_t30 pattern. Set to 1
        # without PULSE_TIMING_ACTIVE — the timing model can SKIP_EARLY but
        # never BUY_EARLY. Useful when shadow data shows BUY-side is anti-
        # correlated (training class imbalance favors SKIP, BUY-proba never
        # crosses production gate). When both flags are 1, ACTIVE wins.
        self._timing_skip_only_active: bool = (
            os.environ.get("PULSE_TIMING_SKIP_ONLY_ACTIVE", "0") == "1"
        )
        self._ml_entry_t30_policy: "EntryT30Policy | None" = None
        # Load policy if either LIVE, SKIP-only, or SHADOW mode is requested.
        if (
            self._entry_t30_active
            or self._entry_t30_skip_only_active
            or shadow.t30_shadow_enabled()
        ):
            self._ml_entry_t30_policy = load_entry_t30_policy_if_available()
            if self._ml_entry_t30_policy is None:
                logger.warning(
                    "PULSE_ENTRY_T30_ACTIVE=1 but no T+30 model could be "
                    "loaded — early-decision hook is a no-op."
                )
            else:
                logger.info(
                    "Entry T+30 ACTIVE: model_hash=%s buy_ceiling=%.3f "
                    "skip_floor=%.3f",
                    self._ml_entry_t30_policy.model_hash[:16],
                    self._ml_entry_t30_policy.buy_ceiling,
                    self._ml_entry_t30_policy.skip_floor,
                )
        # Survival model is loaded lazily on the first paper trade tick
        # (the .ubj load itself is cheap, but we don't want to import
        # xgboost at boot when the switch is off).
        self._survival_model: tuple[Any, dict] | None = None
        self._survival_load_attempted: bool = False
        if self._survival_active:
            logger.info(
                "Survival exit ACTIVE: model will load on first paper "
                "trade tick. min_hold=%.0fs",
                self._config.exit_ml_min_hold_seconds,
            )
        # Entry-timing classifier — store as model_path so each predict
        # call re-uses the loaded booster (predict_entry_timing reloads
        # internally; cached in _timing_booster on first hit).
        self._timing_model_path: Path | None = None
        self._timing_booster_cache: tuple[Any, dict] | None = None
        if (
            self._timing_active
            or self._timing_skip_only_active
            or shadow.timing_shadow_enabled()
        ):
            from pulse_bot.ml.entry_timing import TIMING_SCHEMA_VERSION

            default_path = Path("data/ml/entry_timing_model.ubj")
            self._timing_model_path = default_path
            if not default_path.exists():
                logger.warning(
                    "PULSE_TIMING_ACTIVE=1 but no entry-timing model at "
                    "%s — checkpoint hook is a no-op.",
                    default_path,
                )
                self._timing_model_path = None
            else:
                logger.info(
                    "Entry-timing checkpoint ACTIVE: model=%s "
                    "schema=%s checkpoint_every=15s confidence_floor=0.6",
                    default_path,
                    TIMING_SCHEMA_VERSION,
                )

    @property
    def _live_config_id(self) -> str:
        """The config_id whose decisions drive legacy single-config paths."""
        if self._config_registry is not None:
            return self._config_registry.live_config_id
        return "LIVE"

    @property
    def _open_slots(self) -> int:
        """Legacy alias — open-slot count for the LIVE config.

        Resume logic, dashboards and the portfolio-cap guard read this.
        Multi-config slot accounting lives in ``_open_slots_by_config``.
        """
        return self._open_slots_by_config.get(self._live_config_id, 0)

    @_open_slots.setter
    def _open_slots(self, value: int) -> None:
        self._open_slots_by_config[self._live_config_id] = int(value)

    async def run(self) -> None:
        """Main entry point. Connect to WS and process tokens until interrupted."""
        self._running = True
        logger.info(
            "Pipeline starting — fast=%ds, full=%ds, max_concurrent=%d, fast_threshold=%d, full_threshold=%d",
            self._config.fast_observe_seconds,
            self._config.observe_seconds,
            self._config.max_concurrent_observations,
            self._config.fast_score_threshold,
            self._config.score_threshold_buy,
        )

        await self._launchpad.connect()
        tasks: list[asyncio.Task] = []

        # Resume monitoring open trades from previous run
        await self._resume_open_trades()

        try:
            async for token in self._launchpad.stream_new_tokens():
                if not self._running:
                    break
                self._tokens_seen += 1

                # Single code path: insert is idempotent; creator snapshot
                # is derived leak-free from the tokens table as-of the new
                # token's created_at, so live and replay see identical input.
                await self._db.insert_token(token)
                # Fire-and-forget on-chain state fetch. Captures
                # mint/freeze authority for this token. Cheap (1 RPC) and
                # adds a DB row that is NOT in feature schema yet — so
                # no risk to current model. When variance > 0 we can
                # enable as feature by bumping FEATURE_SCHEMA_VERSION.
                if self._onchain_client is not None:
                    self._schedule_onchain_capture(token.mint)
                creator_snapshot = self._db.get_creator_stats_as_of_sync(
                    token.creator, ref_mint=token.mint
                )
                if self._launchpad.name != "replay":
                    # Side-effects for live only: the cumulative creators
                    # table is now cache/metadata, never read for scoring.
                    await self._db.upsert_creator(token.creator, sold_early=False)
                    self._shadow_capture_creator(token.creator)

                # Both live and replay: parallel processing, deterministic snapshot
                task = asyncio.create_task(
                    self._handle_token_bounded(token, creator_snapshot)
                )
                tasks.append(task)
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled")

        # Wait for all in-flight token handlers to complete
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Drain in-flight holder captures (up to ~125s of T+120 delay
        # tasks) with a bounded timeout so shutdown stays responsive.
        if self._holder_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._holder_tasks, return_exceptions=True),
                    timeout=125.0,
                )
            except TimeoutError:
                logger.warning(
                    "Holder capture drain timed out with %d tasks pending",
                    len(self._holder_tasks),
                )
        if self._holder_client is not None:
            try:
                await self._holder_client.close()
            except Exception:
                logger.debug("holder session close failed", exc_info=True)
        if self._onchain_client is not None:
            try:
                await self._onchain_client.close()
            except Exception:
                logger.debug("onchain session close failed", exc_info=True)
        try:
            await self._sol_price.close()
        except Exception:
            logger.debug("sol price session close failed", exc_info=True)

        await self._launchpad.disconnect()
        logger.info(
            "Pipeline stopped — seen=%d, scored=%d, fast_buys=%d",
            self._tokens_seen,
            self._tokens_scored,
            self._fast_buys,
        )

    def stop(self) -> None:
        """Signal the pipeline to stop gracefully."""
        self._running = False

    def _shadow_capture_creator(self, creator: str) -> None:
        """Fire-and-forget creator snapshot refresh for the new enrichment
        pipeline (#48). Runs in shadow mode: the result is persisted for
        analysis but is not consumed by Scorer — that wire-up happens in a
        later phase once we've verified data quality."""
        if self._creator_snap_service is None:
            return

        async def _run() -> None:
            try:
                await self._creator_snap_service.get_for_live(creator)
            except Exception:
                logger.exception("shadow creator capture failed for %s", creator)

        task = asyncio.create_task(_run())
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    def _schedule_onchain_capture(self, mint: str) -> None:
        """Fire-and-forget SPL Mint state fetch for a new token.

        Reads mint_authority + freeze_authority state once (not
        time-series; these flags don't change post-launch except via
        explicit revoke txs). Writes to tokens.mint_authority_revoked +
        freeze_authority_revoked for future feature use.
        """
        if self._onchain_client is None:
            return

        async def _run() -> None:
            try:
                state = await self._onchain_client.fetch_mint_state(mint)
                if state is None or state.parse_error:
                    return
                await self._db.save_mint_onchain_state(
                    mint,
                    mint_authority_revoked=state.mint_authority_revoked,
                    freeze_authority_revoked=state.freeze_authority_revoked,
                )
            except Exception:
                logger.debug("onchain capture failed for %s", mint, exc_info=True)

        task = asyncio.create_task(_run())
        self._onchain_tasks.add(task)
        task.add_done_callback(self._onchain_tasks.discard)

    def _schedule_holder_capture(self, mint: str, created_at: float) -> None:
        """Schedule 3 holder snapshots (T+30s, T+60s, T+120s) for a new
        token. Bounded by semaphore (concurrent RPC cap) to prevent
        unbounded growth when many tokens await their capture slot.

        Pre-capture death detection via DB trades was removed — trades
        aren't inserted until after the 45s observation window, so at
        T+10 the check would always return "no trades" and censor the
        entire dataset. Analysis instead treats a mint with 3× parse_error
        in ``holder_capture_failures`` (and zero snapshot rows) as a
        pre-capture death class.

        Lag instrumentation (2026-04-25, Phase 3 prereq): logs
        ``Helius T+N capture lag: actual=X scheduled=Y delta=Δ`` per
        capture so we can quantify how far past the target age the
        snapshot actually fired. Three lag sources we measure:

        * ``loop_lag``  — wakeup-from-sleep jitter (asyncio scheduler).
        * ``sem_wait``  — time queued at the concurrency semaphore.
        * ``rpc_time``  — HTTP roundtrip itself.

        Fixes vs. pre-2026-04-25 implementation:

        1. Semaphore raised 50 → 100. At 200 tokens × 3 captures, a 50-
           slot cap with ~500 ms p50 RPC time serialised T+30 bursts:
           bursts of 100+ simultaneous wakeups had to wait ~1 RPC each
           to acquire. Helius free tier comfortably handles 100
           concurrent. Override via ``PULSE_HELIUS_HOLDER_CONCURRENCY``.
        2. Use ``loop.time()`` for the semaphore wait measurement
           (monotonic, immune to wall-clock jumps).
        """
        if self._holder_client is None:
            return
        from pulse_bot.helius_holders import CAPTURE_AGE_SECONDS

        if self._holder_sem is None:
            import os as _os

            sem_size = int(_os.environ.get("PULSE_HELIUS_HOLDER_CONCURRENCY", "100"))
            self._holder_sem = asyncio.Semaphore(sem_size)

        loop = asyncio.get_event_loop()

        async def _run_one(target_age: float) -> None:
            t0 = time.time()
            scheduled_at_wall = created_at + target_age
            try:
                delay = scheduled_at_wall - t0
                if delay > 0:
                    await asyncio.sleep(delay)
                # loop_lag = how much later than scheduled we woke up.
                woke_at = time.time()
                loop_lag = max(0.0, woke_at - scheduled_at_wall)
                # sem_wait = time queued at the semaphore (monotonic).
                sem_wait_start = loop.time()
                async with self._holder_sem:  # type: ignore[arg-type]
                    sem_wait = max(0.0, loop.time() - sem_wait_start)
                    rpc_start = time.time()
                    snap = await self._holder_client.fetch(mint, target_age)
                    rpc_time = max(0.0, time.time() - rpc_start)
                actual_age = (
                    snap.observed_at - created_at if snap is not None else float("nan")
                )
                total_lag = (
                    actual_age - target_age if snap is not None else float("nan")
                )
                logger.info(
                    "Helius T+%.0f capture lag: actual=%.2fs scheduled=%.2fs "
                    "delta=%+.2fs (loop=%.2fs sem=%.2fs rpc=%.2fs) mint=%s",
                    target_age,
                    actual_age,
                    float(target_age),
                    total_lag,
                    loop_lag,
                    sem_wait,
                    rpc_time,
                    mint[:12],
                )
                if snap is not None:
                    await asyncio.to_thread(self._db.save_holder_snapshot, snap)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("holder capture failed for %s", mint[:12])

        for age in CAPTURE_AGE_SECONDS:
            t = asyncio.create_task(_run_one(age))
            self._holder_tasks.add(t)
            t.add_done_callback(self._holder_tasks.discard)

    def _count_open_trades(self) -> int:
        """Count open paper trades from DB — the single source of truth."""
        return self._db.count_open_paper_trades()

    async def _resume_open_trades(self) -> None:
        """Resume monitoring open paper trades from a previous pipeline run.

        Re-subscribes to WS and restarts _paper_trade tasks so positions
        are managed the same way as freshly opened ones.
        """
        from pulse_bot.models import Token

        open_trades = self._db._sync_query(
            """SELECT p.id, p.mint, p.symbol, p.entry_price, p.entry_time,
                      p.entry_mcap_sol, p.entry_buyer_number, p.entry_type,
                      p.entry_score, p.buy_amount_sol, p.price_updated_at,
                      p.config_id, t.creator, t.created_at
               FROM paper_trades p
               LEFT JOIN tokens t ON t.mint = p.mint
               WHERE p.status='open'"""
        )

        if not open_trades:
            return

        logger.info("Resuming %d open trades from previous run", len(open_trades))
        # Seed per-config slot counters so each config's cap enforces
        # correctly on resume. _paper_trade's finally-block decrements
        # the matching config bucket as each position closes.
        for _t in open_trades:
            _cid = _t.get("config_id") or "LIVE"
            self._open_slots_by_config[_cid] = (
                self._open_slots_by_config.get(_cid, 0) + 1
            )

        for t in open_trades:
            token = Token(
                mint=t["mint"],
                name=t["symbol"] or "",
                symbol=t["symbol"] or "",
                creator=t["creator"] or "",
                created_at=t["created_at"] or t["entry_time"] or 0,
                uri="",
                launchpad="pumpfun",
            )
            # Preserve the original open timestamp so hold_seconds stays
            # anchored to when the position was actually opened, not now.
            resume_entry_ts = float(t["entry_time"] or time.time())
            # Restore the last observed activity so a position that was
            # already idle pre-restart can close via dead_token immediately
            # instead of getting a fresh inactivity window.
            resume_last_event_ts = float(
                t["price_updated_at"] or t["entry_time"] or time.time()
            )
            asyncio.create_task(
                self._paper_trade(
                    token,
                    t["entry_price"] or 0,
                    t["entry_mcap_sol"] or 0,
                    t["entry_buyer_number"] or 0,
                    t["entry_type"] or "fast",
                    t["entry_score"] or 0,
                    resume_entry_ts,
                    resume_trade_id=t["id"],
                    resume_last_event_ts=resume_last_event_ts,
                    config_id=t.get("config_id") or "LIVE",
                )
            )

    async def _handle_token_bounded(
        self, token: Token, creator_snapshot: CreatorStats | None = None
    ) -> None:
        """Acquire semaphore, then process token."""
        async with self._semaphore:
            await self._handle_token(token, creator_snapshot)

    async def _handle_token(
        self, token: Token, creator_snapshot: CreatorStats | None = None
    ) -> None:
        """Two-phase pipeline for one token."""
        mint_short = token.mint[:12]

        try:
            logger.info(
                "New token: %s (%s) by %s", token.symbol, mint_short, token.creator[:12]
            )

            # Holder concentration collector (Apr 2026 redesign): fire for
            # EVERY new token, independent of any bot decision. See
            # helius_holders.py for why — in short, gating on FAST_BUY
            # contaminates the dataset with the broken filter's biases.
            # Ground truth comes from objective outcomes (graduation /
            # peak MC / death), not bot PnL. Replay launchpad doesn't
            # expose point-in-time on-chain state so skip there.
            if self._holder_client is not None and self._launchpad.name != "replay":
                self._schedule_holder_capture(token.mint, token.created_at)

            await self._launchpad.subscribe_trades(token.mint)

            # Single collection over the full observation window. We filter
            # trades by trade.timestamp after collection so live and replay
            # see the same deterministic set regardless of wall-clock jitter
            # or WS arrival latency. Live needs a small tail buffer so late-
            # arriving trades (whose block ts is inside the window but WS
            # delivery lagged) are captured before we cut.
            is_replay = self._launchpad.name == "replay"
            collect_duration = self._config.observe_seconds + (
                0.0 if is_replay else 2.0
            )
            collected: list[Trade] = []
            # Phase 3 / 5 deployment hook: when an early-decision policy
            # is active, a parallel checkpoint task watches accumulated
            # trades and may set a verdict (BUY_EARLY / SKIP_EARLY)
            # mid-window. With both switches off the task is never
            # spawned and the collection loop is identical to before.
            checkpoint_state: dict[str, Any] = {"verdict": None}
            checkpoint_task: asyncio.Task | None = None
            if not is_replay and (
                (self._entry_t30_active and self._ml_entry_t30_policy is not None)
                or (
                    self._entry_t30_skip_only_active
                    and self._ml_entry_t30_policy is not None
                )
                or (self._timing_active and self._timing_model_path is not None)
                or (
                    self._timing_skip_only_active
                    and self._timing_model_path is not None
                )
                or (
                    shadow.t30_shadow_enabled()
                    and self._ml_entry_t30_policy is not None
                )
                or (
                    shadow.timing_shadow_enabled()
                    and self._timing_model_path is not None
                )
            ):
                checkpoint_task = asyncio.create_task(
                    self._observation_checkpoint_loop(
                        token, collected, creator_snapshot, checkpoint_state
                    )
                )
            try:
                async for trade in self._launchpad.stream_trades(
                    token.mint, collect_duration
                ):
                    collected.append(trade)
                    if checkpoint_state["verdict"] is not None:
                        # Checkpoint already decided — stop collecting and
                        # let the post-loop logic act on the verdict.
                        break
            finally:
                if checkpoint_task is not None and not checkpoint_task.done():
                    checkpoint_task.cancel()
                    try:
                        await checkpoint_task
                    except (asyncio.CancelledError, Exception):
                        pass

            fast_end = token.created_at + self._config.fast_observe_seconds
            full_end = token.created_at + self._config.observe_seconds
            fast_trades = [t for t in collected if t.timestamp <= fast_end]
            all_trades = [t for t in collected if t.timestamp <= full_end]

            fast_result = self._fast_filter.evaluate(token, fast_trades)

            fast_entry_price = 0.0
            if fast_trades:
                fast_buys = [
                    t for t in fast_trades if t.tx_type == "buy" and t.token_amount > 0
                ]
                if fast_buys:
                    last = fast_buys[-1]
                    fast_entry_price = last.sol_amount / last.token_amount

            if fast_result.decision == "FAST_BUY":
                self._fast_buys += 1
                logger.info(
                    "FAST_BUY %s (%s): score=%d buyers=%d vol=%.2f rate=%.1f/s | %s",
                    token.symbol,
                    mint_short,
                    fast_result.score,
                    fast_result.unique_buyers,
                    fast_result.volume_sol,
                    fast_result.buy_rate,
                    fast_result.reasons[:80],
                )

            # Store all trades and get DB IDs (skip in replay — trades already in DB)
            if not is_replay:
                trade_ids = await self._db.insert_trades_batch(all_trades)
                # Phase E: keep wallet_activity materialized view fresh for
                # top-3 buyer prior-stats features. Running here (after trade
                # insert, before scoring) guarantees the aggregate reflects
                # everything the scorer will see.
                try:
                    await self._db.upsert_wallet_activity_from_trades(all_trades)
                except Exception as exc:
                    logger.warning("wallet_activity upsert failed (non-fatal): %s", exc)
            else:
                trade_ids = [getattr(t, "_db_id", 0) for t in all_trades]
            fast_ids = trade_ids[: len(fast_trades)] if trade_ids else []
            full_ids = trade_ids if trade_ids else []

            # Market context derived deterministically from the tokens
            # table as-of this token's created_at. Same code for live and
            # replay — no semaphore or wall-clock reads.
            tokens_5min = self._db.get_tokens_last_5min_sync(ref_mint=token.mint)
            concurrent = self._db.get_concurrent_observations_sync(
                ref_mint=token.mint,
                observe_seconds=self._config.observe_seconds,
            )
            creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                token.creator, ref_mint=token.mint
            )
            # Pull the merged T+30/T+120 holder snapshot for ML inference.
            # build_dataset.py joins the same rows at training time, so
            # passing them here closes a long-standing train/serve skew
            # gap (predict_proba previously saw holder=None on every
            # token because this fetch wasn't wired).
            holder_snapshot_all: dict = {}
            try:
                holder_snapshot_all = self._fetch_holder_snapshot_all(token.mint)
            except Exception as exc:
                # Promote to WARNING with traceback — silent failures here
                # mean every ML inference falls back to all-zero HELIUS
                # features, which silently re-introduces train/serve skew.
                logger.warning(
                    "holder_snapshot_all fetch failed for %s: %s",
                    token.mint[:12],
                    exc,
                    exc_info=True,
                )
            result = self._scorer.score(
                token,
                all_trades,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
                creator_snapshot=creator_snapshot,
                creator_tokens_today=creator_tokens_today,
                holder_snapshot=holder_snapshot_all or None,
            )

            # Attach fast phase data
            result.source = "backtest" if self._launchpad.name == "replay" else "live"
            result.fast_trade_count = len(fast_trades)
            result.full_trade_count = len(all_trades)
            result.fast_trade_ids = ",".join(str(i) for i in fast_ids)
            result.full_trade_ids = ",".join(str(i) for i in full_ids)
            result.fast_decision = fast_result.decision
            result.fast_score = fast_result.score
            result.fast_reasons = fast_result.reasons
            result.fast_buy_count = fast_result.buy_count
            result.fast_volume_sol = fast_result.volume_sol
            result.fast_buy_rate = fast_result.buy_rate
            result.fast_unique_buyers = fast_result.unique_buyers
            result.fast_sell_ratio = fast_result.sell_ratio
            result.fast_elapsed = fast_result.elapsed
            result.fast_scored_at = token.created_at + self._config.fast_observe_seconds
            result.fast_entry_price = fast_entry_price

            # P&L at fast entry point vs end of full observation
            if fast_entry_price > 0 and result.exit_price > 0:
                result.pnl_at_fast_entry_pct = (
                    (result.exit_price - fast_entry_price) / fast_entry_price
                ) * 100.0

            await self._db.upsert_scoring_result(result)
            self._tokens_scored += 1

            # Fire-and-forget SOL price capture for external context.
            # Cached per-minute inside SOLPriceCache — cheap per call.
            sol_price = await self._sol_price.get()
            if sol_price is not None:
                try:
                    await self._db.save_sol_price(token.mint, sol_price)
                except Exception:
                    logger.debug("save_sol_price failed", exc_info=True)

            # 2026-04-28 (architecture phase F): hydration centralized
            # in FeatureHydrationService. Replaces ~40 lines of inline
            # try/except for top-N wallets + wallet_prior_stats + sniper
            # count. Same train/serve parity guarantees, but tested in
            # isolation (see tests/pulse_bot/test_feature_hydration.py).
            scoring_cutoff_ts = float(result.scored_at or 0.0)
            hydrated = self._hydration.hydrate_for_t90(
                token=token,
                all_trades=all_trades,
                scored_at=scoring_cutoff_ts,
            )
            # Pull legacy variable names so the rest of this function
            # doesn't have to change shape (gradual migration).
            top3_wallets = hydrated.top_n_wallets
            wallet_prior_stats = hydrated.wallet_prior_stats
            n_snipers_first_5s = hydrated.n_buyers_first_5s
            wallet_cls = hydrated.wallet_classifications  # v21

            # Shadow ML logging — never drives live decisions; writes
            # ml_entry_proba + feature vector alongside rules output so
            # we can post-hoc compare ML-vs-rules without trading risk.
            # creator_snapshot is critical: prior to 2026-04-23 it was
            # never passed → 4 CREATOR_FEATURES silently zeroed at live
            # while the trained model expected real values (train/serve
            # skew). Now forwarded explicitly.
            if self._ml_entry_policy is not None:
                try:
                    proba = self._ml_entry_policy.predict_proba(
                        result,
                        holder_snapshot=holder_snapshot_all or None,
                        creator_snapshot=creator_snapshot,
                        wallet_prior_stats=wallet_prior_stats,
                        top3_buyer_wallets=top3_wallets,
                        cutoff_ts=scoring_cutoff_ts,
                        n_buyers_first_5s=n_snipers_first_5s,
                        wallet_classifications=wallet_cls,
                    )
                    feat_json = self._ml_entry_policy.dump_features_json(
                        result,
                        holder_snapshot=holder_snapshot_all or None,
                        creator_snapshot=creator_snapshot,
                        wallet_prior_stats=wallet_prior_stats,
                        top3_buyer_wallets=top3_wallets,
                        cutoff_ts=scoring_cutoff_ts,
                        n_buyers_first_5s=n_snipers_first_5s,
                        wallet_classifications=wallet_cls,
                    )
                    await self._db.save_ml_prediction(
                        mint=token.mint,
                        proba=proba,
                        model_hash=self._ml_entry_policy.model_hash,
                        feature_vector_json=feat_json,
                        schema_version=self._ml_entry_policy.schema_version,
                    )
                    # Reset the fail counter on success so transient blips
                    # don't accumulate into a false alarm.
                    self._ml_shadow_fails = 0
                except Exception as e:
                    # 2026-04-23: was logger.debug — that hid the creator
                    # skew bug for 3 months. Loud by default now: WARN on
                    # every failure with exc_info, ERROR escalation after
                    # 10 consecutive fails so an ongoing silent regression
                    # cannot sit in debug logs unnoticed.
                    self._ml_shadow_fails = getattr(self, "_ml_shadow_fails", 0) + 1
                    logger.warning(
                        "ML shadow predict FAILED for %s (%s): %s",
                        token.mint[:12],
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                    if self._ml_shadow_fails >= 10:
                        logger.error(
                            "ML shadow predict has failed %d times in a "
                            "row. Model or feature pipeline is broken — "
                            "do not trust ml_entry_proba rows after this. "
                            "Check model schema + CreatorStats wiring.",
                            self._ml_shadow_fails,
                        )

            # Log
            log_fn = (
                logger.info
                if result.decision == "BUY" or fast_result.decision == "FAST_BUY"
                else logger.debug
            )
            log_fn(
                "Scored %s (%s): fast=%s(%d) full=%s(%d) buyers=%d vol=%.1f pnl_fast=%+.0f%%",
                token.symbol,
                mint_short,
                fast_result.decision,
                fast_result.score,
                result.decision,
                result.total_score,
                result.unique_buyers,
                result.buy_volume_sol,
                result.pnl_at_fast_entry_pct,
            )

            # Save live decision for backtest comparison
            await self._db.save_live_decision(
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "fast_decision": fast_result.decision,
                    "fast_score": fast_result.score,
                    "full_decision": result.decision,
                    "full_score": result.total_score,
                    "buy_count": result.buy_count,
                    "unique_buyers": result.unique_buyers,
                    "buy_volume_sol": result.buy_volume_sol,
                    "created_at": token.created_at,
                    "decided_at": result.scored_at,
                }
            )

            await self._db.log_event(
                "score",
                {
                    "mint": token.mint,
                    "symbol": token.symbol,
                    "fast": fast_result.decision,
                    "full": result.decision,
                    "fast_score": fast_result.score,
                    "full_score": result.total_score,
                },
            )

            # Entry decision — shared core logic
            from pulse_bot.core import decide_entry

            should_enter, entry_type, entry_score, entry_buyer_num = decide_entry(
                fast_result, result, self._config
            )

            # 2026-04-28 (architecture phase B step 1): bot-cluster
            # pre-filter delegated to DecisionService. Same semantics
            # as before but tested in isolation.
            #
            # 2026-05-13 multi-config: creator-blacklist is config-
            # independent (the creators table is global), so we run it
            # ONCE on the shared base. bot_cluster / wash_cluster /
            # smart-money / top3-PnL are config-parameterised (SCAMSTRICT,
            # NOSCAM, SMARTONLY, TOP3PNL all set different thresholds /
            # toggles) and 2026-05-15 (Round 2) we moved them INSIDE the
            # per-config loop below so each config actually respects its
            # own settings. DB load is trivial at the bot's ~3 tokens/h
            # entry rate even with 20+ configs × 4 queries per token.
            from pulse_bot.decision_service import EntryDecision as _ED

            _decision = _ED(
                should_enter=should_enter,
                entry_type=entry_type,
                entry_score=entry_score,
                entry_buyer_num=entry_buyer_num,
            )
            _decision = await self._decision.filter_creator_blacklist(
                token, _decision, mint_short
            )
            # Shared post-(creator-blacklist) base — every config branches
            # from here and then runs its OWN config-specific pre-filters.
            _decision_base = _decision

            # ── ML confidence-gating (hybrid mode) ────────────────
            # In hybrid mode ML has authority over entry when it is
            # confident. Mechanics:
            #   * action=BUY   → force should_enter=True (override rules
            #                     skip; ML saw a winner pattern).
            #   * action=SKIP  → force should_enter=False (override rules
            #                     buy; ML saw a loser pattern).
            #   * action=RULES → keep rules' verdict (grey zone).
            # Every override logs at WARN so regressions stay visible.
            ml_action = "N/A"
            ml_proba = None
            # reg_pnl_by_model maps reg_model_path → predicted PnL%. We
            # score every distinct reg model the registry references so
            # each config's apply_ml_override sees the right number.
            reg_pnl_by_model: dict[str, float | None] = {}
            if self._policy_mode == "hybrid" and self._ml_entry_policy is not None:
                try:
                    ml_action, ml_proba, ml_cal = (
                        self._ml_entry_policy.decide_with_confidence(
                            result,
                            holder_snapshot=holder_snapshot_all or None,
                            creator_snapshot=creator_snapshot,
                            wallet_prior_stats=wallet_prior_stats,
                            top3_buyer_wallets=top3_wallets,
                            cutoff_ts=scoring_cutoff_ts,
                            n_buyers_first_5s=n_snipers_first_5s,
                            wallet_classifications=wallet_cls,
                        )
                    )
                    # Codex Q4 #1 — regression head ranking (2026-04-28).
                    # 2026-05-13: score EVERY distinct reg model so
                    # configs pointing at different models (e.g. ROLLBACK
                    # → yesterday's .bak) get their own forecast.
                    for _rp, _reg_pol in (self._reg_policies_by_path or {}).items():
                        try:
                            reg_pnl_by_model[_rp] = _reg_pol.predict_score(
                                result,
                                holder_snapshot=holder_snapshot_all or None,
                                creator_snapshot=creator_snapshot,
                                wallet_prior_stats=wallet_prior_stats,
                                top3_buyer_wallets=top3_wallets,
                                cutoff_ts=scoring_cutoff_ts,
                                n_buyers_first_5s=n_snipers_first_5s,
                                wallet_classifications=wallet_cls,
                            )
                        except Exception as _reg_exc:
                            logger.debug(
                                "Entry reg predict_score (%s) failed for %s: %s",
                                _rp,
                                mint_short,
                                _reg_exc,
                            )
                            reg_pnl_by_model[_rp] = None
                except Exception as e:
                    # A broken ML policy must not silently sink live
                    # trading — loud WARN + fall back to rules.
                    logger.warning(
                        "ML decide_with_confidence FAILED for %s (%s): %s; "
                        "falling back to rules decision.",
                        mint_short,
                        type(e).__name__,
                        e,
                        exc_info=True,
                    )
                    ml_action = "N/A"

            # ── Per-config decision fan-out (2026-05-13) ──────────
            # For every loaded config, branch from the shared post-filter
            # base, apply that config's ml_override (its reg_floor + reg
            # model) then the config-independent checkpoint override.
            # ``per_config_decision`` is consumed by the slot/spawn loop
            # below. The LIVE config's result also drives the legacy
            # scalar variables so existing logging/state stays intact.
            cp_kwargs = dict(
                cp_verdict=checkpoint_state.get("verdict"),
                cp_proba=checkpoint_state.get("proba"),
                cp_source=checkpoint_state.get("source", "checkpoint"),
                all_trades=all_trades,
                mint_short=mint_short,
            )
            if self._decisions_by_config:
                _eval_services = self._decisions_by_config
            else:
                _eval_services = {self._live_config_id: self._decision}
            per_config_decision: dict[str, _ED] = {}
            for _cid, _ds in _eval_services.items():
                _d = _decision_base
                # 2026-05-15 Round 2: per-config pre-filters. Each config's
                # own thresholds (bot/wash) and toggles (smart_money,
                # top3_pnl) are applied here, BEFORE its apply_ml_override.
                _d = await _ds.filter_bot_cluster(token, all_trades, _d, mint_short)
                _d = await _ds.filter_wash_cluster(token, all_trades, _d, mint_short)
                _d = await _ds.filter_smart_money_required(
                    token, all_trades, _d, mint_short
                )
                _d = await _ds.filter_top3_positive_pnl(
                    token, all_trades, _d, mint_short
                )
                if (
                    self._policy_mode == "hybrid"
                    and self._ml_entry_policy is not None
                    and ml_action != "N/A"
                ):
                    _cfg = (
                        self._config_registry.by_id(_cid)
                        if self._config_registry is not None
                        else None
                    )
                    _model_path = (
                        _cfg.reg_model_path
                        if _cfg is not None
                        else "entry_model_reg.ubj"
                    )
                    # Explicit None default (not ``reg_pnl``): a config
                    # whose reg model was disabled/missing must get
                    # reg_pnl_pct=None so apply_ml_override bypasses the
                    # reg-floor/ceiling gate — NOT silently substitute
                    # the default model's forecast.
                    _reg_pnl_cfg = reg_pnl_by_model.get(_model_path)
                    _d = _ds.apply_ml_override(
                        _d,
                        ml_action,
                        ml_proba,
                        ml_cal,
                        result,
                        mint_short,
                        reg_pnl_pct=_reg_pnl_cfg,
                    )
                _d = _ds.apply_checkpoint_override(_d, **cp_kwargs)
                per_config_decision[_cid] = _d

            # LIVE config drives legacy scalars + counters.
            _live_decision = per_config_decision.get(
                self._live_config_id,
                _decision_base,
            )
            should_enter = _live_decision.should_enter
            entry_type = _live_decision.entry_type
            entry_score = _live_decision.entry_score
            entry_buyer_num = _live_decision.entry_buyer_num
            self._ml_overrides_buy = self._decision.ml_overrides_buy
            self._ml_overrides_skip = self._decision.ml_overrides_skip

            # ── Quality gates (config-independent) ────────────────
            # collector mode, exit_price<=0, and the real-sim gate
            # apply uniformly: if any of them trip, NO config enters
            # this token. Computed once, then the per-config slot loop
            # below only has to check that config's own slot budget.
            gate_skip_all = False
            # Collector mode: score every token for diagnostics but
            # never open paper trades.
            if self._config.collector_only:
                gate_skip_all = True
            # 2026-04-28: gate on entry_price > 0. ML override path was
            # entering with result.exit_price=0 (token had no observable
            # trades during the scoring window) — phantom positions that
            # never had a real entry price. Skip cleanly.
            _any_wants_entry = any(d.should_enter for d in per_config_decision.values())
            if (result.exit_price or 0.0) <= 0.0:
                if _any_wants_entry:
                    logger.warning(
                        "ML/rules said BUY but exit_price=%.0f for %s — skipping "
                        "(no observable price; ML override quality gate).",
                        result.exit_price or 0.0,
                        mint_short,
                    )
                gate_skip_all = True
            # Real on-chain simulation gate. Runs ONCE — the simulation
            # depends only on the mint + buy size, not on which entry
            # config chose to enter. Result applies to every config.
            sim_meta: dict | None = None
            if (
                not gate_skip_all
                and _any_wants_entry
                and self._config.paper_use_real_sim
                and self._sim_executor.enabled
            ):
                # Match dynamic sizing: sim with the SAME size the
                # supervisor will use at open (read from realized balance).
                from pulse_bot.config import compute_buy_amount_sol as _cba

                realized_for_sim = self._db.get_realized_balance_sync(
                    self._config.portfolio_initial_sol,
                )
                buy_amount_for_sim = _cba(self._config, realized_for_sim)
                sol_amount_lamports = int(buy_amount_for_sim * 1e9)
                sim_res = await self._sim_executor.simulate_entry(
                    token.mint, sol_amount_lamports
                )
                sim_meta = {
                    "entry": {
                        "success": sim_res.success,
                        "expected_tokens_raw": sim_res.expected_tokens_raw,
                        "sol_in_lamports": sim_res.sol_in_lamports,
                        "slippage_bps_cap": sim_res.slippage_bps_cap,
                        "units_consumed": sim_res.units_consumed,
                        "err": str(sim_res.err) if sim_res.err is not None else None,
                    }
                }
                if not sim_res.success:
                    if self._config.paper_sim_gate:
                        logger.info(
                            "REAL_SIM SKIP entry %s: err=%s",
                            mint_short,
                            sim_res.err,
                        )
                        gate_skip_all = True
                    else:
                        logger.info(
                            "REAL_SIM SHADOW would-skip %s: err=%s "
                            "(gate=False, opening paper trade anyway)",
                            mint_short,
                            sim_res.err,
                        )

            # Entry timestamp lives in the SAME clock as the trade stream
            # so ExitManager.elapsed = trade.timestamp − entry_ts is not
            # skewed by provider latency or wall-clock drift.
            if all_trades:
                entry_ts = all_trades[-1].timestamp
            elif self._launchpad.name == "replay":
                entry_ts = token.created_at + self._config.observe_seconds
            else:
                entry_ts = time.time()

            # ── Per-config slot reservation + spawn ───────────────
            # Each config gets its own portfolio_max_positions budget.
            # asyncio is single-threaded so the per-config check+increment
            # is race-free against other _handle_token coroutines.
            # Entry-sim token count — fed to the supervisor's close path
            # for the bonding-curve sell estimate (sim_metadata.exit).
            _sim_entry_tokens: int | None = None
            if sim_meta is not None:
                _e = sim_meta.get("entry") or {}
                if _e.get("success"):
                    _sim_entry_tokens = _e.get("expected_tokens_raw") or None
            any_reserved = False
            for _cid, _cdec in per_config_decision.items():
                if gate_skip_all or not _cdec.should_enter:
                    continue
                _cur = self._open_slots_by_config.get(_cid, 0)
                if _cur >= self._config.portfolio_max_positions:
                    continue
                self._open_slots_by_config[_cid] = _cur + 1
                any_reserved = True
                asyncio.create_task(
                    self._paper_trade(
                        token,
                        result.exit_price,
                        result.market_cap_sol,
                        _cdec.entry_buyer_num,
                        _cdec.entry_type,
                        _cdec.entry_score,
                        entry_ts,
                        config_id=_cid,
                        sim_entry_tokens_raw=_sim_entry_tokens,
                    )
                )

            if any_reserved:
                # Stash real-sim metadata on the freshly-opened LIVE
                # paper_trade row. The sim is per-mint so attaching it
                # to one row (LIVE) is enough for dashboards comparing
                # math vs real fills. Background task — non-blocking.
                if sim_meta is not None:
                    asyncio.create_task(self._attach_sim_metadata(token.mint, sim_meta))
            else:
                # SKIP/RULES path (no config entered): optionally keep
                # collecting trades post-scoring so ML label/sweep
                # pipelines see >observe_seconds of activity.
                extra = float(self._config.pulse_extended_observe_seconds)
                if extra > 0 and not is_replay:
                    asyncio.create_task(self._extended_observation(token.mint, extra))
                else:
                    await self._launchpad.unsubscribe_trades(token.mint)

        except Exception:
            logger.exception("Error processing token %s (%s)", token.symbol, mint_short)
            await self._launchpad.unsubscribe_trades(token.mint)

    def _resolve_tick_seconds(self) -> float:
        """Resolve the timer-tick interval for paper trades (Phase 4A).

        Precedence: ``PULSE_TICK_SECONDS`` env var > ``config.pulse_tick_seconds``
        attr (if present) > 5.0 default. Returning 0 disables the tick task
        entirely — same behaviour as before Phase 4A.

        Reading the env var directly (rather than threading a new config
        field) keeps this orthogonal to the in-flight ``config.py`` commit
        so the two changes can land independently.
        """
        import os

        raw = os.environ.get("PULSE_TICK_SECONDS")
        if raw is not None:
            try:
                return float(raw)
            except ValueError:
                logger.warning("PULSE_TICK_SECONDS=%r is not a number; ignoring", raw)
        # Allow tests / future config to override via attribute without
        # requiring an env var.
        return float(getattr(self._config, "pulse_tick_seconds", 5.0))

    async def _observation_checkpoint_loop(
        self,
        token: "Token",
        collected: list["Trade"],
        creator_snapshot: "CreatorStats | None",
        state: dict,
    ) -> None:
        """Phase 3 + Phase 5 in-window early-decision checkpoint.

        Runs in parallel with the trade-stream collection loop in
        ``_handle_token``. Wakes at fixed offsets (T+15, T+30, T+45,
        T+60, T+75) and asks the registered policies whether the bot
        should jump to entry early or skip immediately.

        Decision priority:
          1. T+30 model (Phase 3) — wakes only at T+30.
          2. Entry-timing classifier (Phase 5) — wakes every 15 s.
          3. If neither fires a verdict by T+90, fall through (caller's
             collection loop will hit its natural deadline).

        Per the deployment spec, T+30 BUY supersedes a same-tick
        timing-classifier verdict — so we evaluate T+30 first at its
        single checkpoint and only run timing as fallback.

        State communication: ``state`` dict is mutated in place with
            ``verdict``: ``"BUY_EARLY"`` / ``"SKIP_EARLY"`` / ``None``,
            ``source``: ``"t30"`` / ``"timing"`` for logs / db.source,
            ``proba``:  numeric used in WARN log.

        On any unhandled exception we log and exit cleanly — the
        collection loop must continue regardless of policy errors.
        """
        try:
            now_offset = self._TIMING_FIRST_CHECKPOINT
            t30_done = False
            window_end = float(self._config.observe_seconds)
            # 2026-04-28 (codex review): event-time watermark.
            # Earlier this loop slept to ``token.created_at + now_offset``
            # in wall-clock and made decisions on whatever ``collected``
            # held at that instant — trades arriving later (provider lag
            # 100-500 ms is normal on PumpPortal) didn't make it into
            # the snapshot. The same checkpoint in replay/backtest uses
            # event-time filtering (``trade.timestamp <= scored_at``) so
            # live and replay diverged exactly when lag was high.
            #
            # Now: after the wall-clock wakeup, also wait until either
            # (a) ``max(collected.timestamp) >= target_age``, or
            # (b) we've waited an extra LAG_BUFFER_SEC for slow trades.
            # Then build the decision input by event-time filter, not
            # by arrival time. Tunable via PULSE_CHECKPOINT_LAG_BUFFER.
            import os as _os_lag

            LAG_BUFFER_SEC = float(
                _os_lag.environ.get("PULSE_CHECKPOINT_LAG_BUFFER", "0.5")
            )
            while now_offset < window_end:
                # Sleep until the next checkpoint relative to token creation.
                target_wall = float(token.created_at) + now_offset
                delay = target_wall - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if state.get("verdict") is not None:
                    return  # already decided

                # Event-time watermark — wait for trades up to target_age
                # to actually arrive, bounded by LAG_BUFFER_SEC.
                target_age = now_offset
                lag_deadline = time.time() + LAG_BUFFER_SEC
                while time.time() < lag_deadline:
                    if not collected:
                        break  # nothing yet, no point waiting
                    # max event-time age observed so far
                    last_age = float(collected[-1].timestamp) - float(token.created_at)
                    if last_age >= target_age:
                        break
                    await asyncio.sleep(0.05)
                if state.get("verdict") is not None:
                    return

                # ── T+30 hook (Phase 3) ─────────────────────────────
                # Two activation modes:
                #   _entry_t30_active = LIVE (t30 verdict influences decision)
                #   t30_shadow_enabled() = SHADOW (verdict logged, never acted on)
                # SHADOW lets us collect production signal for activation gate
                # before flipping to LIVE.
                if (
                    not t30_done
                    and self._ml_entry_t30_policy is not None
                    and abs(now_offset - 30.0) < 1e-6
                    and (
                        self._entry_t30_active
                        or self._entry_t30_skip_only_active
                        or shadow.t30_shadow_enabled()
                    )
                ):
                    t30_done = True
                    # Codex 2026-04-28: filter by event-time, not arrival.
                    # Pass only trades with timestamp <= target_age relative
                    # to mint creation so live decision input matches what
                    # build_dataset_t30 sees in training (`WHERE timestamp
                    # <= scored_at`).
                    visible_t30 = [
                        t
                        for t in collected
                        if (float(t.timestamp) - float(token.created_at)) <= target_age
                    ]
                    verdict = await self._evaluate_t30_checkpoint(
                        token, visible_t30, creator_snapshot
                    )
                    if verdict is not None:
                        action, proba = verdict
                        # Always log to shadow when shadow flag set, regardless
                        # of LIVE flag (for backtesting consistency: same row
                        # written even if a future flip activates LIVE).
                        if shadow.t30_shadow_enabled():
                            shadow.record_t30_shadow(
                                mint=token.mint,
                                scored_at=token.created_at + 30.0,
                                proba=proba,
                                action=action,
                                model_hash=getattr(
                                    self._ml_entry_t30_policy,
                                    "model_hash",
                                    None,
                                ),
                            )
                        # 2026-04-27: confidence gates added. The earlier
                        # design fired SKIP/BUY whenever model action ==
                        # "SKIP"/"BUY" (i.e. proba beyond the meta-tuned
                        # floor/ceiling). Problem: with a miscalibrated or
                        # uncertain model, "barely below floor" still
                        # triggered SKIP and blocked the main T+90 path
                        # from ever buying. Now we require the proba to be
                        # in the *extreme tail* — well past the floor /
                        # ceiling — before overriding rules. Tunable via
                        # PULSE_T30_SKIP_TAIL / PULSE_T30_BUY_TAIL.
                        #
                        # 2026-04-28 retune: skip_tail tightened 0.05 → 0.005.
                        # Live T+30 distribution shows median proba ≈ 0.021,
                        # p90 = 0.086 — at 0.05 tail ~80% of tokens triggered
                        # SKIP_EARLY and blocked main-model ml_override
                        # entirely (the 04-27 reason for disabling the flag).
                        # At 0.005 tail only ~20% bottom (truly DOA) get
                        # short-circuited; main T+90 still sees borderline
                        # tokens. Test set skip_wr at proba<0.15 = 0.05% so
                        # missed-winner cost is negligible.
                        import os as _os_t30

                        t30_skip_tail = float(
                            _os_t30.environ.get("PULSE_T30_SKIP_TAIL", "0.005")
                        )
                        t30_buy_tail = float(
                            _os_t30.environ.get("PULSE_T30_BUY_TAIL", "0.85")
                        )
                        if self._entry_t30_active:
                            state["source"] = "t30"
                            state["proba"] = proba
                            if action == "BUY" and proba > t30_buy_tail:
                                state["verdict"] = "BUY_EARLY"
                                return
                            if action == "SKIP" and proba < t30_skip_tail:
                                state["verdict"] = "SKIP_EARLY"
                                return
                            # Confidence gate failed — defer to T+90.
                        elif (
                            self._entry_t30_skip_only_active
                            and action == "SKIP"
                            and proba < t30_skip_tail
                        ):
                            state["source"] = "t30_skip"
                            state["proba"] = proba
                            state["verdict"] = "SKIP_EARLY"
                            return

                # ── Entry-timing hook (Phase 5) ─────────────────────
                # LIVE: _timing_active influences decision (BUY + SKIP).
                # SKIP-only: _timing_skip_only_active fires SKIP_EARLY only.
                # SHADOW: shadow.timing_shadow_enabled() → log only.
                if self._timing_model_path is not None and (
                    self._timing_active
                    or self._timing_skip_only_active
                    or shadow.timing_shadow_enabled()
                ):
                    # Same event-time filter as T+30 above (codex review
                    # 2026-04-28). Critical for replay parity — timing
                    # snapshots in build_for_token are built with the
                    # exact same `t.timestamp <= snapshot_t` semantics.
                    visible_timing = [
                        t
                        for t in collected
                        if (float(t.timestamp) - float(token.created_at)) <= target_age
                    ]
                    verdict = self._evaluate_timing_checkpoint(
                        token, visible_timing, now_offset
                    )
                    if verdict is not None:
                        action, proba = verdict
                        if shadow.timing_shadow_enabled():
                            shadow.record_timing_shadow(
                                mint=token.mint,
                                scored_at=token.created_at + now_offset,
                                snapshot_t=now_offset,
                                action=action,
                                proba=proba,
                            )
                        # 2026-05-01: import locally — the outer ``_os_t30``
                        # symbol only exists if the t30 block ran above, but
                        # timing can be called without t30 being active.
                        import os as _os_timing

                        timing_gate = float(
                            _os_timing.environ.get(
                                "PULSE_TIMING_CONFIDENCE_GATE", "0.85"
                            )
                        )
                        # Timing is a 3-class softmax — `proba` is the
                        # CONFIDENCE in the chosen class. Same gate for
                        # BUY and SKIP — only fire when model is highly
                        # confident. Tunable via PULSE_TIMING_CONFIDENCE_GATE.
                        if self._timing_active:
                            state["source"] = "timing"
                            state["proba"] = proba
                            # 2026-04-30 (codex review): BUY-side audit shows
                            # mean PnL -7.77% on n=259 in [0.60-0.85), and 0
                            # rows ≥0.85 in shadow. BUY-branch fires only
                            # under PULSE_TIMING_ACTIVE=1, never under SKIP-
                            # only. Future retrains may flip the sign — keep
                            # the branch but guard it on the LIVE flag.
                            if action == "BUY" and proba > timing_gate:
                                state["verdict"] = "BUY_EARLY"
                                return
                            if action == "SKIP" and proba > timing_gate:
                                state["verdict"] = "SKIP_EARLY"
                                return
                            # Confidence gate failed — silently defer.
                        elif (
                            self._timing_skip_only_active
                            and action == "SKIP"
                            and proba > timing_gate
                        ):
                            state["source"] = "timing_skip"
                            state["proba"] = proba
                            state["verdict"] = "SKIP_EARLY"
                            return

                now_offset += self._TIMING_CHECKPOINT_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "checkpoint loop crashed for %s; falling back to T+90 path",
                token.mint[:12],
            )

    async def _evaluate_t30_checkpoint(
        self,
        token: "Token",
        trades_so_far: list["Trade"],
        creator_snapshot: "CreatorStats | None",
    ) -> tuple[str, float] | None:
        """Run the T+30 dual-snapshot model on the partial trade stream.

        Returns ``(action, proba)`` where ``action`` is ``"BUY"``,
        ``"SKIP"`` or ``"DEFER"`` (DEFER → caller continues to T+90).
        Returns ``None`` on any error so the rest of the pipeline keeps
        moving — early-decision is purely additive.

        Builds a partial ScoringResult by re-running the live scorer on
        the subset of trades visible by T+30, then asks
        :class:`EntryT30Policy` for a 3-way verdict.
        """
        try:
            policy = self._ml_entry_t30_policy
            if policy is None:
                return None
            t30_cutoff = token.created_at + 30.0
            visible = [t for t in trades_so_far if t.timestamp <= t30_cutoff]
            tokens_5min = self._db.get_tokens_last_5min_sync(ref_mint=token.mint)
            concurrent = self._db.get_concurrent_observations_sync(
                ref_mint=token.mint, observe_seconds=30.0
            )
            creator_tokens_today = self._db.get_creator_tokens_on_day_sync(
                token.creator, ref_mint=token.mint
            )
            partial = self._scorer.score(
                token,
                visible,
                tokens_last_5min=tokens_5min,
                concurrent_observations=concurrent,
                creator_snapshot=creator_snapshot,
                creator_tokens_today=creator_tokens_today,
            )
            # Holder snapshot @T+30 (best-effort: capture may not have
            # landed yet when the bot polls). We pass None on miss; the
            # T30 policy zero-fills HELIUS_FEATURES_T30 which mirrors
            # the training-time fallback for late captures.
            # Holder capture is scheduled at T+30 — same wall-clock as
            # this hook fires. The Helius RPC call + DB write usually
            # lands within 200-500ms but can race the policy lookup.
            # Retry up to 5× spaced 0.4s apart so we typically read the
            # snapshot the first or second poll without blocking the
            # checkpoint loop for more than ~2s.
            holder_t30 = None
            for attempt in range(5):
                try:
                    holder_t30 = await asyncio.to_thread(
                        self._fetch_holder_snapshot_t30, token.mint
                    )
                except Exception:
                    logger.debug(
                        "t30 holder fetch crash for %s",
                        token.mint[:12],
                        exc_info=True,
                    )
                    holder_t30 = None
                if holder_t30 is not None:
                    break
                if attempt < 4:
                    await asyncio.sleep(0.4)
            # 2026-04-28 (architecture phase F): T+30 hydration via the
            # same FeatureHydrationService used at T+90. ``visible`` is
            # event-time-clipped (see codex Issue #1 fix above), so
            # the service sees the exact slice training pipeline saw.
            # ``getattr`` keeps tests that bypass ``__init__`` working
            # (test_pipeline_integrations builds a Pipeline via
            # ``__new__`` and doesn't run our hydration setup).
            hydration = getattr(self, "_hydration", None)
            if hydration is None:
                from pulse_bot.feature_hydration import FeatureHydrationService

                hydration = FeatureHydrationService(
                    db=self._db,
                    holder_fetcher=getattr(
                        self,
                        "_fetch_holder_snapshot_all",
                        lambda mint: None,
                    ),
                )
            hydrated_t30 = hydration.hydrate_for_t30(
                token=token,
                visible_trades=visible,
                t30_cutoff=t30_cutoff,
            )
            top3_wallets_t30 = hydrated_t30.top_n_wallets
            wallet_prior_stats_t30 = hydrated_t30.wallet_prior_stats
            n_snipers_t30 = hydrated_t30.n_buyers_first_5s
            wallet_cls_t30 = hydrated_t30.wallet_classifications  # v21
            action, proba = policy.decide_with_confidence(
                partial,
                holder_snapshot_t30=holder_t30,
                creator_snapshot=creator_snapshot,
                wallet_prior_stats=wallet_prior_stats_t30,
                top3_buyer_wallets=top3_wallets_t30,
                cutoff_ts=t30_cutoff,
                n_buyers_first_5s=n_snipers_t30,
                wallet_classifications=wallet_cls_t30,
            )
            logger.info(
                "T+30 decision %s: %s proba=%.3f buys=%d (ceiling=%.2f floor=%.2f)",
                token.mint[:12],
                action,
                proba,
                len(visible),
                policy.buy_ceiling,
                policy.skip_floor,
            )
            return action, proba
        except Exception:
            logger.exception(
                "T+30 evaluation crashed for %s — deferring to T+90",
                token.mint[:12],
            )
            return None

    def _fetch_holder_snapshot_t30(self, mint: str) -> dict | None:
        """Best-effort sync DB lookup for the @T+30 holder snapshot.

        Returns a plain dict keyed by ``top1_30 / top5_30 / top10_30 /
        hc_30`` so the T+30 feature extractor sees the names it expects.
        Missing snapshot → ``None`` and the caller zero-fills.
        """
        rows = self._db._sync_query(
            "SELECT top1_pct, top5_pct, top10_pct, holder_count "
            "FROM token_holders_snapshots "
            "WHERE mint = %s AND capture_at_age_sec = 30.0 "
            "AND is_negative_row = 0 LIMIT 1",
            (mint,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "top1_30": float(r.get("top1_pct") or 0.0),
            "top5_30": float(r.get("top5_pct") or 0.0),
            "top10_30": float(r.get("top10_pct") or 0.0),
            "hc_30": float(r.get("holder_count") or 0.0),
        }

    def _fetch_holder_snapshot_all(self, mint: str) -> dict:
        """Build the flat ``HELIUS_FEATURES`` dict expected by
        :meth:`EntryMLPolicy.predict_proba` and the scorer at T+90.

        Pulls the T+30 / T+60 / T+120 rows from ``token_holders_snapshots``
        and synthesises the derived ``*_delta`` and ``hc_velocity`` fields.

        T+120 race: scoring runs at T+90 by ``observe_seconds`` default,
        but the T+120 capture is scheduled at exactly T+120s — so on every
        live token the T+120 row is missing at predict time, leaving 4
        HELIUS + 3 DERIVED features stuck at 0 (~7/82 dead). Linearly
        extrapolate ``top1_120 / top5_120 / top10_120 / hc_120`` from the
        observed (T+30, T+60) pair when T+120 is absent. Train/serve are
        kept consistent — ``build_dataset.py`` learns on the same
        extrapolated synthetic values when the historical T+120 row is
        missing.
        """
        rows = self._db._sync_query(
            "SELECT capture_at_age_sec, top1_pct, top5_pct, top10_pct, "
            "holder_count "
            "FROM token_holders_snapshots "
            "WHERE mint = %s AND capture_at_age_sec IN (30, 60, 120) "
            "AND is_negative_row = 0",
            (mint,),
        )
        by_age: dict[int, dict] = {}
        for r in rows or []:
            age = int(r.get("capture_at_age_sec") or 0)
            by_age[age] = r

        # 2026-05-12 train/serve parity (codex review): pre-fix this fn
        # returned literal 0.0 for every age that wasn't captured. Training
        # paths (build_dataset.py) leave those columns as NaN — pandas /
        # XGBoost natively understand missing-value semantics and the
        # model learned its default-direction routing on the NaN path. By
        # serving 0.0 we were forcing the model to interpret "captures
        # missing" as "holder concentration == 0 %", which is nonsense.
        # Now: emit NaN for every per-age value that the DB lacks. The
        # T+120 extrapolation still fires when (T+30, T+60) are both
        # present; otherwise its result is NaN through propagation, which
        # is the honest answer.
        import math

        _NAN = float("nan")
        out: dict[str, float] = {}

        def _f(row: dict | None, key: str) -> float:
            if row is None:
                return _NAN
            v = row.get(key)
            if v is None:
                return _NAN
            try:
                f = float(v)
            except (TypeError, ValueError):
                return _NAN
            return f

        for age in (30, 60):
            r = by_age.get(age)
            out[f"top1_{age}"] = _f(r, "top1_pct")
            out[f"top5_{age}"] = _f(r, "top5_pct")
            out[f"top10_{age}"] = _f(r, "top10_pct")
            out[f"hc_{age}"] = _f(r, "holder_count")

        r120 = by_age.get(120)
        if r120 is not None:
            out["top1_120"] = _f(r120, "top1_pct")
            out["top5_120"] = _f(r120, "top5_pct")
            out["top10_120"] = _f(r120, "top10_pct")
            out["hc_120"] = _f(r120, "holder_count")
        else:
            # Linear extrapolation from (T+30, T+60). NaN propagates: if
            # either source is NaN, the result is NaN — exactly mirroring
            # build_dataset.py:301-319 which only extrapolates when both
            # sources are non-null.
            def _extrap(
                t30: float,
                t60: float,
                clamp_lo: float = 0.0,
                clamp_hi: float | None = 100.0,
            ) -> float:
                if math.isnan(t30) or math.isnan(t60):
                    return _NAN
                v = 2.0 * t60 - t30
                if clamp_hi is not None:
                    v = min(clamp_hi, v)
                return max(clamp_lo, v)

            def _hc_extrap(hc30: float, hc60: float) -> float:
                if math.isnan(hc30) or math.isnan(hc60):
                    return _NAN
                return max(hc30, hc60, 2.0 * hc60 - hc30)

            out["top1_120"] = _extrap(out["top1_30"], out["top1_60"])
            out["top5_120"] = _extrap(out["top5_30"], out["top5_60"])
            out["top10_120"] = _extrap(out["top10_30"], out["top10_60"])
            out["hc_120"] = _hc_extrap(out["hc_30"], out["hc_60"])

        out["top1_delta"] = out["top1_120"] - out["top1_30"]
        out["top5_delta"] = out["top5_120"] - out["top5_30"]
        out["top10_delta"] = out["top10_120"] - out["top10_30"]
        # hc_velocity propagates NaN naturally — division of NaN is NaN.
        out["hc_velocity"] = (out["hc_120"] - out["hc_30"]) / 90.0
        return out

    def _evaluate_timing_checkpoint(
        self,
        token: "Token",
        trades_so_far: list["Trade"],
        snapshot_t: float,
    ) -> tuple[str, float] | None:
        """Run the per-snapshot entry-timing classifier (Phase 5).

        Returns ``(action, proba_max)`` where ``action`` is one of
        ``BUY`` / ``SKIP`` / ``WAIT_MORE``. Confidence is the predicted
        probability of the argmax class — caller acts only when this
        clears :data:`_TIMING_CONFIDENCE_FLOOR` (default 0.6).
        """
        try:
            from pulse_bot.ml.entry_timing import (
                CLASS_BUY_NOW,
                CLASS_NAMES,
                CLASS_SKIP,
                extract_snapshot_features,
                predict_entry_timing,
            )

            if self._timing_model_path is None:
                return None
            feats = extract_snapshot_features(
                trades_so_far, snapshot_t, float(token.created_at)
            )
            pred = predict_entry_timing(feats, self._timing_model_path)
            probs = pred.as_vector()
            max_proba = max(probs)
            logger.info(
                "TIMING checkpoint %s @T+%.0f: %s (p_wait=%.2f p_buy=%.2f "
                "p_skip=%.2f)",
                token.mint[:12],
                snapshot_t,
                pred.decision,
                probs[0],
                probs[1],
                probs[2],
            )
            if pred.decision == CLASS_NAMES[CLASS_BUY_NOW] and (
                probs[CLASS_BUY_NOW] >= self._TIMING_CONFIDENCE_FLOOR
            ):
                return ("BUY", float(probs[CLASS_BUY_NOW]))
            if pred.decision == CLASS_NAMES[CLASS_SKIP] and (
                probs[CLASS_SKIP] >= self._TIMING_CONFIDENCE_FLOOR
            ):
                return ("SKIP", float(probs[CLASS_SKIP]))
            return ("WAIT_MORE", float(max_proba))
        except Exception:
            logger.exception(
                "timing-classifier checkpoint crashed for %s @T+%.0f",
                token.mint[:12],
                snapshot_t,
            )
            return None

    async def _maybe_survival_exit(
        self,
        runner: Any,
        entry_ts: float,
        now: float,
        mint: str | None = None,
    ) -> Any:
        """Phase 4B — query the survival model and return an early-exit
        result if predicted remaining life is below the configured floor.

        Returns the same shape as ``runner.tick()`` (a ``MonitorResult``
        with ``exit_reason='survival_predict'`` set) so
        ``_close_via_runner_result`` can reuse the standard close path.
        Returns ``None`` to keep holding.

        SHADOW: when ``shadow.survival_shadow_enabled()`` is set, the
        function still runs the model and logs the prediction even if
        ``self._survival_active`` is False — but never returns a non-
        None exit signal.
        """
        # Defaults-OFF gate (LIVE only): if neither LIVE nor SHADOW is
        # set we never even attempt to load the model.
        if not self._survival_active and not shadow.survival_shadow_enabled():
            return None
        try:
            elapsed = max(0.0, now - entry_ts)
            min_hold = float(self._config.exit_ml_min_hold_seconds or 0.0)
            if elapsed < min_hold:
                return None
            if self._survival_model is None and not self._survival_load_attempted:
                self._survival_load_attempted = True
                try:
                    from pulse_bot.ml.survival import load_survival_model

                    self._survival_model = load_survival_model(
                        Path("data/ml/survival_model.ubj")
                    )
                    logger.info(
                        "Survival model loaded: %d features",
                        len(self._survival_model[1].get("features", [])),
                    )
                except FileNotFoundError:
                    logger.info(
                        "Survival ACTIVE but no model at "
                        "data/ml/survival_model.ubj — hook is a no-op."
                    )
                    self._survival_model = None
                except Exception:
                    logger.exception("Failed to load survival model")
                    self._survival_model = None
            if self._survival_model is None:
                return None
            from pulse_bot.ml.survival import predict_remaining_life

            model, meta = self._survival_model
            max_horizon = float(meta.get("max_horizon_seconds", 180.0))
            # 2026-04-30: skip when elapsed exceeds training horizon. Past
            # max_horizon predict_remaining_life returns an empty
            # SurvivalPrediction(remaining_life=0); shadow logs of empty
            # predictions cluttered ~20% of survival rows and would force-
            # exit every late-observed token if survival became LIVE.
            if elapsed >= max_horizon:
                return None
            features_now = self._survival_features_from_runner(runner, elapsed)
            # 2026-04-30 calibration: threshold=0.10 maximises Spearman ρ
            # against actual hold times (+0.500 vs +0.423 at default 0.5)
            # and "kills" only 64% of winners (vs 96% at default).
            # Threshold parsed once at startup (self._survival_threshold)
            # so a malformed env value fails loudly at boot instead of
            # silently dropping every survival tick.
            pred = predict_remaining_life(
                model,
                features_now,
                feature_order=meta["features"],
                bucket_seconds=float(meta.get("bucket_seconds", 5.0)),
                max_horizon_seconds=max_horizon,
                now_elapsed_seconds=elapsed,
                survival_threshold=self._survival_threshold,
            )
            # SHADOW: log every survival prediction (every tick) regardless
            # of LIVE state. Live decision below is gated by _survival_active.
            if shadow.survival_shadow_enabled() and mint is not None:
                shadow.record_survival_shadow(
                    mint=mint,
                    scored_at=entry_ts,
                    snapshot_t=elapsed,
                    remaining_life_seconds=pred.remaining_life_seconds,
                    confidence=pred.confidence,
                    hazard_curve=pred.hazard_curve,
                )
            if not self._survival_active:
                return None
            # 2026-05-06 — confidence gate (Sergey's pull-up): a survival
            # prediction with `pred.confidence < min_confidence` means
            # the hazard curve is close to flat-0.5 (max uncertainty).
            # Acting on it kills positions on noise. Skip the exit and
            # let the trade reach a natural exit reason (TP/SL/dead_token).
            if (
                pred.remaining_life_seconds < 30.0
                and pred.confidence < self._survival_min_confidence
            ):
                logger.info(
                    "Survival exit SKIPPED (low confidence): "
                    "predicted_remaining=%.0fs elapsed=%.0fs "
                    "confidence=%.2f < gate=%.2f — letting trade run",
                    pred.remaining_life_seconds,
                    elapsed,
                    pred.confidence,
                    self._survival_min_confidence,
                )
                return None
            if pred.remaining_life_seconds < 30.0:
                logger.warning(
                    "Survival exit: predicted_remaining=%.0fs elapsed=%.0fs "
                    "confidence=%.2f — closing as survival_predict",
                    pred.remaining_life_seconds,
                    elapsed,
                    pred.confidence,
                )
                # Build a result via runner.timeout_result then override
                # the reason — keeps the exit_price/PnL plumbing identical
                # to the regular close path.
                result = runner.timeout_result()
                # Frozen dataclass may reject assignment; default reason is acceptable.
                try:
                    result.exit_reason = "survival_predict"
                except Exception:  # nosec B110
                    pass
                return result
            return None
        except Exception:
            logger.exception("survival check crashed; continuing to hold")
            return None

    def _survival_features_from_runner(self, runner: Any, elapsed: float) -> dict:
        """Best-effort feature dict for survival inference.

        The training script writes whatever numeric columns survive
        ``_select_feature_columns`` into ``meta['features']``. At the
        very least it always includes ``elapsed_seconds`` and
        ``bucket_index``; richer features (entry_score, entry_mcap_sol,
        entry_buyer_number) come from the paper_trades row but are not
        always reachable from the runner state. Missing keys are
        zero-filled by ``predict_remaining_life``.
        """
        feats: dict[str, float] = {
            "elapsed_seconds": float(elapsed),
            "bucket_index": float(elapsed) / 5.0,
        }
        # Pull whatever numerical state is exposed on the runner.
        for attr in (
            "current_price",
            "total_buys",
            "total_sells",
            "peak_price",
        ):
            # Opportunistic feature extraction; missing/non-numeric attrs ok.
            try:
                v = getattr(runner, attr, None)
                if v is not None:
                    feats[attr] = float(v)
            except Exception:  # nosec B112
                continue
        return feats

    async def _attach_sim_metadata(
        self,
        mint: str,
        sim_meta: dict,
        *,
        max_wait_sec: float = 10.0,
        poll_interval_sec: float = 0.25,
    ) -> None:
        """Find the freshly-opened paper_trade row for ``mint`` and
        write ``sim_meta`` into ``sim_metadata``. PaperTradeSupervisor
        creates the row asynchronously; this poller waits up to
        ``max_wait_sec`` before giving up. Best-effort — failures are
        logged but don't propagate (the trade itself still records)."""
        deadline = time.monotonic() + max_wait_sec
        trade_id: int | None = None
        while time.monotonic() < deadline:
            row = self._db.get_paper_trade_for_resume(mint)
            if row and row.get("id"):
                trade_id = int(row["id"])
                break
            await asyncio.sleep(poll_interval_sec)
        if trade_id is None:
            logger.debug("sim_metadata: no paper_trade row found for %s", mint[:14])
            return
        try:
            self._db.set_paper_trade_sim_metadata(trade_id, sim_meta)
        except Exception as exc:  # nosec B110 — log + continue
            logger.warning("sim_metadata write failed for trade=%s: %s", trade_id, exc)

    async def _extended_observation(self, mint: str, duration_seconds: float) -> None:
        """Continue saving trades for `duration_seconds` after a SKIP decision.

        Lets ML label / sweep pipelines extend beyond the scoring window
        without changing live entry behavior. Runs as a background task,
        unsubscribes when done. Inactivity bound matches ``exit_inactivity_seconds``
        so a token going silent stops the WS subscription early.
        """
        inactivity = float(self._config.exit_inactivity_seconds or 0.0)
        try:
            async for trade in self._launchpad.stream_trades(
                mint, duration_seconds, inactivity_timeout=inactivity
            ):
                try:
                    await self._db.insert_trades_batch([trade])
                except Exception as exc:
                    logger.debug(
                        "extended observation insert failed (non-fatal): %s", exc
                    )
        finally:
            await self._launchpad.unsubscribe_trades(mint)

    async def _paper_trade(
        self,
        token: Token,
        entry_price: float,
        entry_mcap: float,
        entry_buyer_num: int,
        entry_type: str,
        entry_score: int,
        entry_ts: float,
        resume_trade_id: int | None = None,
        resume_last_event_ts: float | None = None,
        config_id: str = "LIVE",
        sim_entry_tokens_raw: int | None = None,
    ) -> None:
        """Paper-trade lifecycle. Implementation lives in
        pulse_bot/paper_trade_supervisor.py (architecture phase B
        step 2, codex 2026-04-28). This thin wrapper preserves the
        existing call sites and signature.

        ``config_id`` (2026-05-13 multi-config A/B): which entry-decision
        config opened this trade. Threaded through to the supervisor so
        the paper_trades row is tagged and the per-config slot counter
        is decremented correctly on close. Defaults to ``"LIVE"``.

        ``sim_entry_tokens_raw`` (2026-05-14 real-sim exit): token count
        from the entry simulation, used by the supervisor's close path
        for the bonding-curve sell estimate."""
        from pulse_bot.paper_trade_supervisor import PaperTradeSupervisor

        supervisor = PaperTradeSupervisor(self)
        await supervisor.run(
            token=token,
            entry_price=entry_price,
            entry_mcap=entry_mcap,
            entry_buyer_num=entry_buyer_num,
            entry_type=entry_type,
            entry_score=entry_score,
            entry_ts=entry_ts,
            resume_trade_id=resume_trade_id,
            resume_last_event_ts=resume_last_event_ts,
            config_id=config_id,
            sim_entry_tokens_raw=sim_entry_tokens_raw,
        )
