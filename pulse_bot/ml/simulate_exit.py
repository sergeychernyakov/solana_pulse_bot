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
from typing import Iterable, Mapping

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import MonitorResult, PaperTradeRunner
from pulse_bot.models import Trade

logger = logging.getLogger(__name__)


def _replay_trades(
    config: PulseBotConfig,
    trades: Iterable[Trade],
    entry_ts: float,
    entry_price: float,
    tick_seconds: float = 5.0,
    horizon_seconds: float = 0.0,
) -> MonitorResult:
    """Internal core: replay trades + interleaved tick events.

    Live parity: in production the bot has two concurrent coroutines on
    each open paper trade — ``_trade_stream_loop`` (processes WS trades)
    and ``_tick_loop`` (evaluates exit rules every ``tick_seconds``
    regardless of trade activity). The tick loop is the only thing that
    closes positions when the WS goes quiet — ``runner.tick()`` checks
    ``max_hold`` / survival / inactivity via ``elapsed = now - entry_ts``
    without needing a new trade event.

    Previously this function only replayed trades — for the 76 % of
    pump.fun positions that go silent after ~5 s, the simulator missed
    every tick-driven exit live had taken (max_hold, survival, etc.) and
    instead returned a stale ``timeout_result`` at the last trade. Result
    on a 2509-position sample: live PnL = +0.49 SOL, sim PnL = −16.76 SOL
    on identical entries/config — a pure policy-skew artifact.

    We now walk the timeline: drain ticks until the next trade's
    timestamp, then process the trade, repeat. After trades exhaust we
    keep ticking until ``max_hold + buffer`` (or ``horizon_seconds`` if
    larger) so dead-after-5 s positions exit by max_hold like in live.
    """
    runner = PaperTradeRunner(config, entry_price)
    tick_seconds = max(float(tick_seconds or 0.0), 1.0)
    max_hold = float(getattr(config, "exit_max_hold_seconds", 0.0) or 0.0)
    deadline_ts = entry_ts + max(max_hold + tick_seconds, float(horizon_seconds or 0.0))
    next_tick_ts = entry_ts + tick_seconds

    def _drain_ticks_until(target_ts: float) -> MonitorResult | None:
        nonlocal next_tick_ts
        while next_tick_ts <= target_ts:
            res = runner.tick(next_tick_ts, entry_ts)
            if res is not None:
                return res
            next_tick_ts += tick_seconds
        return None

    for trade in trades:
        # Interpolate ticks up to this trade's timestamp first — gives
        # max_hold / inactivity / survival a chance to fire on quiet
        # gaps, matching the live tick_loop behaviour.
        tick_res = _drain_ticks_until(trade.timestamp)
        if tick_res is not None:
            return tick_res
        result = runner.process_trade(trade, entry_ts)
        if result is not None:
            return result

    # Trades exhausted — continue ticking until deadline. This is what
    # live does between the last WS trade and the eventual max_hold /
    # survival exit. Without this, "dead-after-5s" positions wrongly
    # return timeout_result at last_ts instead of the real exit.
    tick_res = _drain_ticks_until(deadline_ts)
    if tick_res is not None:
        return tick_res
    return runner.timeout_result(hold_seconds=max(deadline_ts - entry_ts, 0.0))


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
        no hard exit fires before ``max_hold``, the terminal
        ``timeout_result()`` is returned.

    Tick parity: live runs a ``_tick_loop`` alongside the trade stream,
    so ``max_hold`` / inactivity / survival can close a position even
    when the WS goes silent. ``_replay_trades`` mirrors that by draining
    ``runner.tick()`` events (every ``tick_seconds``) between trades and
    after the trade stream is exhausted — up to ``max_hold + tick``.
    Without this the ~76 % of pump.fun tokens that die after ~5 s would
    wrongly carry their last observed price to a stale timeout instead
    of the real max-hold exit.
    """
    return _replay_trades(config, trades, entry_ts, entry_price)


def simulate_exit_batch(
    config: PulseBotConfig,
    trades_by_mint: Mapping[str, Iterable[Trade]],
    entries: Mapping[str, tuple[float, float]],
) -> dict[str, MonitorResult]:
    """Vectorized-friendly wrapper: many tokens through one call.

    The heavy cost in the dataset build is **not** the per-trade Python
    loop — it's the per-token round-trip to Postgres (one ``SELECT … FROM
    trades WHERE mint = ?`` per token, ×60k tokens). This function lets
    the caller pre-fetch every relevant trade in a single query, group
    them by mint, and hand the dict in here. Internally we still loop
    over mints (PaperTradeRunner is stateful and inherently serial per
    token) but with zero DB I/O.

    Args:
        config: Same live config used by ``simulate_exit``.
        trades_by_mint: ``{mint: iterable[Trade]}`` — post-entry trades
            in timestamp-ascending order. Mints absent from this map (or
            mapped to an empty iterable) are treated as DOA: the runner
            is constructed at ``entry_price`` and immediately yields
            ``timeout_result()`` (matches the per-token call when given
            no trades).
        entries: ``{mint: (entry_ts, entry_price)}``. Every mint that
            should appear in the result must be a key here.

    Returns:
        ``{mint: MonitorResult}`` for every key in ``entries``.

    Notes:
        - Behavior per mint is **bit-identical** to ``simulate_exit`` —
          this function only changes scheduling, not exit semantics.
        - DOA fast-path: when ``trades_by_mint`` has no trades for a
          mint, we still construct PaperTradeRunner(config, entry_price)
          and call ``timeout_result()`` so ``current_price`` and
          ``_weighted_pnl`` produce the same numbers (zero PnL, since no
          partial fills and current_price == entry_price).
    """
    results: dict[str, MonitorResult] = {}
    for mint, (entry_ts, entry_price) in entries.items():
        trades = trades_by_mint.get(mint) or ()
        if not trades:
            # DOA fast path — no trades to replay. Mirror the empty-iter
            # branch of simulate_exit: construct runner, return timeout.
            runner = PaperTradeRunner(config, entry_price)
            results[mint] = runner.timeout_result()
            continue
        results[mint] = _replay_trades(config, trades, entry_ts, entry_price)
    return results
