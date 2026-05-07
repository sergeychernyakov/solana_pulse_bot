# pulse_bot/launchpads/multiplexer.py
"""Multi-source launchpad with primary + fallback dedup.

Wraps two ``Launchpad`` adapters: a low-latency primary (``GeyserLaunchpad``)
and a remote fallback (``PumpFunLaunchpad`` / PumpPortal). Both stream events
in parallel; this multiplexer deduplicates by ``mint`` for create events and
by ``(mint, signature)`` for trades, emitting whichever source delivers first.

If primary stops emitting events (e.g. local validator falls behind tip,
gRPC plugin crashes), the fallback transparently keeps the bot working.

Same ``Launchpad`` interface — pipeline does not need to change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import AsyncIterator

from pulse_bot.config import PUMPFUN_GRADUATION_SOL, PulseBotConfig
from pulse_bot.launchpads.base import Launchpad
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)


class _LRUSet:
    """Bounded set with insertion-order eviction."""

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def add_if_new(self, key: str) -> bool:
        """Return True if added (new), False if already present."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return True


class MultiplexerLaunchpad(Launchpad):
    """Primary + fallback Launchpad with dedup and health monitoring."""

    name = "multiplexer"

    def __init__(
        self,
        config: PulseBotConfig,
        primary: Launchpad,
        fallback: Launchpad,
    ) -> None:
        self._config = config
        self._primary = primary
        self._fallback = fallback
        self.ws_url = f"primary={primary.name} fallback={fallback.name}"
        # 2026-04-28 (architecture phase I): bounded queues prevent
        # unbounded memory growth on burst traffic. PUMP_FUN can spike
        # to 100+ tokens/sec — without a cap, a slow consumer leaks
        # memory until OOM. ``put_nowait`` would raise QueueFull but
        # the producer-side already wraps in try/except.
        # Per-mint trade queue cap is smaller because most tokens have
        # < 50 trades during their entire life; 200 is a generous
        # safety margin.
        import os as _os_mux
        _create_q_cap = int(_os_mux.environ.get("PULSE_MUX_CREATE_QUEUE_MAX", "5000"))
        self._trade_q_cap = int(_os_mux.environ.get("PULSE_MUX_TRADE_QUEUE_MAX", "200"))
        self._create_queue: asyncio.Queue[Token] = asyncio.Queue(maxsize=_create_q_cap)
        self._trade_queues: dict[str, asyncio.Queue[Trade]] = {}
        self._token_creators: dict[str, str] = {}
        self._mints_seen = _LRUSet(maxsize=20000)
        self._sigs_seen = _LRUSet(maxsize=100000)
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
        self._trade_pumps: dict[str, list[asyncio.Task]] = {}  # type: ignore[type-arg]
        self._running = False
        self._stats = {
            "create_primary": 0,
            "create_fallback": 0,
            "trade_primary": 0,
            "trade_fallback": 0,
            "primary_stale_events": 0,
        }
        self._health_lag = config.pulse_geyser_health_lag_seconds
        self._last_health_log: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        await asyncio.gather(
            self._primary.connect(),
            self._fallback.connect(),
        )
        self._running = True
        self._tasks = [
            asyncio.create_task(self._pump_creates(self._primary, "primary")),
            asyncio.create_task(self._pump_creates(self._fallback, "fallback")),
            asyncio.create_task(self._health_monitor()),
        ]
        logger.info(
            "MultiplexerLaunchpad connected (primary=%s fallback=%s, health_lag=%.1fs)",
            self._primary.name,
            self._fallback.name,
            self._health_lag,
        )

    async def disconnect(self) -> None:
        self._running = False
        all_tasks = list(self._tasks)
        for pumps in self._trade_pumps.values():
            all_tasks.extend(pumps)
        for t in all_tasks:
            if not t.done():
                t.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._tasks.clear()
        self._trade_pumps.clear()
        await asyncio.gather(
            self._primary.disconnect(),
            self._fallback.disconnect(),
            return_exceptions=True,
        )

    # ── Public stream API ──────────────────────────────────────

    async def stream_new_tokens(self) -> AsyncIterator[Token]:
        while self._running:
            try:
                token = await asyncio.wait_for(self._create_queue.get(), timeout=2.0)
                self._token_creators[token.mint] = token.creator
                yield token
            except asyncio.TimeoutError:
                continue

    async def subscribe_trades(self, mint: str) -> None:
        # Idempotent: re-subscribing the same mint must not spawn a second pair
        # of pumps (would double-emit every trade after dedup-LRU eviction).
        if mint in self._trade_pumps:
            return
        # Bounded per-mint queue (codex Phase I, 2026-04-28).
        self._trade_queues[mint] = asyncio.Queue(maxsize=self._trade_q_cap)
        await asyncio.gather(
            self._primary.subscribe_trades(mint),
            self._fallback.subscribe_trades(mint),
            return_exceptions=True,
        )
        pumps = [
            asyncio.create_task(self._pump_trades(self._primary, mint, "primary")),
            asyncio.create_task(self._pump_trades(self._fallback, mint, "fallback")),
        ]
        self._trade_pumps[mint] = pumps

    async def unsubscribe_trades(self, mint: str) -> None:
        for t in self._trade_pumps.pop(mint, []):
            if not t.done():
                t.cancel()
        await asyncio.gather(
            self._primary.unsubscribe_trades(mint),
            self._fallback.unsubscribe_trades(mint),
            return_exceptions=True,
        )
        self._trade_queues.pop(mint, None)
        self._token_creators.pop(mint, None)

    async def stream_trades(
        self,
        mint: str,
        duration_seconds: float,
        inactivity_timeout: float = 0,
    ) -> AsyncIterator[Trade]:
        queue = self._trade_queues.get(mint)
        if not queue:
            return

        deadline = time.time() + duration_seconds
        last_trade_time = time.time()
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if inactivity_timeout > 0 and time.time() - last_trade_time >= inactivity_timeout:
                break
            try:
                trade = await asyncio.wait_for(queue.get(), timeout=min(remaining, 2.0))
                last_trade_time = time.time()
                yield trade
            except asyncio.TimeoutError:
                continue

    # Parsing helpers — multiplexer never invokes these directly; child adapters do.
    def parse_create_event(self, raw: dict) -> Token:  # pragma: no cover
        return self._primary.parse_create_event(raw)

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:  # pragma: no cover
        return self._primary.parse_trade_event(raw, creator)

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        return min((v_sol_in_bonding_curve / PUMPFUN_GRADUATION_SOL) * 100.0, 100.0)

    # ── Internal pumps ─────────────────────────────────────────

    async def _pump_creates(self, source: Launchpad, label: str) -> None:
        try:
            async for token in source.stream_new_tokens():
                if not self._running:
                    break
                if not token.mint:
                    continue
                if not self._mints_seen.add_if_new(token.mint):
                    continue  # already emitted from the other source
                self._stats[f"create_{label}"] += 1
                await self._create_queue.put(token)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("create-pump %s crashed: %s", label, exc)

    async def _pump_trades(self, source: Launchpad, mint: str, label: str) -> None:
        # We tap directly into the child's per-mint queue via stream_trades with
        # an effectively-unlimited duration. When unsubscribe_trades runs upstream
        # the child empties its queue and stream_trades returns; this task ends.
        try:
            async for trade in source.stream_trades(
                mint, duration_seconds=86400.0, inactivity_timeout=0
            ):
                if not self._running:
                    break
                queue = self._trade_queues.get(mint)
                if queue is None:
                    break
                # Cross-source dedup with TWO keys (codex review 2026-04-30):
                # 1. Synthetic key always present (mint+wallet+int_ts+sol+type)
                # 2. Signature key when source supplies it (Geyser path)
                # If EITHER key already in LRU → it's a dupe. Both keys
                # added on first sight so future hits from the OTHER source
                # (which may lack signature) still match via synthetic.
                syn_key, sig_key = _trade_dedup_keys(trade)
                if syn_key in self._sigs_seen._data or (
                    sig_key and sig_key in self._sigs_seen._data
                ):
                    continue
                self._sigs_seen.add_if_new(syn_key)
                if sig_key:
                    self._sigs_seen.add_if_new(sig_key)
                self._stats[f"trade_{label}"] += 1
                await queue.put(trade)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("trade-pump %s/%s ended: %s", label, mint[:8], exc)

    async def _health_monitor(self) -> None:
        """Log when primary goes stale (events older than `health_lag`)."""
        while self._running:
            await asyncio.sleep(5.0)
            primary_last = getattr(self._primary, "last_event_ts", 0.0)
            now = time.time()
            stale = primary_last > 0 and (now - primary_last) > self._health_lag
            if stale:
                self._stats["primary_stale_events"] += 1
                if now - self._last_health_log > 30.0:
                    logger.warning(
                        "Multiplexer: primary (%s) stale %.1fs > %.1fs — "
                        "fallback (%s) carrying stream. stats=%s",
                        self._primary.name,
                        now - primary_last,
                        self._health_lag,
                        self._fallback.name,
                        self._stats,
                    )
                    self._last_health_log = now


