# pulse_bot/helius_holders.py
"""Token holder concentration via Helius ``getTokenLargestAccounts`` RPC.

Codex v7 audit recommended new on-chain features as **regime signals** —
the v9 sweep showed every grid axis is regime-dependent; tuning existing
axes hit a p-hacking wall. Holder concentration at early token life is a
candidate regime signal:

- Tokens launched with one wallet holding 50%+ are typically bundled /
  rug-prone.
- Organic launches spread across 20+ wallets before graduation.

## Sampling design (Apr 2026, post-codex v8 audit)

**Capture for EVERY token in the WS stream, at THREE timepoints.**

Earlier design flaws (and why they were fixed):

1. *"Fire only on FAST_BUY"*: contaminated the dataset with the bot
   filter's own biases (82% of fast_buys are noise). Fixed: fire for
   every new token at pipeline entry.
2. *"Single timepoint"*: one snapshot can't distinguish "bundled and
   still concentrated" from "bundled then rapidly distributed". Fixed:
   capture at T+3s, T+10s, T+30s — the **delta** is the signal.
3. *"Pre-capture deaths invisible"*: tokens rugged in first 3s would
   never get a row. But they're the strongest extreme-top1 class and
   would silently censor our strongest signal. Fixed: at T+3s if the
   mint has no observed trades since creation, write a **negative row**
   (``is_negative_row=1``) so analysis can still count them.
4. *"Silent timeouts bias toward uncongested slots"*: pump.fun activity
   correlates with Solana congestion which correlates with Helius RPC
   timeouts. Fixed: log every failure to ``holder_capture_failures``
   so we can later measure drop-rate vs pump-rate.
5. *"Unbounded concurrency"*: at 200 concurrent tokens × 30s max delay
   up to 6000 pending tasks. Fixed: semaphore bound of 50 in pipeline.
6. *"Per-call ClientSession"*: each RPC created a fresh connection
   pool. Fixed: single long-lived aiohttp session per HeliusHolderClient.

Ground truth for later analysis = objective outcomes (peak
``market_cap_sol``, graduation, death time) stored in main DB — never
paper_trade PnL (that's the broken filter we're trying to improve).

Live path only; backtest cannot reconstruct point-in-time holders.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp  # noqa: F401 — only for type annotations; real import is lazy

logger = logging.getLogger(__name__)

RPC_URL_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"
DEFAULT_TIMEOUT_SEC = 1.5

# Three capture timepoints (seconds since token's created_at).
# Empirically observed (Apr 2026): Helius indexes pump.fun mints
# somewhere between T+10 (100% parse_error: empty accounts) and T+30
# (100% success). So we start at T+30 where indexing is reliable, and
# use T+60 and T+120 to measure distribution over a 90-second window.
# Delta top1_pct(T+120) − top1_pct(T+30) separates "stuck" (rug
# likely) from "distributed" (organic growth).
CAPTURE_AGE_SECONDS: tuple[float, ...] = (30.0, 60.0, 120.0)


@dataclass
class HolderSnapshot:
    """Top-holder concentration at ``observed_at`` for ``mint``, or a
    negative-row placeholder (is_negative_row=1) marking a token that
    died before the first capture window."""

    mint: str
    observed_at: float
    capture_at_age_sec: float  # 3, 10, 30, or -1 (negative row)
    total_supply_raw: int | None = None
    top1_raw: int | None = None
    top5_raw: int | None = None
    top10_raw: int | None = None
    top1_pct: float | None = None
    top5_pct: float | None = None
    top10_pct: float | None = None
    holder_count: int | None = None
    is_partial: bool = False
    is_negative_row: bool = False
    api_source: str = "helius"


class HeliusHolderClient:
    """Thin async wrapper over Helius RPC ``getTokenLargestAccounts``
    with a single long-lived aiohttp ClientSession (reused across all
    fetches to share the connection pool)."""

    def __init__(
        self,
        api_key: str,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._timeout_sec = timeout_sec
        self._session: "aiohttp.ClientSession | None" = None  # type: ignore[name-defined]
        # Hook called with (mint, target_age, error_type, detail). Pipeline
        # wires this to DB so timeout bias can be audited later.
        self.on_failure = None  # type: ignore[assignment]

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

    async def fetch(
        self, mint: str, capture_at_age_sec: float
    ) -> HolderSnapshot | None:
        """One-shot fetch. ``capture_at_age_sec`` is stored verbatim on
        the snapshot so analysis can stratify by age."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp missing — skip holder fetch")
            return None

        session = await self._get_session()
        url = RPC_URL_TEMPLATE.format(key=self._api_key)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint, {"commitment": "confirmed"}],
        }
        now = time.time()
        try:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            ) as resp:
                if resp.status != 200:
                    await self._report_failure(
                        mint,
                        capture_at_age_sec,
                        "http_error",
                        f"status={resp.status}",
                    )
                    return None
                data = await resp.json()
        except TimeoutError:
            await self._report_failure(
                mint,
                capture_at_age_sec,
                "timeout",
                f"{self._timeout_sec}s",
            )
            return None
        except Exception as exc:
            await self._report_failure(
                mint,
                capture_at_age_sec,
                "exception",
                type(exc).__name__,
            )
            return None

        accounts = (data.get("result") or {}).get("value") or []
        if not accounts:
            await self._report_failure(
                mint,
                capture_at_age_sec,
                "parse_error",
                "no accounts",
            )
            return None

        amounts: list[int] = []
        for a in accounts:
            try:
                amounts.append(int(a.get("amount") or 0))
            except (ValueError, TypeError):
                continue
        if not amounts:
            await self._report_failure(
                mint,
                capture_at_age_sec,
                "parse_error",
                "no valid amounts",
            )
            return None

        amounts.sort(reverse=True)
        total = sum(amounts)
        if total <= 0:
            return None
        top1 = amounts[0]
        top5 = sum(amounts[:5])
        top10 = sum(amounts[:10])
        holder_count = sum(1 for x in amounts if x > 0)
        is_partial = holder_count < 20
        return HolderSnapshot(
            mint=mint,
            observed_at=now,
            capture_at_age_sec=capture_at_age_sec,
            total_supply_raw=total,
            top1_raw=top1,
            top5_raw=top5,
            top10_raw=top10,
            top1_pct=top1 * 100.0 / total,
            top5_pct=top5 * 100.0 / total,
            top10_pct=top10 * 100.0 / total,
            holder_count=holder_count,
            is_partial=is_partial,
        )

    async def _report_failure(
        self,
        mint: str,
        target_age: float,
        error_type: str,
        error_detail: str,
    ) -> None:
        """Fire-and-forget failure report. Pipeline binds ``on_failure``
        to the DB writer; leaving it None = silent (tests)."""
        cb = self.on_failure
        if cb is None:
            return
        try:
            cb(mint, target_age, error_type, error_detail)
        except Exception:
            logger.debug("holder failure hook raised (ignored)", exc_info=True)


def make_negative_row(mint: str, captured_at: float) -> HolderSnapshot:
    """Pre-capture death placeholder. Analysis counts these as the
    extreme-top1-concentration class."""
    return HolderSnapshot(
        mint=mint,
        observed_at=captured_at,
        capture_at_age_sec=-1.0,
        is_negative_row=True,
    )
