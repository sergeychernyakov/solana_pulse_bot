# pulse_bot/launchpads/geyser.py
"""Yellowstone gRPC adapter — direct stream from local Solana validator.

Subscribes to all transactions touching the pump.fun program via Yellowstone
Geyser plugin gRPC. Decodes Token-create and Buy/Sell events from raw protobuf
``TransactionStatusMeta`` (no JSON parsing — much lower latency than Helius/PumpPortal).

Requires:
* Local Agave validator with Yellowstone Geyser plugin loaded
  (``--geyser-plugin-config <yaml>`` pointing to ``yellowstone-grpc-geyser.so``).
* gRPC port reachable (default ``127.0.0.1:10000``).

Same ``Launchpad`` interface as ``PumpFunLaunchpad``; intended primary source
in the multiplexer (``MultiplexerLaunchpad``) with PumpPortal as fallback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import base58
import grpc

from pulse_bot.config import PUMPFUN_GRADUATION_SOL, PulseBotConfig
from pulse_bot.launchpads.base import Launchpad
from pulse_bot.launchpads.yellowstone_proto import geyser_pb2 as pb
from pulse_bot.launchpads.yellowstone_proto import geyser_pb2_grpc as gpb
from pulse_bot.models import Token, Trade

logger = logging.getLogger(__name__)

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL_MINT = "So11111111111111111111111111111111111111112"


class GeyserLaunchpad(Launchpad):
    """Pump.fun source via local Yellowstone gRPC."""

    name = "geyser"

    def __init__(self, config: PulseBotConfig) -> None:
        self._config = config
        self.ws_url = config.pulse_geyser_endpoint  # reuse base attr name
        self._endpoint = config.pulse_geyser_endpoint
        self._x_token = config.pulse_geyser_x_token or None
        self._channel: grpc.aio.Channel | None = None
        self._stub: gpb.GeyserStub | None = None
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._create_queue: asyncio.Queue[Token] = asyncio.Queue()
        self._trade_queues: dict[str, asyncio.Queue[Trade]] = {}
        self._token_creators: dict[str, str] = {}
        self._running = False
        self._last_event_ts: float = 0.0
        self._graduation_sol = PUMPFUN_GRADUATION_SOL

    # ── Health ──────────────────────────────────────────────────

    @property
    def last_event_ts(self) -> float:
        """Wall-clock of the most recent event seen on the gRPC stream.

        Multiplexer reads this to decide whether to fail over to PumpPortal.
        """
        return self._last_event_ts

    # ── Lifecycle ──────────────────────────────────────────────

    async def connect(self) -> None:
        if self._x_token:
            credentials = grpc.composite_channel_credentials(
                grpc.local_channel_credentials(),
                grpc.access_token_call_credentials(self._x_token),
            )
            self._channel = grpc.aio.secure_channel(self._endpoint, credentials)
        else:
            self._channel = grpc.aio.insecure_channel(self._endpoint)
        self._stub = gpb.GeyserStub(self._channel)
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("Geyser gRPC connected to %s", self._endpoint)

    async def disconnect(self) -> None:
        self._running = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._channel:
            await self._channel.close()
            self._channel = None
        self._stub = None

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
        if mint not in self._trade_queues:
            self._trade_queues[mint] = asyncio.Queue()

    async def unsubscribe_trades(self, mint: str) -> None:
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
            if inactivity_timeout > 0:
                idle = time.time() - last_trade_time
                if idle >= inactivity_timeout:
                    break
            try:
                trade = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, 2.0)
                )
                last_trade_time = time.time()
                yield trade
            except asyncio.TimeoutError:
                continue

    # ── Parsing (Launchpad interface) ──────────────────────────

    def parse_create_event(self, raw: dict) -> Token:
        return Token(
            mint=raw.get("mint", ""),
            name=raw.get("name", ""),
            symbol=raw.get("symbol", ""),
            creator=raw.get("creator", ""),
            created_at=raw.get("timestamp", time.time()),
            uri=raw.get("uri", ""),
            launchpad="pumpfun",
        )

    def parse_trade_event(self, raw: dict, creator: str) -> Trade:
        wallet = raw.get("wallet", "")
        return Trade(
            mint=raw.get("mint", ""),
            wallet=wallet,
            tx_type=raw.get("tx_type", "buy"),
            sol_amount=float(raw.get("sol_amount", 0.0)),
            token_amount=float(raw.get("token_amount", 0.0)),
            new_token_balance=0.0,
            bonding_curve_key="",
            v_sol_in_bonding_curve=0.0,
            v_tokens_in_bonding_curve=0.0,
            market_cap_sol=0.0,
            timestamp=raw.get("timestamp", time.time()),
            is_creator=(wallet == creator),
            signature=str(raw.get("signature", "")),
        )

    def compute_curve_progress(self, v_sol_in_bonding_curve: float) -> float:
        if self._graduation_sol <= 0:
            return 0.0
        return min((v_sol_in_bonding_curve / self._graduation_sol) * 100.0, 100.0)

    # ── Reader loop ────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._subscribe_and_consume()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "Geyser stream error (%s: %s); reconnecting in %.1fs",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _subscribe_and_consume(self) -> None:
        if not self._stub:
            raise RuntimeError("Geyser stub not initialized")

        request = pb.SubscribeRequest(
            transactions={
                "pumpfun": pb.SubscribeRequestFilterTransactions(
                    vote=False,
                    failed=False,
                    account_required=[PUMPFUN_PROGRAM_ID],
                )
            },
            commitment=pb.PROCESSED,
        )

        async def request_iterator() -> AsyncIterator[pb.SubscribeRequest]:
            yield request
            while self._running:
                await asyncio.sleep(30)
                yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1))

        call = self._stub.Subscribe(request_iterator())
        async for update in call:
            if not self._running:
                break
            if update.HasField("transaction"):
                self._last_event_ts = time.time()
                # update.transaction is SubscribeUpdateTransaction (wrapper);
                # its .transaction is SubscribeUpdateTransactionInfo (the real payload).
                info = update.transaction.transaction
                await self._handle_transaction(info, update.created_at)

    async def _handle_transaction(self, info: Any, created_at: Any) -> None:
        """Decode pump.fun create / buy / sell from a Yellowstone tx info."""
        try:
            ev = _decode_pumpfun_event(info)
        except Exception as exc:
            logger.debug("decode failed: %s", exc)
            return
        if ev is None:
            return

        timestamp = (
            created_at.seconds + created_at.nanos / 1e9
            if created_at and created_at.seconds
            else time.time()
        )
        ev["timestamp"] = timestamp

        if ev["kind"] == "create":
            token = self.parse_create_event(ev)
            await self._create_queue.put(token)
        else:  # buy / sell
            mint = ev.get("mint", "")
            queue = self._trade_queues.get(mint)
            if queue is not None:
                creator = self._token_creators.get(mint, "")
                trade = self.parse_trade_event(ev, creator)
                await queue.put(trade)


# ── Pure protobuf decoder ──────────────────────────────────────


def _decode_pumpfun_event(info: Any) -> dict | None:
    """Best-effort decode a pump.fun event from a Yellowstone TransactionInfo.

    Argument is ``SubscribeUpdateTransactionInfo`` (with ``signature``,
    ``transaction``, ``meta`` fields). Returns ``None`` if not a pump.fun
    trade we can interpret.

    Detection via log messages:
    * ``Program log: Instruction: Create`` → token create.
    * ``Program log: Instruction: Buy``    → buy.
    * ``Program log: Instruction: Sell``   → sell.

    Mint extracted from ``meta.post_token_balances`` (non-WSOL token).
    SOL amount = max non-signer lamport delta.
    Token amount = max ui-amount delta for the target mint.
    """
    tx = info.transaction
    meta = info.meta
    if meta.err.err:
        return None

    logs = list(meta.log_messages)
    is_create = any("Instruction: Create" in m for m in logs)
    is_buy = any("Instruction: Buy" in m for m in logs)
    is_sell = any("Instruction: Sell" in m for m in logs)
    if not (is_create or is_buy or is_sell):
        return None

    # Account keys: tx.message.account_keys is repeated bytes (each 32-byte pubkey).
    msg = tx.message
    if not msg.account_keys:
        return None
    signer = base58.b58encode(msg.account_keys[0]).decode("ascii")

    # Identify target mint from token balance entries.
    mint = ""
    for tb in list(meta.post_token_balances) + list(meta.pre_token_balances):
        if tb.mint and tb.mint != WSOL_MINT:
            mint = tb.mint
            break

    if not mint:
        return None

    # Common fields
    sig_bytes = bytes(info.signature)
    signature = base58.b58encode(sig_bytes).decode("ascii") if sig_bytes else ""

    if is_create:
        return {
            "kind": "create",
            "mint": mint,
            "creator": signer,
            "signature": signature,
        }

    # buy / sell: compute amounts from balance deltas
    pre_balances = list(meta.pre_balances)
    post_balances = list(meta.post_balances)
    biggest_delta = 0
    for i in range(1, min(len(pre_balances), len(post_balances))):
        delta = abs(post_balances[i] - pre_balances[i])
        if delta > biggest_delta:
            biggest_delta = delta
    sol_amount = biggest_delta / 1e9

    pre_for_mint = {
        tb.account_index: tb for tb in meta.pre_token_balances if tb.mint == mint
    }
    post_for_mint = {
        tb.account_index: tb for tb in meta.post_token_balances if tb.mint == mint
    }
    token_amount = 0.0
    for idx in set(pre_for_mint) | set(post_for_mint):
        pre_amt = float(
            (pre_for_mint.get(idx).ui_token_amount.ui_amount if idx in pre_for_mint else 0)
        )
        post_amt = float(
            (post_for_mint.get(idx).ui_token_amount.ui_amount if idx in post_for_mint else 0)
        )
        change = abs(post_amt - pre_amt)
        if change > token_amount:
            token_amount = change

    if sol_amount <= 0 or token_amount <= 0:
        return None

    return {
        "kind": "buy" if is_buy else "sell",
        "tx_type": "buy" if is_buy else "sell",
        "mint": mint,
        "wallet": signer,
        "sol_amount": sol_amount,
        "token_amount": token_amount,
        "signature": signature,
    }
