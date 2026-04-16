# pulse_bot/backtest.py
"""BacktestEngine — replays historical data through the full pipeline: score → buy → monitor → exit."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pulse_bot.clock import SimulatedClock
from pulse_bot.config import PUMPFUN_GRADUATION_SOL
from pulse_bot.execution import FillResult, SimulatedExecution
from pulse_bot.filters.fast import FastFilter
from pulse_bot.filters.metrics import MetricsCalculator
from pulse_bot.filters.scorer import Scorer
from pulse_bot.portfolio import Portfolio
from pulse_bot.pulse.exit_manager import ExitManager
from pulse_bot.pulse.monitor import PulseMonitor
from pulse_bot.sources.backtest import BacktestSource

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.db import Database
    from pulse_bot.models import ScoringResult, Token, Trade
    from pulse_bot.portfolio import ClosedTrade

logger = logging.getLogger(__name__)


@dataclass
class EntryCandidate:
    """A timestamped backtest entry decision."""

    token: Token
    entry_time: float
    entry_type: str
    fill: FillResult


@dataclass
class PositionMonitor:
    """Runtime pulse state for one open backtest position."""

    pulse: PulseMonitor
    exit_manager: ExitManager
    recent_trades: list[Trade] = field(default_factory=list)


class BacktestEngine:
    """Replays historical tokens through the complete trading pipeline.

    For each token:
    1. Fast filter (5s trades) → FAST_BUY? → simulate buy
    2. Full scorer (45s trades) → BUY? → simulate buy (if not already in)
    3. Pulse monitor (remaining trades) → exit signals → simulate sell
    4. Record P&L

    All parameters from PulseBotConfig — tunable for optimization.
    """

    def __init__(self, config: PulseBotConfig, db: Database) -> None:
        self._cfg = config
        self._db = db
        self._clock = SimulatedClock()
        self._source = BacktestSource(config.backtest_db_path, self._clock)
        self._fast_filter = FastFilter(config)
        self._scorer = Scorer(config, db)
        self._execution = SimulatedExecution(config)
        self._portfolio = Portfolio(config)
        self._metrics_calc = MetricsCalculator(graduation_sol=PUMPFUN_GRADUATION_SOL)

        # Stats
        self._tokens_seen = 0
        self._tokens_with_trades = 0
        self._fast_buys_attempted = 0
        self._full_buys_attempted = 0

    def run(self) -> BacktestResult:
        """Run the full backtest. Returns summary result."""
        start_wall = time.time()

        token_count = self._source.get_token_count()
        trade_count = self._source.get_trade_count()
        logger.info("Backtest starting: %d tokens, %d trades", token_count, trade_count)

        candidates = self._build_entry_candidates()
        self._run_timeline(candidates)

        logger.info(
            "Processed signals: %d/%d tokens, %d entries, balance=%.4f, positions=%d",
            self._tokens_seen,
            token_count,
            len(candidates),
            self._portfolio.balance,
            self._portfolio.open_count,
        )

        # Force close any remaining positions at last known price
        self._close_remaining_positions()

        elapsed = time.time() - start_wall
        logger.info(
            "Backtest done in %.1fs: %d tokens, %d trades, final_balance=%.4f SOL",
            elapsed,
            self._tokens_seen,
            len(self._portfolio.closed_trades),
            self._portfolio.balance,
        )

        return self._build_result(elapsed)

    def _build_entry_candidates(self) -> list[EntryCandidate]:
        """Score tokens and return entries ordered by their real entry time."""
        candidates: list[EntryCandidate] = []
        for token in self._source.iter_tokens():
            self._tokens_seen += 1
            candidate = self._score_token_for_entry(token)
            if candidate:
                candidates.append(candidate)
            if self._tokens_seen % 100 == 0:
                logger.info(
                    "Scored %d tokens, candidates=%d",
                    self._tokens_seen,
                    len(candidates),
                )
        return sorted(candidates, key=lambda item: item.entry_time)

    def _score_token_for_entry(self, token: Token) -> EntryCandidate | None:
        """Score one token and build a deferred entry candidate.

        Writes scoring result to token_scores with source='backtest'
        so it can be compared with live decisions.
        """
        fast_trades = self._source.get_fast_trades(
            token.mint,
            token.created_at,
            self._cfg.fast_observe_seconds,
        )

        if not fast_trades:
            return None

        self._tokens_with_trades += 1

        fast_result = self._fast_filter.evaluate(token, fast_trades)

        full_trades = self._source.get_full_trades(
            token.mint,
            token.created_at,
            self._cfg.observe_seconds,
        )
        full_result = self._scorer.score(token, full_trades) if full_trades else None

        # Write to token_scores with source='backtest' (same table as live)
        if full_result:
            full_result.source = "backtest"
            full_result.fast_decision = fast_result.decision
            full_result.fast_score = fast_result.score
            full_result.fast_reasons = fast_result.reasons
            full_result.fast_buy_count = fast_result.buy_count
            full_result.fast_volume_sol = fast_result.volume_sol
            full_result.fast_buy_rate = fast_result.buy_rate
            full_result.fast_unique_buyers = fast_result.unique_buyers
            full_result.fast_sell_ratio = fast_result.sell_ratio
            full_result.fast_elapsed = fast_result.elapsed
            full_result.fast_scored_at = (
                token.created_at + self._cfg.fast_observe_seconds
            )
            self._save_score_sync(full_result)

        # Entry decision
        if (
            self._cfg.entry_mode in ("fast", "both")
            and fast_result.decision == "FAST_BUY"
        ):
            fill = self._execution.simulate_buy(fast_trades)
            entry_time = token.created_at + self._cfg.fast_observe_seconds
            return EntryCandidate(token, entry_time, "fast", fill)

        if (
            self._cfg.entry_mode in ("full", "both")
            and full_result
            and full_result.decision == "BUY"
        ):
            fill = self._execution.simulate_buy(full_trades)
            entry_time = token.created_at + self._cfg.observe_seconds
            return EntryCandidate(token, entry_time, "full", fill)

        return None

    def _save_score_sync(self, result: ScoringResult) -> None:
        """Write scoring result to DB synchronously."""
        import sqlite3

        from pulse_bot.db import _SCORE_COLUMNS, Database

        conn = sqlite3.connect(self._cfg.backtest_db_path)
        try:
            placeholders = ", ".join(["?"] * len(_SCORE_COLUMNS))
            cols = ", ".join(_SCORE_COLUMNS)
            values = tuple(
                Database._get_score_value(result, col) for col in _SCORE_COLUMNS
            )
            conn.execute(
                f"INSERT OR REPLACE INTO token_scores ({cols}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    def _run_timeline(self, candidates: list[EntryCandidate]) -> None:
        """Replay entries and exits in global timestamp order."""
        monitors: dict[str, PositionMonitor] = {}
        candidate_index = 0
        self._clock.set(0.0)

        for trade in self._source.iter_trades():
            self._clock.advance_to(trade.timestamp)
            self._close_expired_positions(trade.timestamp, monitors)
            candidate_index = self._open_due_candidates(
                candidates, candidate_index, trade.timestamp, monitors
            )
            if trade.mint in monitors:
                self._handle_position_trade(trade, monitors)

        while candidate_index < len(candidates):
            candidate = candidates[candidate_index]
            self._clock.advance_to(candidate.entry_time)
            self._close_expired_positions(candidate.entry_time, monitors)
            candidate_index = self._open_due_candidates(
                candidates, candidate_index, candidate.entry_time, monitors
            )

    def _open_due_candidates(
        self,
        candidates: list[EntryCandidate],
        start_index: int,
        now_ts: float,
        monitors: dict[str, PositionMonitor],
    ) -> int:
        """Open all entry candidates whose entry time has arrived."""
        index = start_index
        while index < len(candidates) and candidates[index].entry_time <= now_ts:
            candidate = candidates[index]
            self._open_candidate(candidate, monitors)
            index += 1
        return index

    def _open_candidate(
        self,
        candidate: EntryCandidate,
        monitors: dict[str, PositionMonitor],
    ) -> None:
        """Open a position if portfolio constraints allow it at entry time."""
        if (
            not self._portfolio.can_buy
            or candidate.token.mint in self._portfolio.positions
        ):
            return
        opened = self._portfolio.open_position(
            candidate.token.mint,
            candidate.token.symbol,
            candidate.fill,
            candidate.entry_time,
            candidate.entry_type,
        )
        if not opened:
            return
        if candidate.entry_type == "fast":
            self._fast_buys_attempted += 1
        else:
            self._full_buys_attempted += 1
        monitors[candidate.token.mint] = PositionMonitor(
            pulse=PulseMonitor(self._cfg),
            exit_manager=ExitManager(self._cfg),
        )

    def _handle_position_trade(
        self,
        trade: Trade,
        monitors: dict[str, PositionMonitor],
    ) -> None:
        """Feed one trade into the active position monitor."""
        pos = self._portfolio.positions.get(trade.mint)
        monitor = monitors.get(trade.mint)
        if not pos or not monitor:
            return

        monitor.recent_trades.append(trade)
        monitor.recent_trades = monitor.recent_trades[-20:]
        snapshot = monitor.pulse.update(trade)
        if not snapshot:
            return

        current_price = (
            trade.sol_amount / trade.token_amount
            if trade.token_amount > 0
            else pos.entry_price
        )
        pnl_pct = (
            ((current_price - pos.entry_price) / pos.entry_price) * 100
            if pos.entry_price > 0
            else 0
        )
        elapsed = trade.timestamp - pos.entry_time
        signal = monitor.exit_manager.decide(snapshot, pnl_pct, elapsed)

        if signal.action == "sell_partial":
            tokens_to_sell = pos.tokens_held * signal.sell_pct
            fill = self._execution.simulate_sell(monitor.recent_trades, tokens_to_sell)
            self._portfolio.partial_sell(
                trade.mint, signal.sell_pct, fill, trade.timestamp, signal.reason
            )

        elif signal.action == "sell_all":
            tokens_to_sell = pos.tokens_held * pos.remaining_pct
            fill = self._execution.simulate_sell(monitor.recent_trades, tokens_to_sell)
            self._portfolio.close_position(
                trade.mint, fill, trade.timestamp, signal.reason
            )
            monitors.pop(trade.mint, None)

    def _close_expired_positions(
        self,
        now_ts: float,
        monitors: dict[str, PositionMonitor],
    ) -> None:
        """Close positions whose max hold time elapsed before the next event."""
        for mint, pos in list(self._portfolio.positions.items()):
            if now_ts - pos.entry_time <= self._cfg.exit_max_hold_seconds:
                continue
            monitor = monitors.get(mint)
            recent = monitor.recent_trades if monitor else []
            tokens_left = pos.tokens_held * pos.remaining_pct
            fill = self._execution.simulate_sell(recent, tokens_left)
            self._portfolio.close_position(
                mint, fill, pos.entry_time + self._cfg.exit_max_hold_seconds, "timeout"
            )
            monitors.pop(mint, None)

    def _close_remaining_positions(self) -> None:
        """Force close all open positions at last available price."""
        for mint in list(self._portfolio.positions.keys()):
            pos = self._portfolio.positions[mint]
            trades = self._source.get_all_trades_after(mint, pos.entry_time)
            last_trades = trades[-20:] if trades else []
            tokens_left = pos.tokens_held * pos.remaining_pct
            fill = self._execution.simulate_sell(last_trades, tokens_left)
            exit_time = trades[-1].timestamp if trades else pos.entry_time + 60
            self._portfolio.close_position(mint, fill, exit_time, "backtest_end")

    def _build_result(self, elapsed_wall: float) -> BacktestResult:
        """Build summary result from portfolio data."""
        trades = self._portfolio.closed_trades
        wins = [t for t in trades if t.pnl_sol > 0]
        losses = [t for t in trades if t.pnl_sol <= 0]

        total_pnl = sum(t.pnl_sol for t in trades)
        gross_profit = sum(t.pnl_sol for t in wins)
        gross_loss = abs(sum(t.pnl_sol for t in losses))

        return BacktestResult(
            tokens_seen=self._tokens_seen,
            tokens_with_trades=self._tokens_with_trades,
            fast_buys_attempted=self._fast_buys_attempted,
            full_buys_attempted=self._full_buys_attempted,
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / max(len(trades), 1) * 100,
            total_pnl_sol=total_pnl,
            gross_profit_sol=gross_profit,
            gross_loss_sol=gross_loss,
            profit_factor=gross_profit / max(gross_loss, 0.0001),
            avg_win_sol=gross_profit / max(len(wins), 1),
            avg_loss_sol=gross_loss / max(len(losses), 1),
            avg_win_pct=sum(t.pnl_pct for t in wins) / max(len(wins), 1),
            avg_loss_pct=sum(t.pnl_pct for t in losses) / max(len(losses), 1),
            max_drawdown_pct=self._portfolio.max_drawdown_pct,
            final_balance_sol=self._portfolio.balance,
            initial_balance_sol=self._cfg.portfolio_initial_sol,
            roi_pct=(
                (self._portfolio.balance - self._cfg.portfolio_initial_sol)
                / self._cfg.portfolio_initial_sol
            )
            * 100,
            avg_hold_seconds=sum(t.hold_seconds for t in trades) / max(len(trades), 1),
            exit_reasons={
                r: sum(1 for t in trades if t.exit_reason == r)
                for r in set(t.exit_reason for t in trades)
            },
            entry_types={
                "fast": sum(1 for t in trades if t.entry_type == "fast"),
                "full": sum(1 for t in trades if t.entry_type == "full"),
            },
            elapsed_wall_seconds=elapsed_wall,
            closed_trades=trades,
        )


@dataclass
class BacktestResult:
    """Summary of a backtest run."""

    tokens_seen: int
    tokens_with_trades: int
    fast_buys_attempted: int
    full_buys_attempted: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_sol: float
    gross_profit_sol: float
    gross_loss_sol: float
    profit_factor: float
    avg_win_sol: float
    avg_loss_sol: float
    avg_win_pct: float
    avg_loss_pct: float
    max_drawdown_pct: float
    final_balance_sol: float
    initial_balance_sol: float
    roi_pct: float
    avg_hold_seconds: float
    exit_reasons: dict[str, int]
    entry_types: dict[str, int]
    elapsed_wall_seconds: float
    closed_trades: list[ClosedTrade]

    def print_report(self) -> None:
        """Log formatted report to console."""
        logger.info("\n%s", self.format_report())

    def format_report(self) -> str:
        """Build a formatted backtest report."""
        lines = [
            "=" * 60,
            "  BACKTEST REPORT",
            "=" * 60,
            f"  Tokens seen:       {self.tokens_seen}",
            f"  Tokens w/ trades:  {self.tokens_with_trades}",
            f"  Fast buys tried:   {self.fast_buys_attempted}",
            f"  Full buys tried:   {self.full_buys_attempted}",
            f"  Total trades:      {self.total_trades}",
            "-" * 60,
            f"  Wins:              {self.wins}",
            f"  Losses:            {self.losses}",
            f"  Win rate:          {self.win_rate:.1f}%",
            f"  Avg win:           +{self.avg_win_pct:.1f}%  ({self.avg_win_sol:.4f} SOL)",
            f"  Avg loss:          -{self.avg_loss_pct:.1f}%  ({self.avg_loss_sol:.4f} SOL)",
            "-" * 60,
            f"  Total P&L:         {self.total_pnl_sol:+.4f} SOL",
            f"  Profit factor:     {self.profit_factor:.2f}",
            f"  Max drawdown:      {self.max_drawdown_pct:.1f}%",
            f"  ROI:               {self.roi_pct:+.1f}%",
            f"  Initial balance:   {self.initial_balance_sol:.4f} SOL",
            f"  Final balance:     {self.final_balance_sol:.4f} SOL",
            f"  Avg hold time:     {self.avg_hold_seconds:.0f}s",
            "-" * 60,
            "  Entry types:",
        ]
        lines.extend(
            f"    {etype}: {count}" for etype, count in self.entry_types.items()
        )
        lines.append("  Exit reasons:")
        lines.extend(
            f"    {reason}: {count}"
            for reason, count in sorted(
                self.exit_reasons.items(), key=lambda item: -item[1]
            )
        )
        lines.extend(
            [
                "-" * 60,
                f"  Wall time:         {self.elapsed_wall_seconds:.1f}s",
                "=" * 60,
            ]
        )

        by_pnl = sorted(self.closed_trades, key=lambda t: t.pnl_pct, reverse=True)
        lines.append("")
        lines.append("  TOP 5 WINNERS:")
        for t in by_pnl[:5]:
            lines.append(
                f"    {t.symbol:14s} {t.pnl_pct:+7.1f}%  {t.pnl_sol:+.4f} SOL  hold={t.hold_seconds:.0f}s  entry={t.entry_type}  exit={t.exit_reason}"
            )
        lines.append("")
        lines.append("  TOP 5 LOSERS:")
        for t in by_pnl[-5:]:
            lines.append(
                f"    {t.symbol:14s} {t.pnl_pct:+7.1f}%  {t.pnl_sol:+.4f} SOL  hold={t.hold_seconds:.0f}s  entry={t.entry_type}  exit={t.exit_reason}"
            )
        return "\n".join(lines)
