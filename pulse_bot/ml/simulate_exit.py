# pulse_bot/ml/simulate_exit.py
"""Option B — pure-function exit simulation shared by live + training.

Before this module, ``build_entry_dataset`` labeled rows with a fixed
(TP=+50%, SL=-30%, horizon=300s) policy while the live bot used a totally
different exit logic (TP=+100%, SL=-15%, max_hold=90s, trailing stop,
inactivity, sell-pressure rules). The classifier learned patterns the bot
could never capitalize on — a silent train/serve skew.

``simulate_exit`` is the single source of truth for "given an entry and a
stream of post-entry trades, what would the current live exit logic
produce?". It wraps :class:`PaperTradeRunner` (which already encapsulates
pulse monitor + exit manager + fee/slippage math) in a synchronous
function with no I/O.

Callers:
* ``build_dataset.build_entry_dataset`` — label = 1 iff
  ``simulate_exit(...).pnl_pct > 0``.
* Live ``Pipeline._paper_trade`` — uses ``PaperTradeRunner`` directly
  (async stream), but the per-trade logic is identical.

Parity: any change to ``ExitManager`` / ``PulseMonitor`` / ``PaperTradeRunner``
automatically shows up in future rebuilt datasets. Re-training on stale
labels would silently reintroduce skew — hence the schema bump to v15 when
this function first ships.
"""

from __future__ import annotations

import logging
from typing import Iterable

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import MonitorResult, PaperTradeRunner
from pulse_bot.models import Trade

logger = logging.getLogger(__name__)


def simulate_exit(
    config: PulseBotConfig,
    trades: Iterable[Trade],
    entry_ts: float,
    entry_price: float,
) -> MonitorResult:
    """Replay exit decisions over ``trades`` and return the terminal state.

    Args:
        config: Live config — PaperTradeRunner pulls all thresholds from
            here, so ``exit_hard_stop_loss_pct``, ``exit_take_profit_pct``,
            ``exit_max_hold_seconds``, ``exit_inactivity_seconds``,
            ``exit_trailing_stop_*``, and every soft-exit knob is honored.
        trades: Post-entry trade stream in timestamp-ascending order. Must
            include both buys and sells from the token's bonding curve —
            the pulse monitor needs the full stream to compute sell_rate,
            buy_rate, new_wallet_rate, etc.
        entry_ts: Timestamp of position open. Used by ExitManager to
            compute ``elapsed_sec`` for max_hold.
        entry_price: Entry price in SOL per token (same ``market_cap_sol /
            total_supply`` units ExitManager + PaperTradeRunner use).

    Returns:
        ``MonitorResult`` with ``exit_price``, ``exit_reason``,
        ``pnl_pct`` (fee+slippage adjusted, partial-exit-weighted). When
        ``trades`` is exhausted without a hard exit, returns the
        ``timeout_result()`` (last observed price, reason="timeout").

    Inactivity handling: live path uses ``stream_trades(..., inactivity_timeout=X)``
    which terminates the iterator when no trade arrives for X seconds.
    We replicate that here by checking ``trade.timestamp - last_ts`` — if
    a gap exceeds ``config.exit_inactivity_seconds``, we stop iterating
    and return ``timeout_result``. Zero/negative ``exit_inactivity_seconds``
    disables the check (matching live semantics).
    """
    runner = PaperTradeRunner(config, entry_price)
    last_ts = entry_ts
    inactivity = float(getattr(config, "exit_inactivity_seconds", 0.0) or 0.0)
    for trade in trades:
        if inactivity > 0.0 and (trade.timestamp - last_ts) > inactivity:
            # No trades for N seconds → dead. Live stream_trades would
            # have bailed here; return the timeout_result snapshot.
            return runner.timeout_result()
        result = runner.process_trade(trade, entry_ts)
        if result is not None:
            return result
        last_ts = trade.timestamp
    # Trades exhausted (outside the configured windows). Return the
    # terminal state PaperTradeRunner knows — same as live when
    # exit_max_hold_seconds expires.
    return runner.timeout_result()
