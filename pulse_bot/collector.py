# pulse_bot/collector.py
"""Helius collector — downloads historical trades for tokens from Helius Enhanced API."""

from __future__ import annotations

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

HELIUS_API_URL = "https://api-mainnet.helius-rpc.com/v0/addresses"


class HeliusCollector:
    """Downloads transaction data from Helius Enhanced Transactions API."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("HELIUS_API_KEY", "")
        if not self._api_key:
            raise ValueError("HELIUS_API_KEY not set")

    async def get_trades_for_mint(
        self,
        mint: str,
        limit: int = 100,
    ) -> list[dict]:
        """Download SWAP transactions for a token mint from Helius.

        Returns list of parsed trades: {wallet, tx_type, sol_amount, token_amount, timestamp, ...}
        """
        url = f"{HELIUS_API_URL}/{mint}/transactions/"
        params: dict[str, str] = {
            "api-key": self._api_key,
            "limit": str(min(limit, 100)),
            "type": "SWAP",
        }

        trades: list[dict] = []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Helius API error %d for %s", resp.status, mint[:12])
                    return []
                data = await resp.json()

        for tx in data:
            trade = self._parse_transaction(tx, mint)
            if trade:
                trades.append(trade)

        trades.sort(key=lambda t: t["timestamp"])
        return trades

    def _parse_transaction(self, tx: dict, mint: str) -> dict | None:
        """Parse a Helius Enhanced Transaction into a trade dict."""
        if tx.get("source") != "PUMP_FUN":
            return None

        fee_payer = tx.get("feePayer", "")
        timestamp = tx.get("timestamp", 0)

        # Find SOL balance change for fee payer → determines buy/sell
        sol_change = 0.0
        for ad in tx.get("accountData", []):
            if ad["account"] == fee_payer:
                sol_change = ad.get("nativeBalanceChange", 0) / 1e9
                break

        if sol_change == 0:
            return None

        # Buy = spent SOL (negative change), Sell = received SOL (positive change)
        tx_type = "sell" if sol_change > 0 else "buy"
        sol_amount = abs(sol_change)

        # Find token amount
        token_amount = 0.0
        for tt in tx.get("tokenTransfers", []):
            if tt.get("mint") == mint:
                token_amount = tt.get("tokenAmount", 0)
                break

        if token_amount <= 0:
            return None

        return {
            "mint": mint,
            "wallet": fee_payer,
            "tx_type": tx_type,
            "sol_amount": sol_amount,
            "token_amount": token_amount,
            "timestamp": timestamp,
            "signature": tx.get("signature", ""),
        }

    async def download_trades_for_mints(
        self,
        mints: list[str],
        db_path: str,
    ) -> int:
        """Download trades for multiple mints and insert into DB.

        Returns total trades downloaded.
        """
        import psycopg2

        from pulse_bot.db import _resolve_dsn

        conn = psycopg2.connect(_resolve_dsn(db_path))
        total = 0

        for i, mint in enumerate(mints):
            trades = await self.get_trades_for_mint(mint)
            with conn.cursor() as cur:
                for t in trades:
                    cur.execute(
                        """INSERT INTO trades
                        (mint, wallet, tx_type, sol_amount, token_amount,
                         market_cap_sol, v_sol_in_bonding_curve, timestamp, is_creator)
                        VALUES (%s, %s, %s, %s, %s, 0, 0, %s, 0)
                        ON CONFLICT DO NOTHING""",
                        (
                            t["mint"],
                            t["wallet"],
                            t["tx_type"],
                            t["sol_amount"],
                            t["token_amount"],
                            t["timestamp"],
                        ),
                    )
            conn.commit()
            total += len(trades)

            if (i + 1) % 10 == 0:
                logger.info(
                    "Downloaded %d/%d mints, %d trades total", i + 1, len(mints), total
                )

            # Rate limit: Helius free tier
            await _sleep(0.1)

        conn.close()
        return total


async def _sleep(seconds: float) -> None:
    """Async sleep."""
    import asyncio

    await asyncio.sleep(seconds)
