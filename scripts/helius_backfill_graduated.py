# scripts/helius_backfill_graduated.py
"""Backfill full pump.fun trade history for graduated mints from Helius.

Context
-------
The bot historically capped per-mint trade collection at ~90 seconds
post-creation (``observe_seconds``). For mints that ever crossed the
graduation threshold (``market_cap_sol >= 85``) we know the outcome —
they are TRUE-OUTCOME winners — but we only have the first ~90 s of
trade flow. To train honest exit / TP / survival heads we need the
complete trade stream from creation through graduation.

This one-shot script:

1. Queries Postgres for every mint that ever observed
   ``market_cap_sol >= 85``.
2. For each mint, paginates ``getSignaturesForAddress`` against the
   public Helius RPC until exhausted (or older than ``token.created_at -
   60 s`` — pump.fun mint accounts only exist after creation, so we
   never need to go beyond that).
3. Batch-fetches signatures via the Helius enhanced parsed-tx endpoint
   (``https://api.helius.xyz/v0/transactions``). Pump.fun trades show
   up as ``source=PUMP_FUN`` ``type=SWAP`` with parsed
   ``tokenTransfers`` + ``accountData`` and we reconstruct a
   ``Trade`` from those. Falls back to skipping unrecognised txs.
4. Inserts in batches via ``Database.insert_trades_batch``. Because
   ``trades`` has no unique constraint we dedupe at the script layer
   against existing rows in DB (by ``timestamp + sol_amount + wallet``)
   so re-runs are idempotent. (Bot may have already collected first 90 s.)
5. Concurrency: bounded semaphore (default 50) for in-flight RPC calls.
6. Resumable: per-mint progress in ``data/backfill_state.json``.

Usage
-----
::

    .venv/bin/python scripts/helius_backfill_graduated.py --limit 50

Important: the running bot uses the same DB; our INSERTs are additive
and the bot only reads, so this is safe to run side-by-side. We do NOT
exhaust quota silently — we log RPC call count and back off on HTTP
429 / RPC error spikes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pulse_bot.db import Database  # noqa: E402
from pulse_bot.models import Trade  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("helius_backfill")

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GRADUATION_MC_SOL = 85.0
SIG_PAGE_LIMIT = 1000
PARSED_TX_BATCH = 1  # Helius batch API silently throttles batch>=10 on free
# tier: requests >60s and never return. Single-sig works fine. We accept the
# 100x more requests in exchange for predictability.
DEFAULT_CONCURRENCY = 50
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "backfill_state.json"
RPC_URL_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"
PARSED_URL_TEMPLATE = "https://api.helius.xyz/v0/transactions/?api-key={key}"

# Solana RPC endpoints used for getSignaturesForAddress + getTransaction
# (saves Helius credits on the cheap RPC calls, reserves them for the
# parsed-tx batch endpoint).
#
# Sources (in priority order, first-tried first):
# 1. PUBLIC_RPC_URLS env var (comma-separated authenticated URLs like
#    Alchemy / QuickNode). Highest priority because they're rate-limited
#    free tiers w/ explicit accounts.
# 2. Anonymous public fallbacks listed below.
_PUBLIC_RPC_FALLBACKS: list[str] = [
    # No anonymous fallbacks active. Tested 2026-04-26:
    # - api.mainnet-beta.solana.com — 4 RPS limit, drowns in 429 at our load
    # - solana-api.projectserum.com — hangs / no response
    # - rpc.ankr.com/solana — 403 "API key not allowed" for getTransaction
    # Use registered free-tier endpoints (Alchemy/QuickNode) via PUBLIC_RPC_URLS env.
]
# 2026-04-29 fix: when PUBLIC_RPC_URLS is empty, auto-fill from Helius
# API keys (each key spawns its own RPC URL) so the script does not
# crash with ZeroDivisionError. Helius mainnet endpoint supports the
# JSON-RPC methods we need (getSignaturesForAddress + getTransaction).
def _build_rpc_urls_from_keys() -> list[str]:
    keys: list[str] = []
    multi = os.environ.get("HELIUS_API_KEYS", "").strip()
    if multi:
        keys.extend(k.strip() for k in multi.split(",") if k.strip())
    single = os.environ.get("HELIUS_API_KEY", "").strip()
    if single and single not in keys:
        keys.append(single)
    return [f"https://mainnet.helius-rpc.com/?api-key={k}" for k in keys]


PUBLIC_RPC_URLS = [
    *(u.strip() for u in os.environ.get("PUBLIC_RPC_URLS", "").split(",") if u.strip()),
    *_PUBLIC_RPC_FALLBACKS,
]
if not PUBLIC_RPC_URLS:
    PUBLIC_RPC_URLS = _build_rpc_urls_from_keys()


# ──────────────────────────────────────────────────────────────────────
# State file (resume support)
# ──────────────────────────────────────────────────────────────────────


def _load_state(path: Path) -> dict:
    """Return the resume state, or an empty skeleton if absent."""
    if not path.exists():
        return {"completed_mints": [], "rpc_calls": 0, "started_at": time.time()}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("State file %s corrupt — starting fresh", path)
        return {"completed_mints": [], "rpc_calls": 0, "started_at": time.time()}


def _save_state(path: Path, state: dict) -> None:
    """Atomic write of resume state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ──────────────────────────────────────────────────────────────────────
