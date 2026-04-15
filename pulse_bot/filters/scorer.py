# pulse_bot/filters/scorer.py
"""Scorer — uses MetricsCalculator + configurable weights for full phase scoring."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

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
        self._metrics = MetricsCalculator(graduation_sol=config.pumpfun_graduation_sol)

    def score(
        self,
        token: Token,
        trades: list[Trade],
        tokens_last_5min: int = 0,
        concurrent_observations: int = 0,
        creator_snapshot: CreatorStats | None = None,
    ) -> ScoringResult:
        """Compute all metrics, run scoring rules, produce decision."""
        self._creator_snapshot = creator_snapshot
        creator_tokens_today = self._db.get_creator_tokens_today_sync(token.creator)

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
        self._last_creator_score = 0
        self._last_creator_reason = ""

        for rule_score, rule_reason, is_reject in self._apply_rules(m, token, trades):
            total_score += rule_score
            reasons.append(rule_reason)
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
            time_to_first_buy=m.time_to_first_buy,
            buys_per_unique=m.buys_per_unique,
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
            creator_score=self._last_creator_score,
            creator_reason=self._last_creator_reason,
            created_at=token.created_at,
            scored_at=time.time(),
        )
        return result

    def _apply_rules(self, m: TokenMetrics, token: Token, trades: list[Trade]):
        """Generator of (score, reason, is_hard_reject) tuples. All configurable."""
        cfg = self._cfg

        # ── Creator (use snapshot taken in main loop for deterministic parallel scoring) ──
        stats = (
            self._creator_snapshot
            if self._creator_snapshot
            else self._db.get_creator_stats_sync(token.creator)
        )
        if stats and stats.blacklisted:
            yield 0, "creator_blacklisted", True
            return
        if stats and stats.total_tokens_created > cfg.creator_serial_threshold:
            yield -5, f"serial_creator({stats.total_tokens_created}tok)", False
        elif stats and stats.total_tokens_created > 1:
            yield 10, f"clean_creator({stats.total_tokens_created}tok)", False

        # ── Unique buyers ──────────────────────────────────
        if m.unique_buyers >= 30:
            yield cfg.buyers_30_score, f"buyers_{m.unique_buyers}(30+)", False
        elif m.unique_buyers >= 10:
            yield cfg.buyers_10_score, f"buyers_{m.unique_buyers}(10+)", False
        elif m.unique_buyers >= 5:
            yield cfg.buyers_5_score, f"buyers_{m.unique_buyers}(5+)", False
        else:
            yield cfg.buyers_low_score, f"buyers_low_{m.unique_buyers}", False

        # ── Volume ─────────────────────────────────────────
        vol = m.total_buy_volume_sol
        if vol > cfg.volume_massive_sol:
            yield cfg.volume_massive_score, f"vol_{vol:.0f}(massive)", False
        elif vol > cfg.volume_high_sol:
            yield cfg.volume_high_score, f"vol_{vol:.1f}(high)", False
        elif vol > cfg.min_buy_volume_sol:
            yield cfg.volume_ok_score, f"vol_{vol:.2f}(ok)", False
        else:
            yield cfg.volume_low_score, f"vol_{vol:.2f}(low)", False

        # ── Diversity ──────────────────────────────────────
        if m.buy_diversity >= 4:
            yield 10, f"diverse_{m.buy_diversity}", False
        elif m.buy_diversity < 2 and m.buy_count > 3:
            yield -15, "uniform_amounts(bot?)", False

        # ── Curve ──────────────────────────────────────────
        pct = m.curve_progress_pct
        if pct > cfg.curve_near_grad_pct:
            yield cfg.curve_near_grad_score, f"near_grad_{pct:.0f}%", False
        elif pct > cfg.max_curve_progress_pct:
            yield cfg.curve_mid_score, f"mid_curve_{pct:.0f}%", False
        elif pct > cfg.curve_low_pct:
            yield cfg.curve_healthy_score, f"curve_{pct:.0f}%(ok)", False
        else:
            yield cfg.curve_low_score, f"curve_low_{pct:.0f}%", False

        # ── Creator sold ───────────────────────────────────
        if m.creator_sold:
            yield cfg.creator_sold_score, "creator_sold", False
        else:
            yield 5, "creator_hold", False

        # ── Whale dominance ────────────────────────────────
        if m.total_buy_volume_sol > 0 and m.max_buy_sol > 0:
            dom = (m.max_buy_sol / m.total_buy_volume_sol) * 100
            if dom > cfg.whale_dominance_pct:
                yield -20, f"whale_{dom:.0f}%", False

        # ── Sell pressure ──────────────────────────────────
        if m.buy_count >= 3:
            ratio = m.sell_ratio
            if ratio >= cfg.sell_ratio_dump:
                yield cfg.sell_dump_penalty, f"dump_{ratio:.1f}x", False
            elif ratio >= cfg.sell_ratio_heavy:
                yield cfg.sell_heavy_penalty, f"sell_heavy_{ratio:.1f}x", False
            elif ratio >= cfg.sell_ratio_moderate:
                yield cfg.sell_moderate_penalty, f"sell_mod_{ratio:.1f}x", False
            else:
                yield cfg.sell_dominant_bonus, f"buy_dominant_{ratio:.1f}x", False

        # ── Top3 concentration ─────────────────────────────
        if m.top3_buyer_pct > 80:
            yield -10, f"top3_{m.top3_buyer_pct:.0f}%", False
        elif m.top3_buyer_pct < 50 and m.unique_buyers >= 5:
            yield 5, f"spread_{m.top3_buyer_pct:.0f}%", False

        # ── Velocity trend ─────────────────────────────────
        if m.buy_velocity_trend > 1.5:
            yield 10, f"accelerating_{m.buy_velocity_trend:.1f}x", False
        elif m.buy_velocity_trend < 0.5 and m.buy_count >= 6:
            yield -10, f"decelerating_{m.buy_velocity_trend:.1f}x", False

        # ── Wash trading detection ─────────────────────────
        if m.buys_per_unique > 2.0 and m.buy_count >= 6:
            yield -15, f"wash_{m.buys_per_unique:.1f}x", False

        # ── First buy sniper ───────────────────────────────
        if m.first_buy_sol > 2.0:
            yield -10, f"sniper_{m.first_buy_sol:.1f}sol", False

        # ── Curve velocity ─────────────────────────────────
        if m.curve_velocity > 0.5:
            yield 5, f"curve_fast_{m.curve_velocity:.2f}sol/s", False

        # N/A filters (need RPC)
        yield 0, "authority:N/A", False
        yield 0, "bundled_buy:N/A", False

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
