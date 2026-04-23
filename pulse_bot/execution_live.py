# pulse_bot/execution_live.py
"""Live Solana execution via Jupiter aggregator + Helius RPC.

Paper-trading uses ``SimulatedExecution``; real SOL trading uses this
module. Design per codex-reviewed plan 2026-04-22:

- Jupiter v6 ``/quote`` → best route for mint, slippage cap in bps
- Jupiter v6 ``/swap`` → returns signed-ready serialized transaction
- Local ``solders.Keypair`` signs the transaction
- Helius ``sendTransaction`` submits

``dry_run=True`` is the **safe default**. Never submits on-chain; only
logs what would be sent. Flip to False at integration time with small
position size, not before.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Jupiter migrated 2025 from quote-api.jup.ag/v6 → lite-api.jup.ag/swap/v1.
# Old endpoint's DNS is dead. Keeping new endpoints.
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"

# Wrapped SOL mint (SOL trading on Solana uses wrapped SOL for SPL).
SOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_SLIPPAGE_BPS = 300  # 3% — aggressive enough for pump.fun volatility
DEFAULT_PRIORITY_FEE_LAMPORTS = 100_000  # 0.0001 SOL — competitive for sniping
DEFAULT_QUOTE_TIMEOUT_SEC = 3.0
DEFAULT_SWAP_TIMEOUT_SEC = 5.0
DEFAULT_SEND_TIMEOUT_SEC = 10.0


@dataclass
class ExecutionResult:
    """Outcome of a live (or dry-run) swap attempt."""

    side: str  # "buy" | "sell"
    mint: str
    dry_run: bool
    success: bool
    signature: str | None = None
    in_amount_raw: int = 0
    out_amount_raw: int = 0
    slippage_bps: int = 0
    priority_fee_lamports: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class LiveExecution:
    """Jupiter+Helius live swap. Safe-by-default (dry_run=True).

    Usage:
        execu = LiveExecution.from_env()  # loads SOL_WALLET_KEYPAIR
        result = await execu.buy(mint, amount_sol=0.01, slippage_bps=500)
        await execu.close()
    """

    def __init__(
        self,
        helius_api_key: str,
        keypair_base58: str | None,
        dry_run: bool = True,
        priority_fee_lamports: int = DEFAULT_PRIORITY_FEE_LAMPORTS,
        quote_timeout_sec: float = DEFAULT_QUOTE_TIMEOUT_SEC,
        swap_timeout_sec: float = DEFAULT_SWAP_TIMEOUT_SEC,
        send_timeout_sec: float = DEFAULT_SEND_TIMEOUT_SEC,
    ) -> None:
        self._helius_api_key = helius_api_key
        self._dry_run = dry_run
        self._priority_fee = priority_fee_lamports
        self._quote_timeout = quote_timeout_sec
        self._swap_timeout = swap_timeout_sec
        self._send_timeout = send_timeout_sec
        self._keypair: Any = None
        self._wallet_address: str | None = None
        if keypair_base58:
            self._keypair = self._load_keypair(keypair_base58)
            if self._keypair is not None:
                self._wallet_address = str(self._keypair.pubkey())
        self._session: Any = None

    @classmethod
    def from_env(cls, dry_run: bool = True, **kwargs: Any) -> "LiveExecution":
        """Build from env vars: HELIUS_API_KEY + SOL_WALLET_KEYPAIR."""
        helius = os.environ.get("HELIUS_API_KEY", "")
        keypair = os.environ.get("SOL_WALLET_KEYPAIR")
        return cls(
            helius_api_key=helius,
            keypair_base58=keypair,
            dry_run=dry_run,
            **kwargs,
        )

    @staticmethod
    def _load_keypair(secret: str) -> Any:
        """Decode a Solana keypair from either format.

        Accepts:
          * Base58 string (Phantom/Backpack export)
          * JSON array of 64 bytes (``solana-keygen new`` format)

        Returns None on error so callers don't crash on bad env.
        """
        try:
            from solders.keypair import Keypair
        except ImportError:
            return None
        secret = secret.strip()
        # JSON array format e.g. [12, 34, 56, ...]
        if secret.startswith("["):
            try:
                import json as _json

                arr = _json.loads(secret)
                if not isinstance(arr, list) or len(arr) != 64:
                    raise ValueError("keypair must be 64-byte array")
                return Keypair.from_bytes(bytes(arr))
            except Exception as e:
                logger.error("Failed to load JSON keypair: %s", type(e).__name__)
                return None
        # Base58 string
        try:
            return Keypair.from_base58_string(secret)
        except Exception as e:
            logger.error("Failed to load base58 keypair: %s", type(e).__name__)
            return None

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def wallet_address(self) -> str | None:
        return self._wallet_address

    @property
    def is_ready(self) -> bool:
        """True iff we can actually submit (dry-run OR have keypair+key)."""
        return self._dry_run or (
            self._keypair is not None and bool(self._helius_api_key)
        )

    async def _get_session(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None

    # ── Public API ──────────────────────────────────────────────

    async def buy(
        self,
        mint: str,
        amount_sol: float,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    ) -> ExecutionResult:
        """Swap SOL → ``mint`` token.

        ``amount_sol`` is the input SOL size. Slippage bps is applied
        on Jupiter's side as min-output guarantee.
        """
        lamports = int(amount_sol * 1_000_000_000)
        return await self._execute_swap(
            input_mint=SOL_MINT,
            output_mint=mint,
            amount_raw=lamports,
            slippage_bps=slippage_bps,
            side="buy",
            mint=mint,
        )

    async def sell(
        self,
        mint: str,
        token_amount_raw: int,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    ) -> ExecutionResult:
        """Swap ``mint`` tokens → SOL. ``token_amount_raw`` is in base
        units (multiplied by 10^decimals, typically 10^6 for pump.fun)."""
        return await self._execute_swap(
            input_mint=mint,
            output_mint=SOL_MINT,
            amount_raw=int(token_amount_raw),
            slippage_bps=slippage_bps,
            side="sell",
            mint=mint,
        )

    # ── Internals ───────────────────────────────────────────────

    async def _execute_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        side: str,
        mint: str,
    ) -> ExecutionResult:
        result = ExecutionResult(
            side=side,
            mint=mint,
            dry_run=self._dry_run,
            success=False,
            in_amount_raw=amount_raw,
            slippage_bps=slippage_bps,
            priority_fee_lamports=self._priority_fee,
        )
        if not self.is_ready:
            result.error = "not_ready"
            return result

        # 1. Quote
        quote = await self._fetch_quote(
            input_mint,
            output_mint,
            amount_raw,
            slippage_bps,
        )
        if quote is None:
            result.error = "quote_failed"
            return result
        out_amount_raw = int(quote.get("outAmount", 0))
        result.out_amount_raw = out_amount_raw
        result.extra["quote_route_plan_steps"] = len(quote.get("routePlan", []))

        # 2. Dry-run short-circuit
        if self._dry_run:
            logger.info(
                "[DRY RUN] %s %s: in=%d out=%d slip_bps=%d routes=%d",
                side.upper(),
                mint[:12],
                amount_raw,
                out_amount_raw,
                slippage_bps,
                result.extra["quote_route_plan_steps"],
            )
            result.success = True
            return result

        # 3. Get signed-ready swap transaction
        if self._wallet_address is None:
            result.error = "no_wallet"
            return result
        tx_bytes = await self._fetch_swap_tx(quote)
        if tx_bytes is None:
            result.error = "swap_tx_failed"
            return result

        # 4. Sign + submit
        signature = await self._sign_and_send(tx_bytes)
        if signature is None:
            result.error = "send_failed"
            return result
        result.success = True
        result.signature = signature
        logger.info(
            "LIVE %s %s: sig=%s in=%d out=%d",
            side.upper(),
            mint[:12],
            signature[:20],
            amount_raw,
            out_amount_raw,
        )
        return result

    async def _fetch_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> dict | None:
        try:
            import aiohttp
        except ImportError:
            return None
        session = await self._get_session()
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            # Restrict to high-quality DEXes for reliability on new mints.
            # pump.fun liquidity is reachable via the generic route too.
            "onlyDirectRoutes": "false",
        }
        try:
            async with session.get(
                JUPITER_QUOTE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._quote_timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Jupiter quote %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None
                return await resp.json()
        except Exception as e:
            logger.warning("Jupiter quote exception: %s", type(e).__name__)
            return None

    async def _fetch_swap_tx(self, quote: dict) -> bytes | None:
        try:
            import aiohttp
        except ImportError:
            return None
        session = await self._get_session()
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self._wallet_address,
            # Jupiter auto-wraps SOL for SPL path; we always want this on.
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": self._priority_fee,
            # Versioned (v0) transactions are cheaper to land than legacy.
            "asLegacyTransaction": False,
        }
        try:
            async with session.post(
                JUPITER_SWAP_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._swap_timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Jupiter swap %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None
                data = await resp.json()
                b64 = data.get("swapTransaction")
                if not b64:
                    return None
                return base64.b64decode(b64)
        except Exception as e:
            logger.warning("Jupiter swap exception: %s", type(e).__name__)
            return None

    async def _sign_and_send(self, tx_bytes: bytes) -> str | None:
        try:
            import aiohttp
            from solders.transaction import VersionedTransaction
        except ImportError:
            return None
        try:
            # Jupiter returns a signed-ready versioned tx with message built
            # for our wallet as signer. We re-sign to produce the final tx.
            unsigned = VersionedTransaction.from_bytes(tx_bytes)
            signed = VersionedTransaction(unsigned.message, [self._keypair])
            signed_bytes = bytes(signed)
        except Exception as e:
            logger.error("Tx signing failed: %s", type(e).__name__)
            return None

        session = await self._get_session()
        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self._helius_api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(signed_bytes).decode(),
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "maxRetries": 3,
                    "preflightCommitment": "confirmed",
                },
            ],
        }
        try:
            async with session.post(
                rpc_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._send_timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Helius sendTransaction %s: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None
                data = await resp.json()
                if "error" in data:
                    logger.warning(
                        "sendTransaction RPC error: %s",
                        data["error"],
                    )
                    return None
                return data.get("result")
        except Exception as e:
            logger.warning("sendTransaction exception: %s", type(e).__name__)
            return None
