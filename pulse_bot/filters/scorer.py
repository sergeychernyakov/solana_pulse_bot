# pulse_bot/filters/scorer.py
"""Scorer — uses MetricsCalculator + configurable weights for full phase scoring."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from pulse_bot.config import PUMPFUN_GRADUATION_SOL
from pulse_bot.filters.metrics import MetricsCalculator, TokenMetrics
from pulse_bot.models import CreatorStats, ScoringResult, Token, Trade

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database

logger = logging.getLogger(__name__)


class Scorer:
    """Full phase scorer. Uses MetricsCalculator for metrics, configurable weights for scoring."""

    def __init__(self, config: PulseBotConfig, db: Database) -> None:
        self._cfg = config
        self._db = db
        self._metrics = MetricsCalculator(graduation_sol=PUMPFUN_GRADUATION_SOL)

    def score(
        self,
        token: Token,
        trades: list[Trade],
        tokens_last_5min: int = 0,
        concurrent_observations: int = 0,
        creator_snapshot: CreatorStats | None = None,
        creator_tokens_today: int | None = None,
        holder_snapshot: dict | None = None,
    ) -> ScoringResult:
        """Compute all metrics, run scoring rules, produce decision.

        `creator_tokens_today` may be passed by the optimizer to reflect the
        value as-of the token's creation date during a historical replay; when
        None we fall back to the live "today" count from the DB.
        """
        if creator_tokens_today is None:
            creator_tokens_today = (
                self._db.get_creator_tokens_today_sync(token.creator)
                if self._db is not None
                else 0
            )

        m = self._metrics.compute(
            token,
            trades,
            creator_tokens_today=creator_tokens_today,
            tokens_last_5min=tokens_last_5min,
            concurrent_observations=concurrent_observations,
        )

        # ── Scoring rules ──────────────────────────────────
        total_score = 0
        reasons: list[str] = []
        hard_rejected = False
        creator_score_local = 0
        creator_reason_local = ""

        for rule_score, rule_reason, is_reject, is_creator in self._apply_rules(
            m, token, trades, creator_snapshot, holder_snapshot
        ):
            total_score += rule_score
            reasons.append(rule_reason)
            if is_creator:
                creator_score_local = rule_score
                creator_reason_local = rule_reason
            if is_reject:
                hard_rejected = True
                total_score = 0
                break

        # P&L calculations
        pnl_5, pnl_10, pnl_20, pnl_50, pnl_100 = self._compute_pnl(trades, m.exit_price)

        # Decision
        if hard_rejected:
            decision = "SKIP"
        elif total_score >= self._cfg.score_threshold_buy:
            decision = "BUY"
        elif total_score >= self._cfg.score_threshold_borderline:
            decision = "BORDERLINE"
        else:
            decision = "SKIP"

        # Build result with ALL metrics
        result = ScoringResult(
            mint=token.mint,
            symbol=token.symbol,
            name=token.name,
            creator=token.creator,
            total_score=total_score,
            decision=decision,
            reasons_summary=" | ".join(reasons),
            # Trade metrics
            buy_count=m.buy_count,
            sell_count=m.sell_count,
            unique_buyers=m.unique_buyers,
            unique_sellers=m.unique_sellers,
            buy_volume_sol=m.total_buy_volume_sol,
            sell_volume_sol=m.total_sell_volume_sol,
            buy_diversity=m.buy_diversity,
            max_buy_sol=m.max_buy_sol,
            creator_sold=m.creator_sold,
            sell_pressure=m.sell_ratio,
            avg_buy_sol=m.avg_buy_sol,
            median_buy_sol=m.median_buy_sol,
            std_buy_sol=m.std_buy_sol,
            top3_buyer_pct=m.top3_buyer_pct,
            repeat_buyer_count=m.repeat_buyer_count,
            first_buy_sol=m.first_buy_sol,
            buy_velocity_trend=m.buy_velocity_trend,
            buy_size_trend=m.buy_size_trend,
            first_half_buy_rate=m.first_half_buy_rate,
            second_half_buy_rate=m.second_half_buy_rate,
            avg_first_half_buy_sol=m.avg_first_half_buy_sol,
            avg_second_half_buy_sol=m.avg_second_half_buy_sol,
            time_gap_median_first20=m.time_gap_median_first20,
            buy_volume_first10s=m.buy_volume_first10s,
            unique_buyers_first30s=m.unique_buyers_first30s,
            unique_buyers_last30s=m.unique_buyers_last30s,
            curve_progress_at_t30=m.curve_progress_at_t30,
            curve_progress_at_t60=m.curve_progress_at_t60,
            curve_progress_at_t90=m.curve_progress_at_t90,
            time_to_first_buy=m.time_to_first_buy,
            buys_per_unique=m.buys_per_unique,
            # 2026-04-23 v11 — collected but NOT yet in ENTRY_FEATURE_ORDER
            # (codex: premature at N=661 / 118 positives, wait for N_pos ≥ 250).
            median_time_between_buys=m.median_time_between_buys,
            buy_wallet_entropy=m.buy_wallet_entropy,
            # Curve
            curve_progress_pct=m.curve_progress_pct,
            curve_velocity=m.curve_velocity,
            curve_acceleration=m.curve_acceleration,
            sol_to_graduation=m.sol_to_graduation,
            market_cap_sol=m.market_cap_sol,
            # Price
            token_price_sol=m.token_price_sol,
            exit_price=m.exit_price,
            pnl_5th_pct=pnl_5,
            pnl_10th_pct=pnl_10,
            pnl_20th_pct=pnl_20,
            pnl_50th_pct=pnl_50,
            pnl_100th_pct=pnl_100,
            # Metadata
            name_length=m.name_length,
            symbol_length=m.symbol_length,
            has_uri=m.has_uri,
            is_all_caps=m.is_all_caps,
            has_numbers=m.has_numbers,
            # Timing
            hour_utc=m.hour_utc,
            creator_tokens_today=m.creator_tokens_today,
            gap_create_to_first_trade=m.gap_create_to_first_trade,
            # Context
            tokens_last_5min=m.tokens_last_5min,
            concurrent_observations=m.concurrent_observations,
            # Timestamps
            creator_score=creator_score_local,
            creator_reason=creator_reason_local,
            created_at=token.created_at,
            scored_at=time.time(),
        )
        return result

    def _apply_rules(
        self,
        m: TokenMetrics,
        token: Token,
        trades: list[Trade],
        creator_snapshot: CreatorStats | None = None,
        holder_snapshot: dict | None = None,
    ):
        """Generator of (score, reason, is_hard_reject, is_creator) tuples."""
        cfg = self._cfg

        # ── Hard entry filters (reject before scoring) ────
        if cfg.min_market_cap_sol > 0 and m.market_cap_sol < cfg.min_market_cap_sol:
            yield 0, f"mcap_too_low_{m.market_cap_sol:.0f}", True, False
            return
        if (
            cfg.max_sell_pressure_for_entry < 999
            and m.sell_ratio > cfg.max_sell_pressure_for_entry
        ):
            yield 0, f"sell_pressure_reject_{m.sell_ratio:.1f}", True, False
            return
        if (
            cfg.min_curve_for_entry > 0
            and m.curve_progress_pct < cfg.min_curve_for_entry
        ):
            yield 0, f"curve_too_low_{m.curve_progress_pct:.0f}%", True, False
            return
        # Rug-filter hard gates (all default-off; optimizer tunes them).
        if (
            cfg.entry_min_unique_buyers_hard > 0
            and m.unique_buyers < cfg.entry_min_unique_buyers_hard
        ):
            yield 0, f"uniq_buyers_low_{m.unique_buyers}", True, False
            return
        if (
            cfg.entry_min_sol_volume_hard > 0
            and m.total_buy_volume_sol < cfg.entry_min_sol_volume_hard
        ):
            yield 0, f"sol_vol_low_{m.total_buy_volume_sol:.2f}", True, False
            return
        if (
            cfg.entry_min_velocity_accel > 0
            and m.buy_velocity_trend < cfg.entry_min_velocity_accel
        ):
            yield 0, f"velocity_fade_{m.buy_velocity_trend:.2f}", True, False
            return
        if (
            cfg.entry_min_curve_velocity > 0
            and m.curve_velocity < cfg.entry_min_curve_velocity
        ):
            yield 0, f"curve_vel_low_{m.curve_velocity:.3f}", True, False
            return
        if m.curve_acceleration < cfg.entry_min_curve_acceleration:
            yield 0, f"curve_accel_low_{m.curve_acceleration:.3f}", True, False
            return
        max_curve_accel = getattr(cfg, "entry_max_curve_acceleration", 100.0)
        if max_curve_accel < 100.0 and m.curve_acceleration > max_curve_accel:
            yield 0, f"curve_accel_high_{m.curve_acceleration:.3f}", True, False
            return
        if (
            cfg.entry_max_top3_buyer_pct < 100
            and m.top3_buyer_pct > cfg.entry_max_top3_buyer_pct
        ):
            yield 0, f"top3_concentrated_{m.top3_buyer_pct:.1f}", True, False
            return
        max_creator_today = getattr(cfg, "creator_max_tokens_today", 1000)
        if max_creator_today < 1000 and m.creator_tokens_today > max_creator_today:
            yield 0, f"creator_spammer_{m.creator_tokens_today}", True, False
            return
        # Helius holder concentration hard gates (Apr 2026). Applied only
        # when a snapshot is available (live path captures T+30; backtest
        # joins with token_holders_snapshots at replay time). Absent
        # snapshot = skip gate (neutral).
        holder = holder_snapshot
        if holder is not None:
            h_top1 = holder.get("top1_pct")
            h_top5 = holder.get("top5_pct")
            h_d1 = holder.get("top1_delta_pct")
            max_top1 = getattr(cfg, "entry_max_top1_holder_pct", 100.0)
            max_top5 = getattr(cfg, "entry_max_top5_holder_pct", 100.0)
            max_d1 = getattr(cfg, "entry_max_top1_delta_pct", 100.0)
            min_d1 = getattr(cfg, "entry_min_top1_delta_pct", -100.0)
            if max_top1 < 100.0 and h_top1 is not None and h_top1 > max_top1:
                yield 0, f"holder_top1_{h_top1:.1f}", True, False
                return
            if max_top5 < 100.0 and h_top5 is not None and h_top5 > max_top5:
                yield 0, f"holder_top5_{h_top5:.1f}", True, False
                return
            # Delta gates (positive delta = dev buying more → likely rug).
            if h_d1 is not None:
                if max_d1 < 100.0 and h_d1 > max_d1:
                    yield 0, f"holder_delta1_high_{h_d1:+.1f}", True, False
                    return
                if min_d1 > -100.0 and h_d1 < min_d1:
                    yield 0, f"holder_delta1_low_{h_d1:+.1f}", True, False
                    return

        # Creator snapshot is always provided by the caller (pipeline /
        # optimizer / backtest) as a leak-free as-of view anchored on
        # ROWID. We never fall back to the cumulative ``creators`` table
        # here — that would leak future upserts back into entry scoring
        # and desynchronise live from replay.
        stats = creator_snapshot
        if stats and stats.blacklisted:
            yield 0, "creator_blacklisted", True, True
            return
        # Snapshot-based hard gates (#58). Only fire if snapshot has enough
        # prior tokens to be statistically meaningful — avoids rejecting
        # brand-new creators on a sample of 1.
        if (
            stats is not None
            and stats.snapshot_prior_tokens >= cfg.creator_snapshot_min_priors
        ):
            if (
                cfg.creator_rug_rate_reject < 1.0
                and stats.rug_rate >= cfg.creator_rug_rate_reject
            ):
                yield (
                    0,
                    f"creator_rug_rate_{stats.rug_rate:.2f}",
                    True,
                    True,
                )
                return
            if (
                cfg.creator_min_graduation_rate > 0.0
                and stats.graduation_rate < cfg.creator_min_graduation_rate
            ):
                yield (
                    0,
                    f"creator_grad_rate_low_{stats.graduation_rate:.2f}",
                    True,
                    True,
                )
                return
            if (
                cfg.creator_min_age_days > 0.0
                and stats.creator_age_days < cfg.creator_min_age_days
            ):
                yield (
                    0,
                    f"creator_too_new_{stats.creator_age_days:.1f}d",
                    True,
                    True,
                )
                return
        if stats and stats.total_tokens_created > cfg.creator_serial_threshold:
            reason = f"serial_creator({stats.total_tokens_created}tok)"
            yield -5, reason, False, True
        elif stats and stats.total_tokens_created > 1:
            reason = f"clean_creator({stats.total_tokens_created}tok)"
            yield 10, reason, False, True
        # Soft bonus: creator has graduated tokens before (from snapshot).
        if (
            stats is not None
            and stats.snapshot_prior_tokens >= cfg.creator_snapshot_min_priors
            and stats.graduation_rate > 0.0
        ):
            bonus = min(20, int(stats.graduation_rate * 30))
            yield bonus, f"creator_graduated_{stats.graduation_rate:.2f}", False, True

        # ── Unique buyers ──────────────────────────────────
        if m.unique_buyers >= 30:
            yield cfg.buyers_30_score, f"buyers_{m.unique_buyers}(30+)", False, False
        elif m.unique_buyers >= 10:
            yield cfg.buyers_10_score, f"buyers_{m.unique_buyers}(10+)", False, False
        elif m.unique_buyers >= 5:
            yield cfg.buyers_5_score, f"buyers_{m.unique_buyers}(5+)", False, False
        else:
            yield cfg.buyers_low_score, f"buyers_low_{m.unique_buyers}", False, False

        # ── Volume ─────────────────────────────────────────
        vol = m.total_buy_volume_sol
        if vol > cfg.volume_massive_sol:
            yield cfg.volume_massive_score, f"vol_{vol:.0f}(massive)", False, False
        elif vol > cfg.volume_high_sol:
            yield cfg.volume_high_score, f"vol_{vol:.1f}(high)", False, False
        elif vol > cfg.min_buy_volume_sol:
            yield cfg.volume_ok_score, f"vol_{vol:.2f}(ok)", False, False
        else:
            yield cfg.volume_low_score, f"vol_{vol:.2f}(low)", False, False

        # ── Diversity ──────────────────────────────────────
        if m.buy_diversity >= 4:
            yield 10, f"diverse_{m.buy_diversity}", False, False
        elif m.buy_diversity < 2 and m.buy_count > 3:
            yield -15, "uniform_amounts(bot?)", False, False

        # ── Curve ──────────────────────────────────────────
        pct = m.curve_progress_pct
        if pct > cfg.curve_near_grad_pct:
            yield cfg.curve_near_grad_score, f"near_grad_{pct:.0f}%", False, False
        elif pct > cfg.max_curve_progress_pct:
            yield cfg.curve_mid_score, f"mid_curve_{pct:.0f}%", False, False
        elif pct > cfg.curve_low_pct:
            yield cfg.curve_healthy_score, f"curve_{pct:.0f}%(ok)", False, False
        else:
            yield cfg.curve_low_score, f"curve_low_{pct:.0f}%", False, False

        # ── Creator sold ───────────────────────────────────
        if m.creator_sold:
            yield cfg.creator_sold_score, "creator_sold", False, False
        else:
            yield 5, "creator_hold", False, False

        # ── Whale dominance ────────────────────────────────
        if m.total_buy_volume_sol > 0 and m.max_buy_sol > 0:
            dom = (m.max_buy_sol / m.total_buy_volume_sol) * 100
            if dom > cfg.whale_dominance_pct:
                yield -20, f"whale_{dom:.0f}%", False, False

        # ── Sell pressure ──────────────────────────────────
        if m.buy_count >= 3:
            ratio = m.sell_ratio
            if ratio >= cfg.sell_ratio_dump:
                yield cfg.sell_dump_penalty, f"dump_{ratio:.1f}x", False, False
            elif ratio >= cfg.sell_ratio_heavy:
                yield cfg.sell_heavy_penalty, f"sell_heavy_{ratio:.1f}x", False, False
            elif ratio >= cfg.sell_ratio_moderate:
                yield cfg.sell_moderate_penalty, f"sell_mod_{ratio:.1f}x", False, False
            else:
                yield cfg.sell_dominant_bonus, f"buy_dominant_{ratio:.1f}x", False, False

        # ── Top3 concentration ─────────────────────────────
        if m.top3_buyer_pct > 80:
            yield -10, f"top3_{m.top3_buyer_pct:.0f}%", False, False
        elif m.top3_buyer_pct < 50 and m.unique_buyers >= 5:
            yield 5, f"spread_{m.top3_buyer_pct:.0f}%", False, False

        # ── Velocity trend ─────────────────────────────────
        if m.buy_velocity_trend > 1.5:
            yield 10, f"accelerating_{m.buy_velocity_trend:.1f}x", False, False
        elif m.buy_velocity_trend < 0.5 and m.buy_count >= 6:
            yield -10, f"decelerating_{m.buy_velocity_trend:.1f}x", False, False

        # ── Wash trading detection ─────────────────────────
        if m.buys_per_unique > 2.0 and m.buy_count >= 6:
            yield -15, f"wash_{m.buys_per_unique:.1f}x", False, False

        # ── First buy sniper ───────────────────────────────
        if m.first_buy_sol > 2.0:
            yield -10, f"sniper_{m.first_buy_sol:.1f}sol", False, False

        # ── Curve velocity ─────────────────────────────────
        if m.curve_velocity > 0.5:
            yield 5, f"curve_fast_{m.curve_velocity:.2f}sol/s", False, False

        # N/A filters (need RPC)
        yield 0, "authority:N/A", False, False
        yield 0, "bundled_buy:N/A", False, False

    def _compute_pnl(
        self, trades: list[Trade], exit_price: float
    ) -> tuple[float, float, float, float, float]:
        """Compute P&L at 5th, 10th, 20th, 50th, 100th average entry."""
        buys = [
            t
            for t in trades
            if t.tx_type == "buy" and t.token_amount > 0 and t.sol_amount > 0
        ]
        results = []
        for nth in [5, 10, 20, 50, 100]:
            if nth <= len(buys) and exit_price > 0:
                first_n = buys[:nth]
                total_sol = sum(t.sol_amount for t in first_n)
                total_tokens = sum(t.token_amount for t in first_n)
                avg_entry = total_sol / total_tokens if total_tokens > 0 else 0
                pnl = (
                    ((exit_price - avg_entry) / avg_entry) * 100.0
                    if avg_entry > 0
                    else 0
                )
                results.append(pnl)
            else:
                results.append(0.0)
        return results[0], results[1], results[2], results[3], results[4]
