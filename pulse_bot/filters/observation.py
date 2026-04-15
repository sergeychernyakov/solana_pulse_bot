# pulse_bot/filters/observation.py
"""Observation window filter — scores based on trade activity during the observation period."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pulse_bot.filters.base import Filter
from pulse_bot.models import FilterResult, ObservationResult, Token, Trade

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig

logger = logging.getLogger(__name__)


class ObservationFilter(Filter):
    """Main scoring filter. Analyzes trades collected during the observation window.

    Sub-scores: unique buyers, volume, buy diversity, curve progress,
    creator behavior, whale dominance, sell pressure.
    """

    name = "observation"

    def __init__(self, config: PulseBotConfig) -> None:
        self._config = config

    def check(self, token: Token, trades: list[Trade]) -> FilterResult:
        """Run all observation sub-scores and return aggregated result."""
        obs = self._compute_observation(token, trades)

        total_score = 0
        reasons: list[str] = []

        sub_checks = [
            self._score_unique_buyers(obs),
            self._score_volume(obs),
            self._score_diversity(obs),
            self._score_curve_progress(obs),
            self._score_creator_behavior(obs),
            self._score_whale_dominance(obs),
            self._score_sell_pressure(obs),
        ]

        for score_delta, reason, hard_reject in sub_checks:
            total_score += score_delta
            reasons.append(reason)
            if hard_reject:
                return FilterResult(
                    filter_name=self.name,
                    score=0,
                    hard_reject=True,
                    reason=" | ".join(reasons),
                )

        return FilterResult(
            filter_name=self.name,
            score=total_score,
            hard_reject=False,
            reason=" | ".join(reasons),
        )

    def get_observation(self, token: Token, trades: list[Trade]) -> ObservationResult:
        """Public access to computed observation metrics (for ScoringResult)."""
        return self._compute_observation(token, trades)

    # ── Observation computation ────────────────────────────────

    def _compute_observation(self, token: Token, trades: list[Trade]) -> ObservationResult:
        """Aggregate raw trades into observation metrics."""
        buys = [t for t in trades if t.tx_type == "buy"]
        sells = [t for t in trades if t.tx_type == "sell"]

        unique_buyers = len({t.wallet for t in buys})
        unique_sellers = len({t.wallet for t in sells})

        buy_amounts = [t.sol_amount for t in buys]
        total_buy_sol = sum(buy_amounts)
        total_sell_sol = sum(t.sol_amount for t in sells)

        creator_sold = any(t.wallet == token.creator for t in sells)

        max_buy = max(buy_amounts, default=0.0)

        curve_pct = 0.0
        if trades:
            last_trade = trades[-1]
            curve_pct = (
                min(
                    (last_trade.v_sol_in_bonding_curve / self._config.pumpfun_graduation_sol) * 100.0,
                    100.0,
                )
                if last_trade.v_sol_in_bonding_curve > 0
                else 0.0
            )

        elapsed = 0.0
        if trades:
            elapsed = trades[-1].timestamp - trades[0].timestamp

        return ObservationResult(
            mint=token.mint,
            unique_buyers=unique_buyers,
            unique_sellers=unique_sellers,
            total_buy_volume_sol=total_buy_sol,
            total_sell_volume_sol=total_sell_sol,
            buy_count=len(buys),
            sell_count=len(sells),
            buy_amounts=buy_amounts,
            curve_progress_pct=curve_pct,
            creator_sold=creator_sold,
            max_single_buy_sol=max_buy,
            observation_seconds=elapsed,
        )

    # ── Sub-scores ─────────────────────────────────────────────

    def _score_unique_buyers(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Score based on number of unique buyers. Higher bar — data shows <10 buyers = weak."""
        if obs.unique_buyers >= 30:
            return 30, f"buyers_{obs.unique_buyers}(30+)", False
        if obs.unique_buyers >= 10:
            return 20, f"buyers_{obs.unique_buyers}(10+)", False
        if obs.unique_buyers >= 5:
            return 5, f"buyers_{obs.unique_buyers}(5+)", False
        return -15, f"buyers_low_{obs.unique_buyers}", False

    def _score_volume(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Score based on total buy volume in SOL. Data shows volume is strongest signal."""
        vol = obs.total_buy_volume_sol
        if vol > 20.0:
            return 25, f"vol_{vol:.0f}sol(massive)", False
        if vol > 5.0:
            return 15, f"vol_{vol:.1f}sol(high)", False
        if vol > self._config.min_buy_volume_sol:
            return 5, f"vol_{vol:.2f}sol(ok)", False
        return -5, f"vol_{vol:.2f}sol(low)", False

    def _score_diversity(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Score based on buy amount diversity (anti-bot check)."""
        if not obs.buy_amounts:
            return 0, "no_buys", False
        unique_amounts = len({round(a, 4) for a in obs.buy_amounts})
        if unique_amounts >= 4:
            return 10, f"diverse_{unique_amounts}amounts", False
        if unique_amounts < 2 and len(obs.buy_amounts) > 3:
            return -15, "uniform_amounts(bot?)", False
        return 0, f"diversity_{unique_amounts}", False

    def _score_curve_progress(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Score based on bonding curve fill. High curve + high volume = momentum, not late entry."""
        pct = obs.curve_progress_pct
        if pct > 70:
            # Near graduation — risky but data shows some still profit
            return -10, f"near_grad_{pct:.0f}%", False
        if pct > self._config.max_curve_progress_pct:
            # Only mild penalty — data shows many >40% tokens are profitable
            return -5, f"mid_curve_{pct:.0f}%", False
        if pct > 10:
            return 5, f"curve_{pct:.0f}%(healthy)", False
        return 0, f"curve_{pct:.1f}%", False

    def _score_creator_behavior(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Penalty if creator sold, but NOT hard reject — sometimes creator sells small and token moons."""
        if obs.creator_sold:
            # Soft penalty instead of hard reject: data shows many profitable tokens have creator sells
            return -15, "creator_sold(-15)", False
        return 5, "creator_hold(+5)", False

    def _score_whale_dominance(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Penalty if a single buyer dominates the volume."""
        if obs.total_buy_volume_sol <= 0 or obs.max_single_buy_sol <= 0:
            return 0, "no_whale_data", False
        dominance = (obs.max_single_buy_sol / obs.total_buy_volume_sol) * 100
        if dominance > self._config.whale_dominance_pct:
            return -20, f"whale_{dominance:.0f}%", False
        return 0, f"whale_ok_{dominance:.0f}%", False

    def _score_sell_pressure(self, obs: ObservationResult) -> tuple[int, str, bool]:
        """Sell ratio is the strongest loss predictor. ratio>=1.0 caught 6/11 losers in backtest."""
        if obs.buy_count < 3:
            return 0, "few_trades", False
        ratio = obs.sell_count / max(obs.buy_count, 1)
        if ratio >= 1.0:
            return -30, f"dump_{ratio:.1f}x", False
        if ratio >= 0.7:
            return -15, f"sell_heavy_{ratio:.1f}x", False
        if ratio >= 0.4:
            return -5, f"sell_moderate_{ratio:.1f}x", False
        return 5, f"buy_dominant_{ratio:.1f}x", False