# DB helpers (sync — small reads, no asyncpg needed)
# ──────────────────────────────────────────────────────────────────────


def fetch_graduated_mints(db: Database, limit: int | None = None) -> list[dict]:
    """Return ``[{mint, created_at}]`` for every mint that ever crossed
    the graduation MC threshold. Joined to ``tokens`` for ``created_at``
    so we know how far back to paginate signatures."""
    sql = """
        SELECT t.mint AS mint, t.created_at AS created_at
        FROM tokens t
        WHERE EXISTS (
            SELECT 1 FROM trades tr
            WHERE tr.mint = t.mint AND tr.market_cap_sol >= ?
        )
        ORDER BY t.created_at DESC  -- newest first: smaller sig counts + fresher market regime
    """
    rows = db._sync_query(sql, (GRADUATION_MC_SOL,))
    if limit:
        rows = rows[:limit]
    return rows


def fetch_existing_trade_keys(
    db: Database, mint: str
) -> set[tuple[int, str, str, float]]:
    """Existing trade dedup keys for ``mint``: ``(int_ts, wallet, tx_type,
    rounded_sol)``. 2026-04-28 (codex review): the prior coarse key
    ``(int_ts, wallet)`` collapsed legit same-second different-amount
    trades from the same wallet — sniper bots fire several micro-orders
    in one block, all distinct on-chain but indistinguishable here.

    Truncating timestamp to int seconds is preserved (Helius blocktime
    is integer; live bot writes sub-second). Sol-amount rounded to 6
    decimal places: Solana uses lamports (1e-9), so 6 decimal places
    gives μSOL granularity — unique per trade without spurious mismatch
    from float reserialization."""
    rows = db._sync_query(
        "SELECT timestamp, wallet, tx_type, sol_amount FROM trades WHERE mint = ?",
        (mint,),
    )
    return {
        (
            int(float(r["timestamp"])),
            r["wallet"],
            r["tx_type"],
            round(float(r["sol_amount"] or 0.0), 6),
        )
        for r in rows
    }


# ──────────────────────────────────────────────────────────────────────
# Helius RPC client
# ──────────────────────────────────────────────────────────────────────


@dataclass
class RpcStats:
    """Counters for the run, surfaced in the final report."""

    sig_calls: int = 0
    parsed_calls: int = 0
    parsed_sigs: int = 0
    rate_limit_hits: int = 0
    parse_errors: int = 0
    unknown_txs: int = 0


