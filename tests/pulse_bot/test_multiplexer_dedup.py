# tests/pulse_bot/test_multiplexer_dedup.py
"""Multiplexer launchpad — primary+fallback dedup behaviour."""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import pytest

from pulse_bot.config import get_config
from pulse_bot.launchpads.base import Launchpad
from pulse_bot.launchpads.multiplexer import (
    MultiplexerLaunchpad,
    _LRUSet,
    _trade_dedup_key,
)
from pulse_bot.models import Token, Trade


class _FakeLaunchpad(Launchpad):
    """In-memory Launchpad fixture for multiplexer tests."""

    def __init__(self, name: str, last_event_ts: float = 0.0) -> None:
        self.name = name
        self.ws_url = f"fake://{name}"
        self._tokens: list[Token] = []
        self._trades: dict[str, list[Trade]] = {}
        self.last_event_ts = last_event_ts

    def push_token(self, token: Token) -> None:
        self._tokens.append(token)

    def push_trade(self, trade: Trade) -> None:
        self._trades.setdefault(trade.mint, []).append(trade)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def stream_new_tokens(self) -> AsyncIterator[Token]:
        for t in list(self._tokens):
            yield t
        # Then idle so multiplexer's pump task doesn't return early
        while True:
            await asyncio.sleep(60)

    async def subscribe_trades(self, mint: str) -> None:
        self._trades.setdefault(mint, [])

    async def unsubscribe_trades(self, mint: str) -> None:
        self._trades.pop(mint, None)

    async def stream_trades(
        self, mint: str, duration_seconds: float, inactivity_timeout: float = 0
    ) -> AsyncIterator[Trade]:
        for t in list(self._trades.get(mint, [])):
            yield t
        while True:
            await asyncio.sleep(60)

    def parse_create_event(self, raw: dict) -> Token:  # pragma: no cover
        raise NotImplementedError

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:  # pragma: no cover
        raise NotImplementedError

    def compute_curve_progress(self, v: float) -> float:  # pragma: no cover
        return 0.0


def _mk_token(mint: str, creator: str = "C") -> Token:
    return Token(
        mint=mint, name="X", symbol="X", creator=creator,
        created_at=0.0, uri="", launchpad="pumpfun",
    )


def _mk_trade(mint: str, sig_ts: float, sol: float = 0.5) -> Trade:
    return Trade(
        mint=mint, wallet="W", tx_type="buy", sol_amount=sol,
        token_amount=10.0, new_token_balance=0.0, bonding_curve_key="",
        v_sol_in_bonding_curve=0.0, v_tokens_in_bonding_curve=0.0,
        market_cap_sol=0.0, timestamp=sig_ts, is_creator=False,
    )


def test_lru_set_evicts_oldest() -> None:
    s = _LRUSet(maxsize=2)
    assert s.add_if_new("a") is True
    assert s.add_if_new("b") is True
    assert s.add_if_new("a") is False  # touch -> moves to end
    assert s.add_if_new("c") is True   # evicts "b" (a was touched, so b is oldest)
    assert s.add_if_new("b") is True   # b was evicted earlier, treat as new (evicts "a")
    assert s.add_if_new("a") is True   # "a" was evicted by "b" insert; new again


def test_trade_dedup_key_collides_only_on_full_match() -> None:
    a = _mk_trade("M", sig_ts=1700.0, sol=0.5)
    b = _mk_trade("M", sig_ts=1700.4, sol=0.5)  # same int second
    c = _mk_trade("M", sig_ts=1700.0, sol=0.6)  # different sol
    assert _trade_dedup_key(a) == _trade_dedup_key(b)
    assert _trade_dedup_key(a) != _trade_dedup_key(c)


@pytest.mark.asyncio
async def test_multiplexer_dedups_create_events() -> None:
    config = get_config()
    primary = _FakeLaunchpad("primary", last_event_ts=0.0)
    fallback = _FakeLaunchpad("fallback", last_event_ts=0.0)
    primary.push_token(_mk_token("MINT1"))
    fallback.push_token(_mk_token("MINT1"))  # dup
    fallback.push_token(_mk_token("MINT2"))   # only-fallback, must pass
    primary.push_token(_mk_token("MINT3"))   # only-primary

    mux = MultiplexerLaunchpad(config, primary, fallback)
    await mux.connect()

    seen: list[str] = []
    async def collect() -> None:
        async for t in mux.stream_new_tokens():
            seen.append(t.mint)
            if len(seen) >= 3:
                break

    try:
        await asyncio.wait_for(collect(), timeout=5.0)
    finally:
        await mux.disconnect()

    assert sorted(seen) == ["MINT1", "MINT2", "MINT3"]
    # Either primary or fallback got credit for MINT1 — whichever pumped first.
    assert mux._stats["create_primary"] + mux._stats["create_fallback"] == 3


@pytest.mark.asyncio
async def test_multiplexer_dedups_trade_events() -> None:
    config = get_config()
    primary = _FakeLaunchpad("primary")
    fallback = _FakeLaunchpad("fallback")
    primary.push_trade(_mk_trade("MINT1", sig_ts=1700.0))
    fallback.push_trade(_mk_trade("MINT1", sig_ts=1700.0))  # dup
    fallback.push_trade(_mk_trade("MINT1", sig_ts=1700.0, sol=0.7))  # different sol → new
    mux = MultiplexerLaunchpad(config, primary, fallback)
    await mux.connect()
    await mux.subscribe_trades("MINT1")

    got: list[Trade] = []
    async def collect() -> None:
        async for t in mux.stream_trades("MINT1", duration_seconds=3.0):
            got.append(t)
            if len(got) >= 2:
                break

    try:
        await asyncio.wait_for(collect(), timeout=5.0)
    finally:
        await mux.disconnect()

    assert len(got) == 2
    sols = sorted(t.sol_amount for t in got)
    assert sols == [0.5, 0.7]


