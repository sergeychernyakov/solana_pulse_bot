# pulse_bot/feature_hydration.py
"""FeatureHydrationService — single entry point for "fetch all the
feature inputs a model needs at scoring time".

Codex review 2026-04-28 (architecture debt phase F): pipeline.py used
to hydrate creator snapshot, holder snapshot, top-N buyer wallets,
wallet prior stats, and n_buyers_first_5s inline across 5+ different
locations, each with its own try/except and fallback. That's:

* a god-object failure mode (Pipeline knows too much),
* hard to test (each lookup is mocked separately if at all),
* hard to swap (e.g. ClickHouse replica for analytics queries),
* hard to add new features (every new model adds another inline
  block to the same function).

This service centralizes the hydration. Pipeline calls
``hydrate_for_t90(token, all_trades, scored_at)`` and gets back a
single ``HydratedContext`` object holding everything ML / shadow
logging / decide_with_confidence will need. Same for T+30:
``hydrate_for_t30(token, visible_trades, t30_cutoff)``.

Design constraints:

* Pure synchronous DB reads — caller wraps in ``asyncio.to_thread``
  if needed. Keeps the service trivially testable.
* No silent fallbacks: every lookup that fails is logged and the
  corresponding field is None. Caller decides how to handle missing
  data (NaN, defer, error).
* No model knowledge: this layer doesn't know about
  ENTRY_FEATURE_ORDER or proba thresholds. It just gathers raw
  inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass
class HydratedContext:
    """All feature inputs needed by entry / T+30 / timing models.

    A field set to None means "the lookup failed or no data" — the
    consumer must treat it as missing, not as zero. NaN-first policy
    in the feature extractor handles this downstream.
    """

    creator_snapshot: Any | None = None
    holder_snapshot: dict[str, Any] | None = None
    top_n_wallets: list[str] = field(default_factory=list)
    wallet_prior_stats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    wallet_classifications: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    n_buyers_first_5s: float = float("nan")
    cutoff_ts: float = 0.0


class FeatureHydrationService:
    """Build HydratedContext from raw DB + trade stream inputs.

    Args:
        db: ``Database`` instance with sync read APIs we use:
            ``get_creator_stats_as_of_sync``, ``get_wallet_prior_stats_sync``.
        holder_fetcher: Callable ``(mint) → dict | None`` that returns
            the merged holder snapshot. In live this is
            ``Pipeline._fetch_holder_snapshot_all``; in tests pass a
            lambda or a mock.
    """

    def __init__(self, db: Any, holder_fetcher: Any) -> None:
        self._db = db
        self._fetch_holder = holder_fetcher

    def hydrate_for_t90(
        self,
        token: Any,
        all_trades: list[Any],
        scored_at: float,
    ) -> HydratedContext:
        """Build context for the main T+90 entry decision.

        ``all_trades`` is the full collection at scoring time; we use
        it to compute the top-10 buyer wallet list, then look up their
        prior stats. ``scored_at`` is the leakage-safe cutoff for
        ``wallet_prior_stats`` (only trades strictly before it count).
        """
        from pulse_bot.ml.features import (
            compute_n_buyers_first_5s,
            compute_topN_buyer_wallets,
        )

        ctx = HydratedContext(cutoff_ts=float(scored_at or 0.0))

        # 2026-05-06 — train/serve parity fix. ``all_trades`` came from
        # in-memory PumpPortal WS accumulation, which routinely lags the
        # DB at scoring time (15,917 wallet_stats=None vs 30 set in 24h
        # of bot.log). Training pipeline reads trades from DB (build_dataset
        # → trades table), so live hydration must too. Prefer DB whenever
        # it has more trades than the in-memory list.
        try:
            db_trades = self._db.get_trades_for_mint_sync(
                token.mint, float(scored_at or 0.0)
            )
            if db_trades and len(db_trades) >= len(all_trades or []):
                all_trades = db_trades
        except Exception as exc:
            logger.debug(
                "trades fetch failed for %s: %s",
                getattr(token, "mint", "?")[:12],
                exc,
            )

        # Creator snapshot — best effort.
        # 2026-05-01 BUG FIX: was passing ``float(scored_at)`` as the 2nd
        # arg, but ``get_creator_stats_as_of_sync`` expects ``ref_mint``
        # (string). Fixed to pass token.mint. The rollback experiment
        # 2026-05-02 04:40 confirmed this fix isn't the cause of the
        # 11h+ entry drought (s_raw stayed at 0.01 with rollback active).
        try:
            ctx.creator_snapshot = self._db.get_creator_stats_as_of_sync(
                token.creator, token.mint
            )
        except Exception as exc:
            logger.warning(
                "creator_snapshot lookup failed for %s: %s",
                getattr(token, "mint", "?")[:12],
                exc,
            )

        # Holder snapshot (T+30 + T+120 + delta).
        try:
            ctx.holder_snapshot = self._fetch_holder(token.mint) or None
        except Exception as exc:
            logger.warning(
                "holder_snapshot lookup failed for %s: %s",
                token.mint[:12],
                exc,
            )

        # Top-N buyers + their prior stats — leakage-safe.
        try:
            ctx.top_n_wallets = compute_topN_buyer_wallets(all_trades, n=10)
            if ctx.top_n_wallets:
                ctx.wallet_prior_stats = (
                    self._db.get_wallet_prior_stats_sync(
                        ctx.top_n_wallets,
                        exclude_mint=token.mint,
                        cutoff_ts=float(scored_at or 0.0),
                    )
                    or {}
                )
        except Exception as exc:
            logger.warning(
                "wallet_prior_stats lookup failed for %s: %s",
                token.mint[:12],
                exc,
            )
            ctx.top_n_wallets = []
            ctx.wallet_prior_stats = {}

        # Sniper proxy — point-in-time, deterministic from raw trades.
        try:
            ctx.n_buyers_first_5s = compute_n_buyers_first_5s(
                all_trades, float(token.created_at or 0.0)
            )
        except Exception:
            ctx.n_buyers_first_5s = float("nan")

        # v21 — wallet_classifications JOIN. Cheap one-shot SQL
        # against an indexed PG table. Best-effort; missing data
        # means counts will be NaN (XGBoost handles missingness).
        if ctx.top_n_wallets:
            ctx.wallet_classifications = self._fetch_wallet_classifications(
                ctx.top_n_wallets
            )

        return ctx

    def _fetch_wallet_classifications(self, wallets: list[str]) -> dict[str, dict]:
        """Single SELECT against wallet_classifications. Returns
        {wallet: {is_sniper, is_smart_money, is_bot, cluster_size}}
        only for wallets that have a row. Empty dict on failure."""
        if not wallets:
            return {}
        try:
            placeholders = ",".join("?" * len(wallets))
            rows = self._db._sync_query(
                f"SELECT wallet, is_sniper, is_smart_money, is_bot, cluster_size "
                f"FROM wallet_classifications WHERE wallet IN ({placeholders})",
                tuple(wallets),
            )
            return {
                r["wallet"]: {
                    "is_sniper": bool(r.get("is_sniper") or 0),
                    "is_smart_money": bool(r.get("is_smart_money") or 0),
                    "is_bot": bool(r.get("is_bot") or 0),
                    "cluster_size": r.get("cluster_size"),
                }
                for r in rows
            }
        except Exception as exc:
            logger.debug("wallet_classifications lookup failed: %s", exc)
            return {}

    def hydrate_for_t30(
        self,
        token: Any,
        visible_trades: list[Any],
        t30_cutoff: float,
    ) -> HydratedContext:
        """Build context for the T+30 early-decision checkpoint.

        Differs from T+90 only in:
        * ``visible_trades`` is the event-time-clipped slice (at most
          first 30s of activity). Compute top-N from it, not from the
          full trade list.
        * ``cutoff_ts`` is the T+30 boundary, not scored_at.
        * Holder snapshot uses the T+30-specific fetch path (caller
          may pass a different ``holder_fetcher`` in tests).

        We DO NOT recompute the creator snapshot — same creator either
        way, and the live path passes it through from T+90 hydration.
        """
        from pulse_bot.ml.features import (
            compute_n_buyers_first_5s,
            compute_topN_buyer_wallets,
        )

        ctx = HydratedContext(cutoff_ts=float(t30_cutoff or 0.0))

        # 2026-05-06 — train/serve parity. WS in-memory accumulation at
        # T+30 routinely shows ``buys=0`` in pipeline.log even though the
        # DB already has 1-5 trades for the same window (PumpPortal batch
        # / processing lag). Pull from DB and prefer it when it has more
        # rows than the live list. Same logic as T+90.
        try:
            db_trades = self._db.get_trades_for_mint_sync(
                token.mint, float(t30_cutoff or 0.0)
            )
            if db_trades and len(db_trades) >= len(visible_trades or []):
                visible_trades = db_trades
        except Exception as exc:
            logger.debug(
                "t30 trades fetch failed for %s: %s",
                token.mint[:12],
                exc,
            )

        try:
            ctx.top_n_wallets = compute_topN_buyer_wallets(visible_trades, n=10)
            if ctx.top_n_wallets:
                ctx.wallet_prior_stats = (
                    self._db.get_wallet_prior_stats_sync(
                        ctx.top_n_wallets,
                        exclude_mint=token.mint,
                        cutoff_ts=float(t30_cutoff or 0.0),
                    )
                    or {}
                )
        except Exception as exc:
            logger.debug(
                "t30 wallet_prior_stats failed for %s: %s",
                token.mint[:12],
                exc,
            )

        try:
            ctx.n_buyers_first_5s = compute_n_buyers_first_5s(
                visible_trades, float(token.created_at or 0.0)
            )
        except Exception:
            ctx.n_buyers_first_5s = float("nan")

        if ctx.top_n_wallets:
            ctx.wallet_classifications = self._fetch_wallet_classifications(
                ctx.top_n_wallets
            )

        return ctx