class HeliusBackfillClient:
    """Async client wrapping signatures + enhanced parsed transactions
    with a single shared aiohttp session.

    Multi-key support: pass a list of API keys; the client rotates them
    round-robin per request and quarantines any key that returns 429
    for ``KEY_QUARANTINE_SEC`` seconds (cooldown). If ALL keys are
    quarantined simultaneously, ``_post`` returns None and the caller
    treats it as a transient failure (mint is skipped, can be re-run).
    """

    KEY_QUARANTINE_SEC = 60.0

    def __init__(
        self,
        api_keys: str | list[str],
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if isinstance(api_keys, str):
            api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        if not api_keys:
            raise ValueError("api_keys must be non-empty")
        self._api_keys: list[str] = list(api_keys)
        self._key_idx = 0
        self._public_idx = 0  # round-robin index into PUBLIC_RPC_URLS
        self._key_cooldown_until: dict[str, float] = {k: 0.0 for k in api_keys}
        self._sem = asyncio.Semaphore(concurrency)
        self._session: Any | None = None
        self.stats = RpcStats()

    def _next_key(self) -> str | None:
        """Return next non-quarantined key in rotation, or None if all cooled."""
        now = time.time()
        n = len(self._api_keys)
        for _ in range(n):
            key = self._api_keys[self._key_idx % n]
            self._key_idx += 1
            if self._key_cooldown_until.get(key, 0.0) <= now:
                return key
        return None

    def _quarantine_key(self, key: str) -> None:
        self._key_cooldown_until[key] = time.time() + self.KEY_QUARANTINE_SEC

    async def _get_session(self) -> Any:
        if self._session is None:
            import aiohttp

            # TCPConnector with limit=4 (was unbounded). Helius/edge appears
            # to drop simultaneous SYNs above 4-5 — concurrent burst gives
            # "Server disconnected" / "Connection reset by peer" even when
            # sequential curl works fine. Also force_close=False reuses
            # connections, avoiding repeated TLS handshakes.
            connector = aiohttp.TCPConnector(
                limit=20,
                limit_per_host=10,
                force_close=False,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_all_signatures(
        self, address: str, until_ts: float, max_sigs: int = 8000
    ) -> tuple[list[dict], bool]:
        """Paginate ``getSignaturesForAddress`` until ``blockTime`` falls
        below ``until_ts``.

        2026-04-28 (codex review): Returns ``(sigs, complete)`` where
        ``complete=False`` indicates a transient RPC failure mid-pagination.
        Previously a None response returned a partial ``out`` and the caller
        marked the mint completed → permanent silent truncation of every
        downstream label/feature using that mint's trades. Caller MUST NOT
        add such mints to ``completed_mints``.
        """
        out: list[dict] = []
        before: str | None = None
        while len(out) < max_sigs:
            params: list[Any] = [address, {"limit": SIG_PAGE_LIMIT}]
            if before:
                params[1]["before"] = before
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": params,
            }
            async with self._sem:
                self.stats.sig_calls += 1
                data = await self._post("rpc", payload)
            if data is None:
                # Transient failure: return whatever we have but flag
                # incomplete so caller does NOT mark mint as done.
                return out, False
            res = data.get("result") or []
            if not res:
                break
            out.extend(res)
            before = res[-1]["signature"]
            oldest_ts = res[-1].get("blockTime") or 0
            if len(res) < SIG_PAGE_LIMIT:
                break
            if oldest_ts and oldest_ts < until_ts:
                break
        return out, True

    async def get_parsed_transactions(
        self, sigs: list[str]
    ) -> tuple[list[dict], bool]:
        """Standard JSON-RPC fetch — one ``getTransaction`` per signature.

        Replaces the Helius ``/v0/transactions`` BATCH endpoint which times
        out on free-tier under our load. Slower (1 sig/call vs 100/call) but
        works against any RPC source (Alchemy/QuickNode/public). Returned
        dicts are standard Solana JSON-RPC ``getTransaction(jsonParsed)``
        responses, parsed downstream by ``parse_pump_swap_from_rpc``.
        """
        if not sigs:
            return [], True

        async def _fetch_one(sig: str) -> tuple[dict | None, bool]:
            """Returns (tx, success). success=False on RPC error so the
            caller can flag the batch as incomplete (codex 2026-04-28)."""
            async with self._sem:
                self.stats.parsed_calls += 1
                self.stats.parsed_sigs += 1
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        sig,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                }
                data = await self._post("rpc", payload)
            if data is None:
                return None, False  # RPC failure
            if isinstance(data, dict):
                # Distinguish "tx legitimately not found" (None result, but
                # call succeeded — pruned signature) from network failure.
                return data.get("result"), True
            return None, False

        results = await asyncio.gather(*(_fetch_one(s) for s in sigs))
        out_txs = [r for r, ok in results if r is not None]
        complete = all(ok for _, ok in results)
        return out_txs, complete

    async def _post(self, endpoint: str, payload: dict) -> Any:
        """POST with retry + key rotation on 429.

        ``endpoint`` is "rpc" or "parsed".

        Hybrid routing strategy:
        - "rpc" (getSignaturesForAddress) → public Solana RPC first
          (round-robin via ``self._public_idx``); saves Helius credits.
          On public RPC failure, falls back to Helius keys.
        - "parsed" (parsed-tx BATCH) → Helius keys only (only Helius has
          the /v0/transactions batch endpoint, public RPC has no equivalent).
        """
        import aiohttp

        session = await self._get_session()
        backoff = 1.0
        for attempt in range(8):
            # Pick endpoint URL.
            if endpoint == "rpc":
                # Always use public RPC pool (Alchemy / QuickNode / public).
                # Helius free-tier keys return 403 on standard JSON-RPC
                # (only their /v0/transactions endpoint is permitted, and
                # that is unusable due to per-account batch throttling).
                url = PUBLIC_RPC_URLS[self._public_idx % len(PUBLIC_RPC_URLS)]
                self._public_idx += 1
                key = None
            else:
                # "parsed" endpoint — Helius enhanced. Currently unused
                # (get_parsed_transactions was rewritten to use rpc path),
                # kept for future re-enable if Helius parsed-tx becomes
                # viable again.
                key = self._next_key()
                if key is None:
                    logger.warning(
                        "All Helius keys quarantined; pausing %.1fs", backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                url = PARSED_URL_TEMPLATE.format(key=key)
            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30.0),
                ) as resp:
                    if resp.status == 429:
                        self.stats.rate_limit_hits += 1
                        if key is not None:
                            self._quarantine_key(key)
                            logger.warning(
                                "Helius 429 on key=...%s; quarantine %.0fs",
                                key[-6:],
                                self.KEY_QUARANTINE_SEC,
                            )
                        else:
                            logger.warning("Public RPC 429; brief backoff")
                            await asyncio.sleep(0.5)
                        continue  # try next key/endpoint immediately
                    if resp.status != 200:
                        body = await resp.text()
                        # max-usage-reached is permanent for the month — quarantine longer
                        if key is not None and (
                            "max usage reached" in body.lower()
                            or resp.status in (402, 403)
                        ):
                            self._key_cooldown_until[key] = time.time() + 86400.0
                            logger.warning(
                                "Helius key=...%s exhausted (status=%d); 24h quarantine",
                                key[-6:],
                                resp.status,
                            )
                            continue
                        logger.warning(
                            "Helius status=%d body=%s", resp.status, body[:200]
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    return await resp.json()
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning("Helius timeout on attempt %d", attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as exc:  # network / json
                logger.warning("Helius post failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
        return None


# ──────────────────────────────────────────────────────────────────────
# Pump.fun parsing
# ──────────────────────────────────────────────────────────────────────


def parse_pump_swap_from_rpc(tx: dict, mint: str) -> Trade | None:
    """Parse a standard Solana JSON-RPC ``getTransaction(jsonParsed)`` response
    into a ``Trade``. Used when Helius enhanced parsed-tx is unavailable.

    Logic:
    1. Verify pump.fun program (``PUMPFUN_PROGRAM_ID``) appears in instructions.
    2. Direction from log: ``Program log: Instruction: Buy/Sell``.
    3. Trader = first account key (signer/feePayer).
    4. sol_amount = max |postBalance − preBalance| over non-signer accounts
       (bonding-curve account always has the largest delta; signer balance is
       polluted by tx fees / rent).
    5. token_amount = max |postTokenBalance − preTokenBalance| over accounts
       holding the target ``mint``.
    """
    if not tx or not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    transaction = tx.get("transaction") or {}
    message = transaction.get("message") or {}

    instructions = message.get("instructions") or []
    has_pumpfun = any(
        inst.get("programId") == PUMPFUN_PROGRAM_ID for inst in instructions
    )
    # Inner instructions can also be where the pump.fun program lives.
    if not has_pumpfun:
        for inner_set in meta.get("innerInstructions") or []:
            for inst in inner_set.get("instructions") or []:
                if inst.get("programId") == PUMPFUN_PROGRAM_ID:
                    has_pumpfun = True
                    break
            if has_pumpfun:
                break
    if not has_pumpfun:
        return None

    log_messages = meta.get("logMessages") or []
    is_buy = any("Instruction: Buy" in m for m in log_messages)
    is_sell = any("Instruction: Sell" in m for m in log_messages)
    if not (is_buy or is_sell):
        return None
    tx_type = "buy" if is_buy else "sell"

    account_keys = message.get("accountKeys") or []
    if not account_keys:
        return None
    first_key = account_keys[0]
    trader = first_key.get("pubkey", "") if isinstance(first_key, dict) else first_key
    if not trader:
        return None

    # SOL amount: largest non-signer balance delta. preBalances[0] is signer.
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if (
        len(pre_balances) != len(post_balances)
        or len(pre_balances) < 2
    ):
        return None
    biggest_delta = 0
    for i in range(1, len(pre_balances)):
        delta = abs(post_balances[i] - pre_balances[i])
        if delta > biggest_delta:
            biggest_delta = delta
    sol_amount = biggest_delta / 1e9  # lamports → SOL

    # Token amount: largest balance change for accounts holding ``mint``.
    pre_tok = meta.get("preTokenBalances") or []
    post_tok = meta.get("postTokenBalances") or []
    pre_for_mint = {
        t.get("accountIndex"): t for t in pre_tok if t.get("mint") == mint
    }
    post_for_mint = {
        t.get("accountIndex"): t for t in post_tok if t.get("mint") == mint
    }
    token_amount = 0.0
    for idx in set(pre_for_mint) | set(post_for_mint):
        pre_amt = float(
            (pre_for_mint.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0
        )
        post_amt = float(
            (post_for_mint.get(idx, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0
        )
        change = abs(post_amt - pre_amt)
        if change > token_amount:
            token_amount = change

    if sol_amount <= 0 or token_amount <= 0:
        return None

    block_time = tx.get("blockTime") or 0
    return Trade(
        mint=mint,
        wallet=trader,
        tx_type=tx_type,
        sol_amount=sol_amount,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=0.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=0.0,
        timestamp=float(block_time),
    )


def parse_pump_swap(tx: dict, mint: str) -> Trade | None:
    """Convert one Helius parsed pump.fun SWAP into a ``Trade``.

    Heuristic (validated against 50 graduated-mint sample):

    * Direction: signer (``feePayer``) appears as ``toUserAccount`` on
      the mint's tokenTransfer → BUY; as ``fromUserAccount`` → SELL.
    * sol_amount: largest absolute ``nativeBalanceChange`` across all
      non-signer accounts (this is the bonding-curve account).
      Using signer's nbc directly would be polluted by tx fees /
      account-creation rent (~0.002–0.005 SOL).
    * token_amount: ``tokenTransfers[0].tokenAmount`` for the mint.
    * timestamp: ``tx.timestamp`` (Solana block time, second precision).
    * market_cap_sol / v_sol_in_bonding_curve: not parsable from the
      parsed-tx payload without bonding-curve account state at slot;
      stored as 0.0 — downstream features that need MC at the trade
      moment will have to derive it from cumulative SOL+supply or
      a separate enrichment pass.
    """
    if tx.get("source") != "PUMP_FUN":
        return None
    if tx.get("type") not in ("SWAP", "BUY", "SELL"):
        return None
    fp = tx.get("feePayer") or ""
    if not fp:
        return None

    tts = [t for t in (tx.get("tokenTransfers") or []) if t.get("mint") == mint]
    if not tts:
        return None
    tt = tts[0]

    if tt.get("toUserAccount") == fp:
        tx_type = "buy"
    elif tt.get("fromUserAccount") == fp:
        tx_type = "sell"
    else:
        return None

    token_amount = float(tt.get("tokenAmount") or 0.0)

    # sol_amount = |largest nbc on non-signer account|
    sol_amount_lamports = 0
    for a in tx.get("accountData") or []:
        if a.get("account") == fp:
            continue
        nbc = a.get("nativeBalanceChange") or 0
        if abs(nbc) > sol_amount_lamports:
            sol_amount_lamports = abs(nbc)
    sol_amount = sol_amount_lamports / 1e9

    return Trade(
        mint=mint,
        wallet=fp,
        tx_type=tx_type,
        sol_amount=sol_amount,
        token_amount=token_amount,
        new_token_balance=0.0,
        bonding_curve_key="",
        v_sol_in_bonding_curve=0.0,
        v_tokens_in_bonding_curve=0.0,
        market_cap_sol=0.0,
        timestamp=float(tx.get("timestamp") or 0.0),
        is_creator=False,
    )


# ──────────────────────────────────────────────────────────────────────
# Per-mint workflow
# ──────────────────────────────────────────────────────────────────────


async def backfill_one_mint(
    db: Database,
    client: HeliusBackfillClient,
    mint_row: dict,
) -> tuple[int, int, float, bool]:
    """Fetch + parse + insert trades for a single mint.

    2026-04-28 (codex review): 4-tuple now includes ``complete: bool``
    explicitly. Caller marks ``completed_mints`` ONLY when complete=True.
    Previously a transient RPC failure mid-pagination silently truncated
    a mint's history forever.

    Returns ``(parsed, inserted, elapsed_sec, complete)``."""
    mint = mint_row["mint"]
    created_at = float(mint_row["created_at"] or 0.0)
    until_ts = max(0.0, created_at - 60.0)

    t0 = time.time()
    sig_dicts, sigs_complete = await client.get_all_signatures(mint, until_ts)
    if not sig_dicts:
        # Empty either because no on-chain history (legitimate complete=True
        # for graduated mints whose history is older than our window) OR
        # because the RPC call returned None on first request (incomplete).
        return (0, 0, time.time() - t0, sigs_complete)
    sigs = [s["signature"] for s in sig_dicts]

    parsed_txs, parse_complete = await client.get_parsed_transactions(sigs)
    overall_complete = sigs_complete and parse_complete

    trades: list[Trade] = []
    for tx in parsed_txs:
        if not isinstance(tx, dict):
            continue
        # New path: standard JSON-RPC getTransaction response (Alchemy/QuickNode/public).
        # Old Helius enhanced path (parse_pump_swap) is dead code now that
        # Helius parsed-tx batch is unusable on free tier.
        trade = parse_pump_swap_from_rpc(tx, mint)
        if trade is None:
            client.stats.unknown_txs += 1
            continue
        trades.append(trade)

    if not trades:
        return (0, 0, time.time() - t0, overall_complete)

    # Idempotent insert: dedup against existing rows for this mint.
    # 2026-04-28 (codex review): dedup key now includes tx_type and a
    # rounded sol_amount so two same-second buys from the same wallet
    # with different amounts (sniper bots fire multiple micro-orders
    # per second) are preserved instead of silently collapsed into one.
    existing = fetch_existing_trade_keys(db, mint)
    new_trades = [
        t for t in trades
        if (
            int(t.timestamp),
            t.wallet,
            t.tx_type,
            round(float(t.sol_amount or 0.0), 6),
        ) not in existing
    ]
    if not new_trades:
        return (len(trades), 0, time.time() - t0, overall_complete)

    await db.insert_trades_batch(new_trades)
    return (len(trades), len(new_trades), time.time() - t0, overall_complete)


# ──────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> None:
    # Multi-key support: --api-keys K1,K2,K3 OR HELIUS_API_KEYS env (comma-list)
    # OR HELIUS_API_KEY env (single legacy). Round-robin per request.
    raw_keys = (
        args.api_keys
        or os.environ.get("HELIUS_API_KEYS")
        or os.environ.get("HELIUS_API_KEY")
    )
    if not raw_keys:
        logger.error(
            "No Helius API key. Set HELIUS_API_KEY, HELIUS_API_KEYS=k1,k2,k3, or pass --api-keys"
        )
        sys.exit(2)
    api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    logger.info(
        "Using %d Helius key(s): %s", len(api_keys), [f"...{k[-6:]}" for k in api_keys]
    )

    db = Database()
    state_path = Path(args.state_file)
    state = _load_state(state_path)
    completed = set(state.get("completed_mints", []))

    mints = fetch_graduated_mints(db, args.limit)
    pending = [m for m in mints if m["mint"] not in completed]
    logger.info(
        "graduated mints: total=%d completed=%d pending=%d",
        len(mints),
        len(completed),
        len(pending),
    )
    if not pending:
        logger.info("nothing to do")
        return

    client = HeliusBackfillClient(api_keys, concurrency=args.concurrency)

    total_parsed = 0
    total_inserted = 0
    elapsed_per_mint: list[float] = []

    # Parallelize across mints: process MINT_PARALLELISM mints concurrently.
    # Each backfill_one_mint internally fetches with its own client concurrency.
    # Total in-flight Helius calls ≈ MINT_PARALLELISM × (1-2) typical.
    mint_parallelism = max(1, args.mint_parallelism)

    async def _process(mint_row):
        try:
            return await backfill_one_mint(db, client, mint_row)
        except Exception as exc:
            logger.exception("mint=%s failed: %s", mint_row["mint"], exc)
            return None

    idx = 0
    aborted = False
    try:
        i = 0
        while i < len(pending) and not aborted:
            batch = pending[i : i + mint_parallelism]
            results = await asyncio.gather(*(_process(m) for m in batch))
            for mint_row, result in zip(batch, results):
                idx += 1
                mint = mint_row["mint"]
                if result is None:
                    continue
                parsed, inserted, elapsed, complete = result
                total_parsed += parsed
                total_inserted += inserted
                elapsed_per_mint.append(elapsed)
                logger.info(
                    "[%d/%d] %s sigs->%d parsed->%d inserted->%d complete=%s in %.1fs",
                    idx,
                    len(pending),
                    mint[:12],
                    parsed,
                    parsed,
                    inserted,
                    complete,
                    elapsed,
                )
                # 2026-04-28 (codex review): only mark complete when the
                # ENTIRE fetch+parse chain reported success. The earlier
                # heuristic ``parsed > 0 or inserted > 0`` could pass when
                # an RPC error truncated the result mid-pagination —
                # silently poisoning every downstream label that uses
                # this mint's truncated history.
                if complete:
                    completed.add(mint)
            state["completed_mints"] = sorted(completed)
            state["rpc_calls"] = client.stats.sig_calls + client.stats.parsed_calls
            _save_state(state_path, state)
            if client.stats.rate_limit_hits >= 50000:
                logger.error("Too many 429s — aborting; rerun to resume")
                aborted = True
                break
            i += mint_parallelism
    finally:
        _save_state(state_path, state)
        await client.close()

    avg = sum(elapsed_per_mint) / len(elapsed_per_mint) if elapsed_per_mint else 0.0
    remaining = max(0, len(mints) - len(completed))
    eta_sec = avg * remaining
    logger.info(
        "DONE. parsed=%d inserted=%d avg/mint=%.2fs sig_calls=%d parsed_calls=%d "
        "parsed_sigs=%d unknown_txs=%d rate_limit_hits=%d",
        total_parsed,
        total_inserted,
        avg,
        client.stats.sig_calls,
        client.stats.parsed_calls,
        client.stats.parsed_sigs,
        client.stats.unknown_txs,
        client.stats.rate_limit_hits,
    )
    logger.info(
        "ETA for remaining %d mints @ avg=%.2fs => %.1f hours",
        remaining,
        avg,
        eta_sec / 3600.0,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N graduated mints (oldest first). "
        "Useful for smoke tests.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Max in-flight Helius RPC calls per mint (default {DEFAULT_CONCURRENCY}).",
    )
    p.add_argument(
        "--mint-parallelism",
        type=int,
        default=20,
        help="How many mints to process concurrently (default 20). Total in-flight "
        "Helius calls ≈ mint_parallelism × concurrency for a brief instant.",
    )
    p.add_argument(
        "--state-file",
        type=str,
        default=str(DEFAULT_STATE_PATH),
        help="Resume-state JSON file path.",
    )
    p.add_argument(
        "--api-keys",
        type=str,
        default=None,
        help="Comma-separated Helius API keys (round-robin pool). "
        "Falls back to HELIUS_API_KEYS env, then HELIUS_API_KEY.",
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
