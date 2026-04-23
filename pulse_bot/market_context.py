# pulse_bot/market_context.py
"""SOL/USD price cache — external market context for scoring.

Lightweight async wrapper over CoinGecko's simple-price endpoint
(free tier, 50 calls/min). Single long-lived aiohttp session shared
across pipeline. Value cached for ``refresh_interval_sec`` so scoring
doesn't bombard the API.

Captured to ``token_scores.sol_price_usd`` as a side-column. NOT yet
in ``ENTRY_FEATURE_ORDER`` — same pattern as mint_authority: capture
now, add as feature once we have enough history that training data
distribution matches live distribution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
)
DEFAULT_REFRESH_INTERVAL_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 2.0


class SOLPriceCache:
    """Caches the latest SOL/USD price; fetches on demand when stale."""

    def __init__(
        self,
        refresh_interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._refresh_interval = refresh_interval_sec
        self._timeout = timeout_sec
        self._session: Any = None
        self._cached_price: float | None = None
        self._cached_at: float = 0.0
        # Prevents multiple concurrent fetches when cache expires under load.
        self._fetch_lock = asyncio.Lock()

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

    async def get(self) -> float | None:
        """Return the cached SOL price in USD, or None if fetch failed.

        Returns cached value if less than ``refresh_interval`` old.
        Never raises — returns None on any failure so callers can use
        the feature as "missing when unavailable" without crashing.
        """
        now = time.time()
        if (
            self._cached_price is not None
            and now - self._cached_at < self._refresh_interval
        ):
            return self._cached_price
        async with self._fetch_lock:
            # Re-check after acquiring lock — another coroutine may
            # have refreshed while we waited.
            now = time.time()
            if (
                self._cached_price is not None
                and now - self._cached_at < self._refresh_interval
            ):
                return self._cached_price
            try:
                import aiohttp
            except ImportError:
                return None
            session = await self._get_session()
            try:
                async with session.get(
                    COINGECKO_URL,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status != 200:
                        return self._cached_price  # stale-but-better-than-none
                    data = await resp.json()
                    price = data.get("solana", {}).get("usd")
                    if isinstance(price, (int, float)) and price > 0:
                        self._cached_price = float(price)
                        self._cached_at = time.time()
                        return self._cached_price
                    return self._cached_price
            except Exception:
                logger.debug("SOL price fetch failed", exc_info=True)
                return self._cached_price  # serve stale if we have any