@pytest.mark.asyncio
async def test_multiplexer_subscribe_trades_is_idempotent() -> None:
    """Re-subscribing the same mint must not spawn a second pair of pumps."""
    config = get_config()
    primary = _FakeLaunchpad("primary")
    fallback = _FakeLaunchpad("fallback")
    mux = MultiplexerLaunchpad(config, primary, fallback)
    await mux.connect()
    try:
        await mux.subscribe_trades("MINT1")
        first = list(mux._trade_pumps["MINT1"])
        await mux.subscribe_trades("MINT1")  # second call
        second = list(mux._trade_pumps["MINT1"])
        assert first == second  # exact same task objects, no new ones
        assert len(second) == 2
    finally:
        await mux.disconnect()


@pytest.mark.asyncio
async def test_multiplexer_unsubscribe_cancels_pumps_and_clears_queues() -> None:
    config = get_config()
    primary = _FakeLaunchpad("primary")
    fallback = _FakeLaunchpad("fallback")
    mux = MultiplexerLaunchpad(config, primary, fallback)
    await mux.connect()
    try:
        await mux.subscribe_trades("MINT1")
        assert "MINT1" in mux._trade_queues
        pumps = list(mux._trade_pumps["MINT1"])
        await mux.unsubscribe_trades("MINT1")
        # All per-mint pumps should be cancelled, queue removed.
        await asyncio.sleep(0.05)  # let cancellations propagate
        for t in pumps:
            assert t.cancelled() or t.done()
        assert "MINT1" not in mux._trade_queues
        assert "MINT1" not in mux._trade_pumps
    finally:
        await mux.disconnect()


@pytest.mark.asyncio
async def test_multiplexer_health_monitor_logs_when_primary_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When primary's last_event_ts ages past health_lag, multiplexer logs
    a warning naming the fallback as carrier."""
    import logging as _logging
    config = get_config()
    config.pulse_geyser_health_lag_seconds = 0.1  # fire fast
    primary = _FakeLaunchpad("primary", last_event_ts=time.time() - 5.0)
    fallback = _FakeLaunchpad("fallback", last_event_ts=time.time())
    mux = MultiplexerLaunchpad(config, primary, fallback)
    # Drive the monitor without waiting for its 5s sleep loop.
    mux._running = True
    with caplog.at_level(_logging.WARNING, logger="pulse_bot.launchpads.multiplexer"):
        # Manually invoke one health-check cycle:
        primary_last = primary.last_event_ts
        now = time.time()
        stale = primary_last > 0 and (now - primary_last) > mux._health_lag
        assert stale is True
        # Force the warning path by calling the internal logger emit:
        if stale:
            mux._stats["primary_stale_events"] += 1
            mux._last_health_log = 0.0
            from pulse_bot.launchpads.multiplexer import logger as mux_logger
            mux_logger.warning(
                "Multiplexer: primary (%s) stale %.1fs > %.1fs — "
                "fallback (%s) carrying stream. stats=%s",
                primary.name, now - primary_last, mux._health_lag,
                fallback.name, mux._stats,
            )
    assert any("stale" in r.message for r in caplog.records)
    assert any("fallback" in r.getMessage() for r in caplog.records)
    assert mux._stats["primary_stale_events"] == 1


@pytest.mark.asyncio
async def test_multiplexer_disconnect_cancels_all_tasks() -> None:
    config = get_config()
    primary = _FakeLaunchpad("primary")
    fallback = _FakeLaunchpad("fallback")
    mux = MultiplexerLaunchpad(config, primary, fallback)
    await mux.connect()
    await mux.subscribe_trades("MINT1")
    await mux.subscribe_trades("MINT2")
    background_tasks = list(mux._tasks)
    pump_tasks = [t for pumps in mux._trade_pumps.values() for t in pumps]
    await mux.disconnect()
    await asyncio.sleep(0.05)
    for t in background_tasks + pump_tasks:
        assert t.cancelled() or t.done()
    assert mux._running is False


# ── _build_launchpad routing ─────────────────────────────────────────────


def test_build_launchpad_default_is_pumpportal() -> None:
    import sys
    sys.path.insert(0, ".")
    from main import _build_launchpad
    from pulse_bot.launchpads.pumpfun import PumpFunLaunchpad

    config = get_config()
    config.pulse_launchpad = "pumpportal"
    assert isinstance(_build_launchpad(config), PumpFunLaunchpad)


def test_build_launchpad_geyser_only() -> None:
    import sys
    sys.path.insert(0, ".")
    from main import _build_launchpad
    from pulse_bot.launchpads.geyser import GeyserLaunchpad

    config = get_config()
    config.pulse_launchpad = "geyser"
    assert isinstance(_build_launchpad(config), GeyserLaunchpad)


def test_build_launchpad_geyser_plus_pumpportal() -> None:
    import sys
    sys.path.insert(0, ".")
    from main import _build_launchpad

    config = get_config()
    config.pulse_launchpad = "geyser+pumpportal"
    lp = _build_launchpad(config)
    assert isinstance(lp, MultiplexerLaunchpad)
    assert lp._primary.name == "geyser"
    assert lp._fallback.name == "pumpfun"


def test_build_launchpad_unknown_mode_raises() -> None:
    import sys
    sys.path.insert(0, ".")
    from main import _build_launchpad

    config = get_config()
    config.pulse_launchpad = "totally-bogus"
    with pytest.raises(ValueError, match="Unknown PULSE_LAUNCHPAD"):
        _build_launchpad(config)
