# pulse_bot/backtest.py
"""BacktestEngine — replays historical data through the full pipeline: score → buy → monitor → exit."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from pulse_bot.clock import SimulatedClock
from pulse_bot.execution import SimulatedExecution
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
    from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)


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
        self._metrics_calc = MetricsCalculator(graduation_sol=config.pumpfun_graduation_sol)

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

        for token in self._source.iter_tokens():
            self._tokens_seen += 1
            self._process_token(token)

            if self._tokens_seen % 100 == 0:
                logger.info(
                    "Progress: %d/%d tokens, %d trades, balance=%.4f, positions=%d",
                    self._tokens_seen, token_count, len(self._portfolio.closed_trades),
                    self._portfolio.balance, self._portfolio.open_count,
                )

        # Force close any remaining positions at last known price
        self._close_remaining_positions()

        elapsed = time.time() - start_wall
        logger.info(
            "Backtest done in %.1fs: %d tokens, %d trades, final_balance=%.4f SOL",
            elapsed, self._tokens_seen, len(self._portfolio.closed_trades), self._portfolio.balance,
        )

        return self._build_result(elapsed)

    def _process_token(self, token: Token) -> None:
        """Process one token through the full pipeline."""
        # Get fast phase trades
        fast_trades = self._source.get_fast_trades(
            token.mint, token.created_at, self._cfg.fast_observe_seconds,
        )

        if not fast_trades:
            return

        self._tokens_with_trades += 1

        # ── Phase 1: Fast entry ────────────────────────────
        entered = False
        if self._cfg.entry_mode in ("fast", "both"):
            fast_result = self._fast_filter.evaluate(token, fast_trades)
            if fast_result.decision == "FAST_BUY" and self._portfolio.can_buy:
                self._fast_buys_attempted += 1
                fill = self._execution.simulate_buy(fast_trades)
                entry_time = token.created_at + self._cfg.fast_observe_seconds
                entered = self._portfolio.open_position(token.mint, token.symbol, fill, entry_time, "fast")

        # ── Phase 2: Full scoring ──────────────────────────
        full_trades = self._source.get_full_trades(
            token.mint, token.created_at, self._cfg.observe_seconds,
        )
        if not entered and self._cfg.entry_mode in ("full", "both") and full_trades:
            result = self._scorer.score(token, full_trades)
            if result.decision == "BUY" and self._portfolio.can_buy:
                self._full_buys_attempted += 1
                fill = self._execution.simulate_buy(full_trades)
                entry_time = token.created_at + self._cfg.observe_seconds
                entered = self._portfolio.open_position(token.mint, token.symbol, fill, entry_time, "full")

        # ── Phase 3: Pulse monitoring → exit ───────────────
        if entered:
            self._monitor_and_exit(token)

    def _monitor_and_exit(self, token: Token) -> None:
        """Monitor position with pulse and exit based on rules."""
        pos = self._portfolio.positions.get(token.mint)
        if not pos:
            return

        # Get all trades after entry for pulse monitoring
        monitor_trades = self._source.get_all_trades_after(token.mint, pos.entry_time)

        if not monitor_trades:
            # No trades after entry — timeout exit
            dummy_fill = self._execution.simulate_sell([], pos.tokens_held)
            self._portfolio.close_position(token.mint, dummy_fill, pos.entry_time + 60, "no_data")
            return

        pulse = PulseMonitor(self._cfg)
        exit_mgr = ExitManager(self._cfg)

        for trade in monitor_trades:
            self._clock.advance_to(trade.timestamp)
            snapshot = pulse.update(trade)

            if not snapshot:
                continue

            # Calculate current P&L
            current_price = trade.sol_amount / trade.token_amount if trade.token_amount > 0 else pos.entry_price
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price > 0 else 0
            elapsed = trade.timestamp - pos.entry_time

            signal = exit_mgr.decide(snapshot, pnl_pct, elapsed)

            if signal.action == "sell_partial":
                tokens_to_sell = pos.tokens_held * signal.sell_pct * pos.remaining_pct
                recent = [t for t in monitor_trades if t.timestamp <= trade.timestamp][-20:]
                fill = self._execution.simulate_sell(recent, tokens_to_sell)
                self._portfolio.partial_sell(token.mint, signal.sell_pct, fill, trade.timestamp, signal.reason)

            elif signal.action == "sell_all":
                tokens_to_sell = pos.tokens_held * pos.remaining_pct
                recent = [t for t in monitor_trades if t.timestamp <= trade.timestamp][-20:]
                fill = self._execution.simulate_sell(recent, tokens_to_sell)
                self._portfolio.close_position(token.mint, fill, trade.timestamp, signal.reason)
                return

        # If we get here, never hit an exit signal — close on timeout
        if token.mint in self._portfolio.positions:
            last_trades = monitor_trades[-20:] if monitor_trades else []
            tokens_left = pos.tokens_held * exit_mgr.remaining_pct
            fill = self._execution.simulate_sell(last_trades, tokens_left)
            self._portfolio.close_position(token.mint, fill, monitor_trades[-1].timestamp, "end_of_data")

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
            roi_pct=((self._portfolio.balance - self._cfg.portfolio_initial_sol) / self._cfg.portfolio_initial_sol) * 100,
            avg_hold_seconds=sum(t.hold_seconds for t in trades) / max(len(trades), 1),
            exit_reasons={r: sum(1 for t in trades if t.exit_reason == r) for r in set(t.exit_reason for t in trades)},
            entry_types={"fast": sum(1 for t in trades if t.entry_type == "fast"), "full": sum(1 for t in trades if t.entry_type == "full")},
            elapsed_wall_seconds=elapsed_wall,
            closed_trades=trades,
        )


class BacktestResult:
    """Summary of a backtest run."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        for k, v in kwargs.items():
            setattr(self, k, v)

    def print_report(self) -> None:
        """Print formatted report to console."""
        print("\n" + "=" * 60)
        print("  BACKTEST REPORT")
        print("=" * 60)
        print(f"  Tokens seen:       {self.tokens_seen}")
        print(f"  Tokens w/ trades:  {self.tokens_with_trades}")
        print(f"  Fast buys tried:   {self.fast_buys_attempted}")
        print(f"  Full buys tried:   {self.full_buys_attempted}")
        print(f"  Total trades:      {self.total_trades}")
        print("-" * 60)
        print(f"  Wins:              {self.wins}")
        print(f"  Losses:            {self.losses}")
        print(f"  Win rate:          {self.win_rate:.1f}%")
        print(f"  Avg win:           +{self.avg_win_pct:.1f}%  ({self.avg_win_sol:.4f} SOL)")
        print(f"  Avg loss:          -{self.avg_loss_pct:.1f}%  ({self.avg_loss_sol:.4f} SOL)")
        print("-" * 60)
        print(f"  Total P&L:         {self.total_pnl_sol:+.4f} SOL")
        print(f"  Profit factor:     {self.profit_factor:.2f}")
        print(f"  Max drawdown:      {self.max_drawdown_pct:.1f}%")
        print(f"  ROI:               {self.roi_pct:+.1f}%")
        print(f"  Initial balance:   {self.initial_balance_sol:.4f} SOL")
        print(f"  Final balance:     {self.final_balance_sol:.4f} SOL")
        print(f"  Avg hold time:     {self.avg_hold_seconds:.0f}s")
        print("-" * 60)
        print("  Entry types:")
        for etype, count in self.entry_types.items():
            print(f"    {etype}: {count}")
        print("  Exit reasons:")
        for reason, count in sorted(self.exit_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")
        print("-" * 60)
        print(f"  Wall time:         {self.elapsed_wall_seconds:.1f}s")
        print("=" * 60)

        # Top winners/losers
        by_pnl = sorted(self.closed_trades, key=lambda t: t.pnl_pct, reverse=True)
        print("\n  TOP 5 WINNERS:")
        for t in by_pnl[:5]:
            print(f"    {t.symbol:14s} {t.pnl_pct:+7.1f}%  {t.pnl_sol:+.4f} SOL  hold={t.hold_seconds:.0f}s  entry={t.entry_type}  exit={t.exit_reason}")
        print("\n  TOP 5 LOSERS:")
        for t in by_pnl[-5:]:
            print(f"    {t.symbol:14s} {t.pnl_pct:+7.1f}%  {t.pnl_sol:+.4f} SOL  hold={t.hold_seconds:.0f}s  entry={t.entry_type}  exit={t.exit_reason}")
        print()
