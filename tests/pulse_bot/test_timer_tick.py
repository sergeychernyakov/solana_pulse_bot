# tests/pulse_bot/test_timer_tick.py
"""Phase 4A timer-tick infrastructure tests.

The Phase 4A refactor adds a periodic ExitManager re-evaluation while a
paper trade is open, so a token going silent can close on
``pulse_dead`` / ``no_new_blood`` instead of being held until
``inactivity_timeout``. Tests below cover:

  * ``PulseMonitor.update_empty_tick`` invariants — same snapshot shape,
    no trend mutation, min-events guard preserved.
  * ``PaperTradeRunner.tick`` fires ``ExitManager`` from existing window
    state without a fresh trade.
  * ``Pipeline._paper_trade`` runs the tick task in parallel with the
    trade-stream task and closes on whichever path triggers first; the
    two paths cannot double-close.
  * Disabling the tick (``PULSE_TICK_SECONDS=0`` / config attr 0)
    restores pre-Phase-4A behaviour exactly.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import pytest

from pulse_bot.config import PulseBotConfig
from pulse_bot.core import PaperTradeRunner
from pulse_bot.models import Trade
from pulse_bot.pulse.monitor import PulseMonitor

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides: object) -> PulseBotConfig:
    """Baseline config with most exits disabled. Tests opt-in to specific
    rules per case so the tick behaviour can be observed in isolation."""
    cfg = PulseBotConfig()
    cfg.exit_take_profit_pct = 10_000.0
    cfg.exit_hard_stop_loss_pct = 99.0
    cfg.exit_trailing_stop_enabled = False
    cfg.exit_on_creator_dump = False
    cfg.exit_on_whale = False
    cfg.exit_inactivity_seconds = 0.0
    cfg.exit_no_new_wallets_events = 9_999
    cfg.exit_trend_dying_count = 9_999
    cfg.exit_sell_pressure_ratio = 9_999.0
    cfg.exit_peak_buy_rate_drop_ratio = 0.0
    cfg.exit_max_hold_seconds = 9_999.0
    cfg.pulse_min_events = 3
    cfg.pulse_window_size = 20
    cfg.pulse_dead_buy_rate = -1.0  # disabled by default
    cfg.buy_amount_sol = 0.1
    cfg.execution_base_slippage = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _mk_trade(
    *,
    ts: float,
    tx_type: str = "buy",
    wallet: str | None = None,
    price: float = 1e-7,
) -> Trade:
    sol = 0.1
    token_amount = sol / price if price > 0 else 1.0
    return Trade(
        mint="M",
        wallet=wallet or f"W{int(ts * 1000)}",
        tx_type=tx_type,
        sol_amount=sol,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=30.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=30.0,
        timestamp=ts,
        is_creator=False,
    )


# ---------------------------------------------------------------------------
# PulseMonitor.update_empty_tick
# ---------------------------------------------------------------------------


def test_empty_tick_returns_none_below_min_events() -> None:
    cfg = _cfg(pulse_min_events=5)
    mon = PulseMonitor(cfg)
    mon.update(_mk_trade(ts=1.0))
    mon.update(_mk_trade(ts=2.0))
    # Only 2 trades, min_events=5 → no snapshot.
    assert mon.update_empty_tick(now=10.0) is None


def test_empty_tick_returns_snapshot_when_window_warm() -> None:
    cfg = _cfg(pulse_min_events=3)
    mon = PulseMonitor(cfg)
    for i in range(4):
        mon.update(_mk_trade(ts=float(i)))
    snap = mon.update_empty_tick(now=10.0)
    assert snap is not None
    assert snap.window_events == 4
    assert snap.buy_rate == pytest.approx(1.0)


def test_empty_tick_does_not_advance_trend_counter() -> None:
    """An empty tick must NOT increment ``trend_declining_count`` —
    otherwise repeated ticks would silently force a ``trend_dying`` exit."""
    cfg = _cfg(pulse_min_events=3, pulse_trend_threshold=0.05)
    mon = PulseMonitor(cfg)
    # Seed window with declining buy_rate so a real ``update`` sets
    # _trend_declining_count to 1.
    for i in range(3):
        mon.update(_mk_trade(ts=float(i), tx_type="buy"))
    mon.update(_mk_trade(ts=3.0, tx_type="sell"))  # buy_rate drops
    mon.update(_mk_trade(ts=4.0, tx_type="sell"))  # declining again
    before = mon._trend_declining_count
    # Multiple empty ticks must keep the counter pinned.
    for _ in range(5):
        mon.update_empty_tick(now=100.0)
    assert mon._trend_declining_count == before


# ---------------------------------------------------------------------------
# PaperTradeRunner.tick
# ---------------------------------------------------------------------------


def test_runner_tick_returns_none_when_pulse_healthy() -> None:
    cfg = _cfg()
    runner = PaperTradeRunner(cfg, entry_price=1e-7)
    # Seed window with healthy buys so no hard rule fires.
    for i in range(5):
        runner._pulse.update(_mk_trade(ts=float(i)))
    result = runner.tick(now=10.0, entry_time=0.0)
    assert result is None


def test_runner_tick_fires_pulse_dead_without_new_trade() -> None:
    """Tick must escalate to ``pulse_dead`` once the existing window
    drops below the dead-rate floor — this is the regression Phase 4A
    is built to fix (previously the bot waited for inactivity_timeout)."""
    cfg = _cfg(pulse_dead_buy_rate=0.5, pulse_min_events=3)
    runner = PaperTradeRunner(cfg, entry_price=1e-7)
    # 5 sells + 1 buy → buy_rate = 1/6 < 0.5 = dead.
    runner._pulse.update(_mk_trade(ts=0.0, tx_type="buy"))
    for i in range(1, 6):
        runner._pulse.update(_mk_trade(ts=float(i), tx_type="sell"))
    result = runner.tick(now=30.0, entry_time=0.0)
    assert result is not None
    assert result.exit_reason == "pulse_dead"


def test_runner_tick_disabled_when_window_cold() -> None:
    cfg = _cfg(pulse_min_events=10, pulse_dead_buy_rate=0.5)
    runner = PaperTradeRunner(cfg, entry_price=1e-7)
    # Only 3 trades — well below min_events=10.
    for i in range(3):
        runner._pulse.update(_mk_trade(ts=float(i), tx_type="sell"))
    assert runner.tick(now=30.0, entry_time=0.0) is None


# ---------------------------------------------------------------------------
# Pipeline._paper_trade timer-tick integration
# ---------------------------------------------------------------------------


class _FakeLaunchpad:
    """Minimal launchpad that yields scripted trades or stalls forever."""

    name = "test"

    def __init__(self, trades: list[Trade] | None = None) -> None:
        self._trades = trades or []
        self.subscribed: set[str] = set()
        self.unsubscribed: list[str] = []

    async def subscribe_trades(self, mint: str) -> None:
        self.subscribed.add(mint)

    async def unsubscribe_trades(self, mint: str) -> None:
        self.unsubscribed.append(mint)

    async def stream_trades(
        self,
        mint: str,
        duration_seconds: float,
        inactivity_timeout: float = 0,
    ) -> AsyncIterator[Trade]:
        for t in self._trades:
            await asyncio.sleep(0)  # cooperative yield
            yield t
        # After scripted trades, idle until cancelled or duration elapses.
        # Keep the duration small so timeout-path tests don't hang.
        try:
            await asyncio.sleep(max(duration_seconds, 0.0))
        except asyncio.CancelledError:
            return


class _FakeDB:
    """Captures calls so tests can assert on close path / no double-close."""

    def __init__(self) -> None:
        self.opened: int = 0
        self.closed: list[dict] = []
        self.updates: int = 0
        self.live_price_updates: int = 0

    async def open_paper_trade(self, fields: dict) -> int:
        self.opened += 1
        return 42

    def get_realized_balance_sync(
        self, initial_sol: float, config_id: str | None = None
    ) -> float:
        # No closed trades in these timer-tick tests — dynamic sizing
        # falls back to start-of-portfolio balance.
        return float(initial_sol)

    async def close_paper_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        total_trades: int,
        market_cap_sol: float,
        entry_price: float,
        buy_amount_sol: float,
        *,
        exit_time: float | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        self.closed.append(
            {
                "trade_id": trade_id,
                "exit_reason": exit_reason,
                "exit_price": exit_price,
                "exit_time": exit_time,
                "pnl_pct": pnl_pct,
            }
        )

    async def update_paper_trade(self, *args: object, **kwargs: object) -> None:
        self.updates += 1

    async def update_live_price(self, *args: object, **kwargs: object) -> None:
        self.live_price_updates += 1

    async def insert_trades_batch(self, trades: list[Trade]) -> list[int]:
        return [0 for _ in trades]

    async def upsert_wallet_activity_from_trades(self, trades: list[Trade]) -> None:
        return None


def _make_pipeline(cfg: PulseBotConfig, launchpad: _FakeLaunchpad, db: _FakeDB):
    """Build a ``Pipeline`` instance bypassing __init__ (avoids loading
    ML models, holder clients, semaphores). We only need ``_paper_trade``
    plus its dependencies."""
    from pulse_bot.pipeline import Pipeline

    pipe = Pipeline.__new__(Pipeline)
    pipe._config = cfg
    pipe._db = db
    pipe._launchpad = launchpad
    # Multi-config refactor: ``_open_slots`` is now a property aliasing
    # the LIVE config's entry in ``_open_slots_by_config``. A Pipeline
    # built via ``__new__`` must seed the backing dict + registry before
    # the property setter is usable.
    pipe._config_registry = None  # single-config mode → _live_config_id="LIVE"
    pipe._open_slots_by_config = {}
    pipe._open_slots = 1  # pretend a slot was reserved at entry
    return pipe


def _token() -> object:
    class _T:
        mint = "Mtest"
        symbol = "TST"
        creator = "C"
        created_at = 0.0

    return _T()


def test_pipeline_tick_fires_pulse_dead_without_trade(monkeypatch) -> None:
    """End-to-end-ish: no fresh trades arrive but the existing window
    already represents a dead pulse → tick task must close the trade."""
    cfg = _cfg(
        pulse_dead_buy_rate=0.5,
        pulse_min_events=3,
        exit_max_hold_seconds=5.0,  # short fallback for test runtime
        exit_inactivity_seconds=0.0,
    )
    monkeypatch.setenv("PULSE_TICK_SECONDS", "0.05")

    # Pre-load runner pulse window via a custom factory: we wrap
    # PaperTradeRunner.__init__ to seed the dead-pulse window before
    # _paper_trade enters its loops.
    import pulse_bot.core as core_mod

    orig_runner_cls = core_mod.PaperTradeRunner

    class _SeededRunner(orig_runner_cls):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._pulse.update(_mk_trade(ts=0.0, tx_type="buy"))
            for i in range(1, 6):
                self._pulse.update(_mk_trade(ts=float(i), tx_type="sell"))

    monkeypatch.setattr(core_mod, "PaperTradeRunner", _SeededRunner)

    launchpad = _FakeLaunchpad(trades=[])
    db = _FakeDB()
    pipe = _make_pipeline(cfg, launchpad, db)

    async def _run() -> None:
        await pipe._paper_trade(
            token=_token(),
            entry_price=1e-7,
            entry_mcap=30.0,
            entry_buyer_num=5,
            entry_type="full",
            entry_score=50,
            entry_ts=time.time(),
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=3.0))

    # Exactly one close, and it must come from the tick path with the
    # ``pulse_dead`` reason rather than ``timeout`` / ``dead_token``.
    assert len(db.closed) == 1
    assert db.closed[0]["exit_reason"] == "pulse_dead"


def test_pipeline_tick_disabled_falls_back_to_inactivity(monkeypatch) -> None:
    """Regression guard: setting ``PULSE_TICK_SECONDS=0`` must restore the
    pre-Phase-4A behaviour — tick task is a no-op and the trade closes
    via the original timeout/dead_token path only."""
    cfg = _cfg(
        pulse_dead_buy_rate=0.5,
        pulse_min_events=3,
        exit_max_hold_seconds=0.05,  # very short so fake stream ends fast
        exit_inactivity_seconds=0.0,
    )
    monkeypatch.setenv("PULSE_TICK_SECONDS", "0")

    # Same dead-pulse seed as previous test — but tick is disabled, so
    # the close reason must NOT be ``pulse_dead``; it has to be the
    # post-stream fallback (``timeout`` here, since inactivity=0).
    import pulse_bot.core as core_mod

    orig_runner_cls = core_mod.PaperTradeRunner

    class _SeededRunner(orig_runner_cls):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._pulse.update(_mk_trade(ts=0.0, tx_type="buy"))
            for i in range(1, 6):
                self._pulse.update(_mk_trade(ts=float(i), tx_type="sell"))

    monkeypatch.setattr(core_mod, "PaperTradeRunner", _SeededRunner)

    launchpad = _FakeLaunchpad(trades=[])
    db = _FakeDB()
    pipe = _make_pipeline(cfg, launchpad, db)

    async def _run() -> None:
        await pipe._paper_trade(
            token=_token(),
            entry_price=1e-7,
            entry_mcap=30.0,
            entry_buyer_num=5,
            entry_type="full",
            entry_score=50,
            entry_ts=time.time(),
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=3.0))

    assert len(db.closed) == 1
    # With tick disabled, the close must come from the post-loop path,
    # which is ``timeout`` (inactivity_seconds=0).
    assert db.closed[0]["exit_reason"] == "timeout"


def test_pipeline_tick_and_trade_no_double_close(monkeypatch) -> None:
    """Tick + trade-stream both wired up: the trade-stream path closes
    the position first; the tick task must not produce a second close."""
    cfg = _cfg(
        # Keep all rules permissive — the close fires via runner.process_trade
        # only when the take_profit threshold is breached.
        exit_take_profit_pct=1.0,
        exit_take_profit_enabled=True,
        exit_hard_stop_loss_pct=99.0,
        exit_max_hold_seconds=5.0,
        exit_inactivity_seconds=0.0,
        pulse_min_events=2,
    )
    monkeypatch.setenv("PULSE_TICK_SECONDS", "0.05")

    # process_trade requires a warm pulse window to fire take_profit; feed
    # ≥pulse_min_events trades, the last one at a 5× price to trigger TP.
    base_ts = time.time()
    trades = [
        _mk_trade(ts=base_ts + 0.01, tx_type="buy", price=1e-7),
        _mk_trade(ts=base_ts + 0.02, tx_type="buy", price=1e-7),
        _mk_trade(ts=base_ts + 0.03, tx_type="buy", price=5e-7),
    ]
    launchpad = _FakeLaunchpad(trades=trades)
    db = _FakeDB()
    pipe = _make_pipeline(cfg, launchpad, db)

    async def _run() -> None:
        await pipe._paper_trade(
            token=_token(),
            entry_price=1e-7,
            entry_mcap=30.0,
            entry_buyer_num=5,
            entry_type="full",
            entry_score=50,
            entry_ts=time.time(),
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=3.0))

    # Exactly one close — the lock prevents the tick task from racing in.
    assert len(db.closed) == 1
    assert db.closed[0]["exit_reason"] == "take_profit"


def test_pipeline_tick_sanity_fires_multiple_times(monkeypatch) -> None:
    """Sanity: with a short tick interval and an idle stream, the tick
    coroutine must run at least N times before the test-level timeout
    closes the trade. Counts ``runner.tick`` invocations directly to
    avoid flaky wall-clock assertions."""
    cfg = _cfg(
        pulse_min_events=3,
        exit_max_hold_seconds=0.5,  # forces fallback close after ~0.5s
        exit_inactivity_seconds=0.0,
    )
    monkeypatch.setenv("PULSE_TICK_SECONDS", "0.05")

    tick_calls: list[float] = []

    import pulse_bot.core as core_mod

    orig_tick = core_mod.PaperTradeRunner.tick

    def _spy_tick(self, now: float, entry_time: float):
        tick_calls.append(now)
        return orig_tick(self, now, entry_time)

    monkeypatch.setattr(core_mod.PaperTradeRunner, "tick", _spy_tick)

    launchpad = _FakeLaunchpad(trades=[])
    db = _FakeDB()
    pipe = _make_pipeline(cfg, launchpad, db)

    async def _run() -> None:
        await pipe._paper_trade(
            token=_token(),
            entry_price=1e-7,
            entry_mcap=30.0,
            entry_buyer_num=5,
            entry_type="full",
            entry_score=50,
            entry_ts=time.time(),
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=3.0))

    # Sanity: 0.5s window / 0.05s interval ≈ 10 ticks; allow large slack.
    # We only need to verify the loop *did* fire repeatedly, not exact count.
    assert len(tick_calls) >= 3