def _trade_dedup_keys(trade: Trade) -> tuple[str, str | None]:
    """Two dedup keys per trade: synthetic (always) + signature (if present).

    Both keys are inserted into the LRU on first sight; on later events the
    multiplexer checks for ANY-of-two match against the LRU, so a trade
    that came from Geyser with a signature can still be deduped against a
    later PumpPortal copy of the same trade (which lacks signature).

    Synthetic key uses ``int(timestamp)`` (1-second bucket) — sniper bots
    placing identical-size orders within the same second collide. With
    Geyser as primary source, signature key absorbs those exact-match
    cases; synthetic key handles cross-source deduplication.

    Returns ``(synthetic_key, signature_key_or_None)``.
    """
    syn = (
        f"syn:{trade.mint}:{trade.wallet}:{int(trade.timestamp)}:"
        f"{round(trade.sol_amount, 6)}:{trade.tx_type}"
    )
    sig = f"sig:{trade.signature}" if trade.signature else None
    return syn, sig


def _trade_dedup_key(trade: Trade) -> str:
    """Backward-compat single-key shim — used by tests and callers that
    only care about the synthetic key. New code should use
    :func:`_trade_dedup_keys` for correct cross-source dedup."""
    syn, _ = _trade_dedup_keys(trade)
    return syn
