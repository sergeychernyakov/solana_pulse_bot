# pulse_bot/core.py
"""Shared core logic for live, backtest, and optimizer.

ALL entry/exit decisions go through these functions.
Pipeline, BacktestEngine, and Optimizer are just orchestrators.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pulse_bot.config import PUMPFUN_FEE_PCT, PUMPFUN_PRIORITY_FEE
from pulse_bot.pulse.exit_manager import ExitManager
from pulse_bot.pulse.monitor import PulseMonitor

if TYPE_CHECKING:
    from pulse_bot.config import PulseBotConfig
    from pulse_bot.filters.fast import FastResult
    from pulse_bot.models import ScoringResult, Trade

logger = logging.getLogger(__name__)


# ── Shared PnL helper ─────────────────────────────────────────


def calc_pnl_pct(
    entry_price: float,
    exit_price: float,
    buy_amount_sol: float,
    num_sell_legs: int = 1,
) -> float:
    """Fee-adjusted P&L for a single round-trip leg (no slippage).

    Applies Pump.fun percentage fees on both entry and exit plus a flat
    priority fee per transaction (1 buy + ``num_sell_legs`` sells).
    Shared by PaperTradeRunner AND db.close_paper_trade so live, replay,
    optimizer, and persisted live PnL all use the identical formula.

    For the ML training label we use ``calc_realized_pnl_pct`` instead,
    which additionally applies slippage — matching live execution math.
    """
    if entry_price <= 0:
        return 0.0
    effective_entry = entry_price * (1 + PUMPFUN_FEE_PCT)
    effective_exit = exit_price * (1 - PUMPFUN_FEE_PCT)
    raw_pnl = ((effective_exit - effective_entry) / effective_entry) * 100
    if buy_amount_sol > 0:
        priority_cost_pct = (
            (1 + num_sell_legs) * PUMPFUN_PRIORITY_FEE / buy_amount_sol
        ) * 100
    else:
        priority_cost_pct = 0.0
    return raw_pnl - priority_cost_pct


def calc_realized_pnl_pct(
    entry_price: float,
    exit_price: float,
    buy_amount_sol: float,
    buy_slip_pct: float,
    sell_slip_pct: float,
    num_sell_legs: int = 1,
) -> float:
    """Full fee + slippage round-trip P&L %. Shared by label builder and
    live leg-PnL so training target matches what live execution realizes.

    Matches PaperTradeRunner._calc_leg_pnl formula exactly.
    """
    if entry_price <= 0:
        return 0.0
    eff_entry = entry_price * (1 + PUMPFUN_FEE_PCT) * (1 + buy_slip_pct)
    eff_exit = exit_price * (1 - PUMPFUN_FEE_PCT) * (1 - sell_slip_pct)
    raw_pnl = ((eff_exit - eff_entry) / eff_entry) * 100
    if buy_amount_sol > 0:
        priority_cost_pct = (
            (1 + num_sell_legs) * PUMPFUN_PRIORITY_FEE / buy_amount_sol
        ) * 100
    else:
        priority_cost_pct = 0.0
    return raw_pnl - priority_cost_pct


# ── Entry Decision ────────────────────────────────────────────


def decide_entry(
    fast_result: FastResult,
    full_result: ScoringResult,
    config: PulseBotConfig,
) -> tuple[bool, str, int, int]:
    """Decide whether to enter a paper trade.

    Returns (should_enter, entry_type, entry_score, entry_buyer_num).
    ``entry_buyer_num`` is drawn from the window that actually triggered the
    entry (fast window for fast entries, full window for full entries), so
    downstream metadata matches the gate that let the trade through.
    Same logic used by Pipeline (live), verify (replay), and Optimizer.
    """
    mode = config.entry_mode
    is_fast = fast_result.decision == "FAST_BUY"
    is_full = full_result.decision == "BUY"

    should_enter = (
        (mode == "fast" and is_fast)
        or (mode == "full" and is_full)
        or (mode == "both" and (is_fast or is_full))
    )
    # Don't enter on fast alone if full explicitly rejected
    if is_fast and not is_full and full_result.total_score < 0:
        should_enter = False

    # Buyer-number gate uses the window that actually triggered the entry:
    # fast entries -> fast buy_count; full entries -> full buy_count.
    # entry_type must reflect config.entry_mode — in "full" runs we must not
    # fall back to the fast window just because fast_result happened to be
    # positive (otherwise buyer-gate + run metadata get contaminated).
    if mode == "full":
        entry_type = "full"
    elif mode == "fast":
        entry_type = "fast"
    else:  # "both" — prefer the richer full signal when available
        entry_type = "full" if is_full else "fast"
    window_buy_count = (
        fast_result.buy_count if entry_type == "fast" else full_result.buy_count
    )
    entry_buyer_num = window_buy_count + 1

    if not (
        should_enter
        and full_result.exit_price > 0
        and entry_buyer_num >= config.min_entry_buyer_number
        and entry_buyer_num <= config.max_entry_buyer_number
    ):
        return False, "", 0, 0

    # Upper cap on fast_score. Data (236 tuned trades): high fast_score
    # correlates with hard_stop — parabolic pumps reverse sharply.
    # getattr guards against lazy-import race: if a running process loaded
    # an older PulseBotConfig class before this field was added, fall back
    # to disabled (1000) instead of crashing the whole sweep.
    max_fast_score = getattr(config, "entry_max_fast_score", 1000)
    if max_fast_score < 1000 and fast_result.score > max_fast_score:
        return False, "", 0, 0

    entry_score = fast_result.score if entry_type == "fast" else full_result.total_score
    return True, entry_type, entry_score, entry_buyer_num


# ── Exit/Monitor Loop ─────────────────────────────────────────


@dataclass
class MonitorResult:
    """Result of monitoring a paper trade.

    ``pnl_pct`` is the final weighted P&L across all partial fills (if any)
    plus the remaining position sold at ``exit_price`` — already fee-adjusted.
    """

    exit_price: float
    exit_reason: str
    pnl_pct: float
    total_buys: int
    total_sells: int


class PaperTradeRunner:
    """Shared exit/monitor logic. Used by Pipeline and Optimizer identically.

    Process trades one at a time. Returns MonitorResult when exit triggered.
    Supports partial exits: ``sell_partial`` signals are accumulated as
    realized fills and the runner keeps monitoring the remaining position.
    """

    def __init__(
        self,
        config: PulseBotConfig,
        entry_price: float,
        entry_ml_proba: float | None = None,
    ) -> None:
        self._config = config
        self._entry_price = entry_price
        self._current_price = entry_price
        self._pulse = PulseMonitor(config)
        # Load exit ML advisor. None if model missing — pipeline then
        # runs rules-only unchanged. ``entry_ml_proba`` carries the
        # Entry model's verdict through the Position lifecycle (E1
        # cross-model signal) and is injected into every exit call.
        from pulse_bot.ml.policy import load_exit_policy_if_available

        self._exit_mgr = ExitManager(
            config,
            ml_advisor=load_exit_policy_if_available(),
            entry_ml_proba=entry_ml_proba,
        )
        self._total_buys = 0
        self._total_sells = 0
        self._last_trade_ts = 0.0
        self._partial_fills: list[tuple[float, float]] = []  # (fraction, price)
        self._remaining: float = 1.0
        # Execution slippage: buy fills slightly above observed price, sells
        # slightly below (with sell_slippage_mult). Live pipeline uses the
        # same model via SimulatedExecution; optimizer/replay previously
        # ignored these knobs and overstated mainnet expectancy.
        self._buy_slip = max(config.execution_base_slippage, 0.0)
        self._sell_slip = min(
            self._buy_slip * max(config.execution_sell_slippage_mult, 0.0),
            max(config.execution_max_slippage, 0.0),
        )

    @property
    def current_price(self) -> float:
        return self._current_price

    @property
    def total_buys(self) -> int:
        return self._total_buys

    @property
    def total_sells(self) -> int:
        return self._total_sells

    def process_trade(self, trade: Trade, entry_time: float) -> MonitorResult | None:
        """Process one trade. Returns MonitorResult if should exit, None to hold.

        ``entry_time`` is the wall-clock (or virtual, for replay) timestamp at
        which this paper position was opened. Used by the exit manager to
        compute how long the position has been held.

        Same logic as Pipeline._paper_trade loop — hard stop, pulse, exit manager.
        """
        # Update counters
        if trade.tx_type == "buy":
            self._total_buys += 1
        else:
            self._total_sells += 1

        # Update price
        if trade.token_amount > 0 and trade.sol_amount > 0:
            self._current_price = trade.sol_amount / trade.token_amount

        # Hard stop loss — check on EVERY trade, don't wait for snapshot.
        # Stop is evaluated on the CURRENT leg (remaining position at current
        # price) so realized partial profits don't mask a tanking residual.
        current_leg_pnl = self._calc_leg_pnl(self._current_price)
        if current_leg_pnl < -self._config.exit_hard_stop_loss_pct:
            stop_price = self._entry_price * (
                1 - self._config.exit_hard_stop_loss_pct / 100
            )
            return MonitorResult(
                exit_price=stop_price,
                exit_reason="hard_stop",
                pnl_pct=self._weighted_pnl(stop_price),
                total_buys=self._total_buys,
                total_sells=self._total_sells,
            )

        # Pulse monitor → exit manager (same code as live)
        snapshot = self._pulse.update(trade)
        if not snapshot:
            self._last_trade_ts = trade.timestamp
            return None

        elapsed = max(trade.timestamp - entry_time, 0.0)
        signal = self._exit_mgr.decide(snapshot, current_leg_pnl, elapsed)

        if signal.action == "sell_partial":
            # Record realized fill at current price, keep monitoring the
            # remaining position. ExitManager tracks its own _remaining_pct
            # for gating further partials; we mirror it here for PnL math.
            sell_frac = min(signal.sell_pct, self._remaining)
            if sell_frac > 0:
                self._partial_fills.append((sell_frac, self._current_price))
                self._remaining = max(self._remaining - sell_frac, 0.0)
            self._last_trade_ts = trade.timestamp
            return None

        if signal.action == "sell_all":
            return MonitorResult(
                exit_price=self._current_price,
                exit_reason=signal.reason,
                pnl_pct=self._weighted_pnl(self._current_price),
                total_buys=self._total_buys,
                total_sells=self._total_sells,
            )

        self._last_trade_ts = trade.timestamp
        return None

    def timeout_result(self) -> MonitorResult:
        """Build result for timeout/dead_token exit."""
        return MonitorResult(
            exit_price=self._current_price,
            exit_reason="timeout",
            pnl_pct=self._weighted_pnl(self._current_price),
            total_buys=self._total_buys,
            total_sells=self._total_sells,
        )

    def _calc_pnl(self) -> float:
        """Weighted P&L at current price — exposed for callers that expose
        intermediate PnL (e.g. for UI/live snapshots)."""
        return self._weighted_pnl(self._current_price)

    def _calc_leg_pnl(self, price: float) -> float:
        """P&L on a single entry→exit leg at ``price``, fee- and slippage-adjusted.

        Used for hard-stop checks and ExitManager input where we care only
        about the health of the remaining open position, not realized
        partial profits.
        """
        if self._entry_price <= 0:
            return 0.0
        eff_entry = self._entry_price * (1 + PUMPFUN_FEE_PCT) * (1 + self._buy_slip)
        eff_exit = price * (1 - PUMPFUN_FEE_PCT) * (1 - self._sell_slip)
        raw_pnl = ((eff_exit - eff_entry) / eff_entry) * 100
        buy_amount = self._config.buy_amount_sol
        priority_cost_pct = (
            (2 * PUMPFUN_PRIORITY_FEE / buy_amount) * 100 if buy_amount > 0 else 0.0
        )
        return raw_pnl - priority_cost_pct

    def _weighted_pnl(self, final_price: float) -> float:
        """Aggregate P&L across partial fills + remaining at ``final_price``.

        Each fill contributes ``fraction * leg_pnl`` where ``leg_pnl`` applies
        entry/exit percentage fees and the configured slippage model.
        Priority fees scale with the total number of transactions
        (1 buy + N sells).
        """
        if self._entry_price <= 0:
            return 0.0
        legs: list[tuple[float, float]] = list(self._partial_fills)
        if self._remaining > 0:
            legs.append((self._remaining, final_price))
        if not legs:
            return 0.0
        total = 0.0
        eff_entry = self._entry_price * (1 + PUMPFUN_FEE_PCT) * (1 + self._buy_slip)
        for frac, price in legs:
            eff_exit = price * (1 - PUMPFUN_FEE_PCT) * (1 - self._sell_slip)
            leg_pnl = ((eff_exit - eff_entry) / eff_entry) * 100
            total += frac * leg_pnl
        buy_amount = self._config.buy_amount_sol
        if buy_amount > 0:
            priority_cost_pct = (
                (1 + len(legs)) * PUMPFUN_PRIORITY_FEE / buy_amount
            ) * 100
            total -= priority_cost_pct
        return total
