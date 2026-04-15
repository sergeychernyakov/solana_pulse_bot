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

from pulse_bot.config import PulseBotConfig
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
        self._running = False
        self._graduation_sol = config.pumpfun_graduation_sol
        self._token_creators: dict[str, str] = {}

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to WS, subscribe to new tokens, start background reader."""
        self._running = True
        await self._establish_connection()
        self._reader_task = asyncio.create_task(self._ws_reader_loop())
        logger.info("PumpFun WS connected and reader started")

    async def disconnect(self) -> None:
        """Stop reader and close WS."""
        self._running = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
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
        self, mint: str, duration_seconds: float
    ) -> AsyncIterator[Trade]:
        """Yield trades for a mint during the observation window."""
        queue = self._trade_queues.get(mint)
        if not queue:
            return

        deadline = time.time() + duration_seconds
        creator = self._token_creators.get(mint, "")

        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=min(remaining, 2.0))
                trade = self.parse_trade_event(raw, creator)
                yield trade
            except asyncio.TimeoutError:
                continue

    # ── Parsing ────────────────────────────────────────────────

    def parse_create_event(self, raw: dict) -> Token:
        """Parse a raw WS create message into a Token."""
        return Token(
            mint=raw.get("mint", ""),
            name=raw.get("name", ""),
            symbol=raw.get("symbol", ""),
            creator=raw.get("traderPublicKey", ""),
            created_at=raw.get("timestamp", time.time()),
            uri=raw.get("uri", ""),
            launchpad="pumpfun",
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
        """Connect to WS and send initial subscription."""
        self._ws = await websockets.connect(
            self.ws_url, ping_interval=30, ping_timeout=10
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
        """Read and route messages from the WS connection."""
        if not self._ws:
            return
        async for raw_msg in self._ws:
            if not self._running:
                break
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
