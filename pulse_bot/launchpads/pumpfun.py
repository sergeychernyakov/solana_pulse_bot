# pulse_bot/launchpads/pumpfun.py
"""Pump.fun launchpad adapter — WebSocket connection and message parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import websockets
from websockets.exceptions import ConnectionClosed

from pulse_bot.config import (
    PUMPFUN_GRADUATION_SOL,
    PUMPPORTAL_API_KEY,
    PulseBotConfig,
)
from pulse_bot.launchpads.base import Launchpad
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)

_MAX_RECONNECT_ATTEMPTS = 5
_RECONNECT_BASE_DELAY = 2.0


class PumpFunLaunchpad(Launchpad):
    """Pump.fun WebSocket adapter.

    Uses a single WS connection. A background reader task routes incoming
    messages to the appropriate queue by type (create → _create_queue,
    trade → _trade_queues[mint]).
    """

    name = "pumpfun"
    ws_url = "wss://pumpportal.fun/api/data"

    def __init__(self, config: PulseBotConfig) -> None:
        self._config = config
        self._ws: Any | None = None
        self._create_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._trade_queues: dict[str, asyncio.Queue[dict]] = {}
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._running = False
        self._graduation_sol = PUMPFUN_GRADUATION_SOL
        self._token_creators: dict[str, str] = {}
        self._last_msg_ts = 0.0

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to WS, subscribe to new tokens, start background reader.

        Retries the initial handshake up to 5× with exponential backoff so
        a transient pump.fun API blip (or local CPU starvation from a
        concurrent sweep) doesn't kill the bot on startup. Once connected,
        ``_ws_reader_loop`` handles its own mid-session reconnects.
        """
        self._running = True
        attempts = 0
        while attempts < 5:
            try:
                await self._establish_connection()
                break
            except Exception as exc:
                attempts += 1
                delay = min(2.0 * (2 ** (attempts - 1)), 30.0)
                logger.warning(
                    "PumpFun WS connect attempt %d/5 failed (%s); " "retrying in %.1fs",
                    attempts,
                    type(exc).__name__,
                    delay,
                )
                if attempts >= 5:
                    raise
                await asyncio.sleep(delay)
        self._last_msg_ts = time.time()
        self._reader_task = asyncio.create_task(self._ws_reader_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("PumpFun WS connected, reader + watchdog started")

    async def disconnect(self) -> None:
        """Stop reader and close WS."""
        self._running = False
        for task in (self._reader_task, self._watchdog_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._trade_queues.clear()
        logger.info("PumpFun WS disconnected")

    # ── Public API ─────────────────────────────────────────────

    async def stream_new_tokens(self) -> AsyncIterator[Token]:
        """Yield Token objects as they arrive from create events."""
        while self._running:
            try:
                raw = await asyncio.wait_for(self._create_queue.get(), timeout=5.0)
                token = self.parse_create_event(raw)
                self._token_creators[token.mint] = token.creator
                yield token
            except asyncio.TimeoutError:
                continue

    async def subscribe_trades(self, mint: str) -> None:
        """Subscribe to trade events for a specific mint."""
        if mint not in self._trade_queues:
            self._trade_queues[mint] = asyncio.Queue()
        if self._ws:
            msg = json.dumps({"method": "subscribeTokenTrade", "keys": [mint]})
            try:
                await self._ws.send(msg)
                logger.debug("Subscribed to trades for %s", mint[:12])
            except ConnectionClosed:
                logger.warning("WS closed when subscribing trades for %s", mint[:12])

    async def unsubscribe_trades(self, mint: str) -> None:
        """Unsubscribe and remove the trade queue for a mint."""
        if self._ws:
            msg = json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]})
            try:
                await self._ws.send(msg)
            except ConnectionClosed:
                pass
        self._trade_queues.pop(mint, None)
        self._token_creators.pop(mint, None)

    async def stream_trades(
        self,
        mint: str,
        duration_seconds: float,
        inactivity_timeout: float = 0,
    ) -> AsyncIterator[Trade]:
        """Yield trades for a mint during the observation window."""
        queue = self._trade_queues.get(mint)
        if not queue:
            return

        deadline = time.time() + duration_seconds
        creator = self._token_creators.get(mint, "")
        last_trade_time = time.time()

        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Check inactivity
            if inactivity_timeout > 0:
                idle = time.time() - last_trade_time
                if idle >= inactivity_timeout:
                    break
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=min(remaining, 2.0))
                trade = self.parse_trade_event(raw, creator)
                last_trade_time = time.time()
                yield trade
            except asyncio.TimeoutError:
                continue

    # ── Parsing ────────────────────────────────────────────────

    def parse_create_event(self, raw: dict) -> Token:
        """Parse a raw WS create message into a Token.

        PumpPortal's WebSocket multiplexes multiple launchpads (pump.fun,
        letsbonk.fun, PumpSwap, etc.) on one feed, distinguished by the
        ``pool`` field. Preserve it so downstream datasets can slice by
        launchpad — previously everything was mislabelled "pumpfun".
        """
        pool = raw.get("pool") or "pumpfun"
        # Canonical internal names: "pumpfun", "letsbonk", or the raw pool
        # string for any other (rarer) launchpad.
        launchpad_name = {
            "pump": "pumpfun",
            "bonk": "letsbonk",
        }.get(pool, pool)
        return Token(
            mint=raw.get("mint", ""),
            name=raw.get("name", ""),
            symbol=raw.get("symbol", ""),
            creator=raw.get("traderPublicKey", ""),
            created_at=raw.get("timestamp", time.time()),
            uri=raw.get("uri", ""),
            launchpad=launchpad_name,
        )

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        """Parse a raw WS trade message into a Trade."""
        wallet = raw.get("traderPublicKey", "")
        return Trade(
            mint=raw.get("mint", ""),
            wallet=wallet,
            tx_type=raw.get("txType", "buy"),
            sol_amount=(
                float(raw.get("solAmount", 0)) / 1e9
                if raw.get("solAmount", 0) > 1000
                else float(raw.get("solAmount", 0))
            ),
            token_amount=float(raw.get("tokenAmount", 0)),
            new_token_balance=float(raw.get("newTokenBalance", 0)),
            bonding_curve_key=raw.get("bondingCurveKey", ""),
            v_sol_in_bonding_curve=(
                float(raw.get("vSolInBondingCurve", 0)) / 1e9
                if raw.get("vSolInBondingCurve", 0) > 1000
                else float(raw.get("vSolInBondingCurve", 0))
            ),
            v_tokens_in_bonding_curve=float(raw.get("vTokensInBondingCurve", 0)),
            market_cap_sol=(
                float(raw.get("marketCapSol", 0)) / 1e9
                if raw.get("marketCapSol", 0) > 1000
                else float(raw.get("marketCapSol", 0))
            ),
            timestamp=raw.get("timestamp", time.time()),
            is_creator=(wallet == creator),
        )

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        """Bonding curve progress as percentage (0-100)."""
        if self._graduation_sol <= 0:
            return 0.0
        return min((v_sol_in_bonding_curve / self._graduation_sol) * 100.0, 100.0)

    # ── Internal ───────────────────────────────────────────────

    async def _establish_connection(self) -> None:
        """Connect to WS and send initial subscription.

        2026-05-02: ping_timeout 10→60s — at 50%+ CPU + many concurrent
        Helius captures + ML inference + survival ticks, the asyncio
        event loop can't always respond to PumpPortal's ping within 10s.

        2026-05-04: append ?api-key=… when PUMPPORTAL_API_KEY is set —
        without it `subscribeTokenTrade` is silently rejected.
        """
        url = self.ws_url
        if PUMPPORTAL_API_KEY:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api-key={PUMPPORTAL_API_KEY}"
        else:
            logger.warning(
                "PUMPPORTAL_API_KEY not set — `subscribeTokenTrade` will "
                "be silently rejected; trade ingestion will not work"
            )
        self._ws = await websockets.connect(
            url, ping_interval=30, ping_timeout=60
        )
        await self._ws.send(json.dumps({"method": "subscribeNewToken"}))
        logger.info("Subscribed to new tokens on PumpFun")

    async def _ws_reader_loop(self) -> None:
        """Background task: read WS messages and route to queues. Auto-reconnect."""
        reconnect_count = 0

        while self._running:
            try:
                await self._read_messages()
            except ConnectionClosed as exc:
                if not self._running:
                    break
                reconnect_count += 1
                delay = min(_RECONNECT_BASE_DELAY * (2 ** (reconnect_count - 1)), 30.0)
                logger.warning(
                    "WS connection lost (attempt %d): %s. Reconnecting in %.1fs...",
                    reconnect_count,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                try:
                    await self._establish_connection()
                    if self._ws is None:
                        raise ConnectionError(
                            "WebSocket reconnect returned no connection"
                        )
                    # Re-subscribe to all active trade mints
                    for mint in list(self._trade_queues.keys()):
                        sub_msg = json.dumps(
                            {"method": "subscribeTokenTrade", "keys": [mint]}
                        )
                        await self._ws.send(sub_msg)
                    self._last_msg_ts = time.time()
                    reconnect_count = 0
                    logger.info("WS reconnected successfully")
                except Exception as reconn_err:
                    logger.error("Reconnection failed: %s", reconn_err)
                    if reconnect_count >= _MAX_RECONNECT_ATTEMPTS:
                        logger.critical("Max reconnection attempts reached, stopping")
                        self._running = False
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected error in WS reader: %s", exc)
                if not self._running:
                    break
                await asyncio.sleep(1.0)

    async def _read_messages(self) -> None:
        """Read and route messages from the WS connection.

        Unrecognised payloads (anything that isn't a create/buy/sell event)
        are surfaced at WARNING — PumpPortal sends ack/error messages here
        too, and silently dropping them masked the 2026-05-04 auth gating
        bug for 3 days.
        """
        if not self._ws:
            return
        async for raw_msg in self._ws:
            if not self._running:
                break
            self._last_msg_ts = time.time()
            try:
                data = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            tx_type = data.get("txType", "")

            if tx_type == "create":
                await self._create_queue.put(data)
            elif tx_type in ("buy", "sell"):
                mint = data.get("mint", "")
                queue = self._trade_queues.get(mint)
                if queue is not None:
                    await queue.put(data)
            else:
                if "errors" in data or "error" in data or "message" in data:
                    logger.warning("PumpPortal WS response: %s", data)

    async def _watchdog_loop(self) -> None:
        """Force reconnect if WS goes silent for too long.

        With ``ping_interval=None`` we no longer rely on RFC ping/pong
        for liveness. PumpPortal occasionally goes silent (no create
        events, no trade events) for extended periods — observed
        2026-05-04 with bursts of activity right after reconnect then
        180s+ of total silence. Threshold is conservative (5 min) so
        this only fires for genuinely-dead connections, not slow
        memecoin hours.
        """
        SILENCE_LIMIT = 300.0
        while self._running:
            await asyncio.sleep(30.0)
            if not self._running or self._ws is None:
                continue
            if self._last_msg_ts == 0.0:
                continue  # not started receiving yet
            silence = time.time() - self._last_msg_ts
            if silence > SILENCE_LIMIT:
                logger.warning(
                    "WS silent for %.0fs — closing to force reconnect",
                    silence,
                )
                self._last_msg_ts = 0.0
                try:
                    await self._ws.close()
                except Exception:
                    pass
