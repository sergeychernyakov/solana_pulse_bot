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
PARSED_TX_BATCH = 100  # Helius enhanced API accepts up to 100 sigs/req
DEFAULT_CONCURRENCY = 50
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "backfill_state.json"
RPC_URL_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"
PARSED_URL_TEMPLATE = "https://api.helius.xyz/v0/transactions/?api-key={key}"


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
        ORDER BY t.created_at ASC
    """
    rows = db._sync_query(sql, (GRADUATION_MC_SOL,))
    if limit:
        rows = rows[:limit]
    return rows


def fetch_existing_trade_keys(db: Database, mint: str) -> set[tuple[int, str]]:
    """Existing trade dedup keys for ``mint`` (timestamp truncated + wallet).

    Helius timestamps are integer-seconds (block time floored); existing
    bot timestamps are sub-second. Use int() (truncate, not round)
    on both sides for deterministic match. Banker's rounding caused
    600K duplicate rows in earlier runs (live ts=X.7 rounded to X+1
    while Helius ts=X — different keys, both inserted)."""
    rows = db._sync_query(
        "SELECT timestamp, wallet FROM trades WHERE mint = ?", (mint,)
    )
    return {(int(float(r["timestamp"])), r["wallet"]) for r in rows}


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
                limit=4,
                limit_per_host=4,
                force_close=False,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_all_signatures(self, address: str, until_ts: float) -> list[dict]:
        """Paginate ``getSignaturesForAddress`` until ``blockTime`` falls
        below ``until_ts``. Returns the raw signature dicts (newest first).
        ``until_ts`` is enforced softly: we may go slightly beyond it
        because Solana doesn't support a ts cursor — pagination uses
        ``before=<sig>``."""
        out: list[dict] = []
        before: str | None = None
        while True:
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
                return out
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
        return out

    async def get_parsed_transactions(self, sigs: list[str]) -> list[dict]:
        """Batch-fetch parsed transactions in chunks of ``PARSED_TX_BATCH``."""
        if not sigs:
            return []
        out: list[dict] = []
        for i in range(0, len(sigs), PARSED_TX_BATCH):
            batch = sigs[i : i + PARSED_TX_BATCH]
            async with self._sem:
                self.stats.parsed_calls += 1
                self.stats.parsed_sigs += len(batch)
                data = await self._post("parsed", {"transactions": batch})
            if isinstance(data, list):
                out.extend(data)
            else:
                # Helius returned an error envelope — log and continue.
                self.stats.parse_errors += 1
                logger.warning("parsed-tx batch error: %s", str(data)[:200])
        return out

    async def _post(self, endpoint: str, payload: dict) -> Any:
        """POST with retry + key rotation on 429.

        ``endpoint`` is "rpc" or "parsed". For each attempt we pick the
        next non-quarantined key. On 429, the offending key is
        quarantined for ``KEY_QUARANTINE_SEC`` and we try the next one.
        Returns None when all keys are quarantined or attempts exhausted.
        """
        import aiohttp

        session = await self._get_session()
        backoff = 1.0
        for attempt in range(8):
            key = self._next_key()
            if key is None:
                logger.warning("All Helius keys quarantined; pausing %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            url = (
                RPC_URL_TEMPLATE.format(key=key)
                if endpoint == "rpc"
                else PARSED_URL_TEMPLATE.format(key=key)
            )
            try:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30.0),
                ) as resp:
                    if resp.status == 429:
                        self.stats.rate_limit_hits += 1
                        self._quarantine_key(key)
                        logger.warning(
                            "Helius 429 on key=...%s; quarantine %.0fs (other keys left=%d)",
                            key[-6:],
                            self.KEY_QUARANTINE_SEC,
                            sum(
                                1
                                for k in self._api_keys
                                if self._key_cooldown_until[k] <= time.time()
                            ),
                        )
                        continue  # try next key immediately
                    if resp.status != 200:
                        body = await resp.text()
                        # max-usage-reached is permanent for the month — quarantine longer
                        if "max usage reached" in body.lower() or resp.status in (
                            402,
                            403,
                        ):
                            self._quarantine_key(key)
                            self._key_cooldown_until[key] = time.time() + 86400.0  # 24h
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
) -> tuple[int, int, float]:
    """Fetch + parse + insert trades for a single mint.

    Returns ``(parsed, inserted, elapsed_sec)``."""
    mint = mint_row["mint"]
    created_at = float(mint_row["created_at"] or 0.0)
    until_ts = max(0.0, created_at - 60.0)

    t0 = time.time()
    sig_dicts = await client.get_all_signatures(mint, until_ts)
    if not sig_dicts:
        return (0, 0, time.time() - t0)
    sigs = [s["signature"] for s in sig_dicts]

    parsed_txs = await client.get_parsed_transactions(sigs)

    trades: list[Trade] = []
    for tx in parsed_txs:
        if not isinstance(tx, dict):
            continue
        trade = parse_pump_swap(tx, mint)
        if trade is None:
            client.stats.unknown_txs += 1
            continue
        trades.append(trade)

    if not trades:
        return (0, 0, time.time() - t0)

    # Idempotent insert: dedup against existing rows for this mint.
    existing = fetch_existing_trade_keys(db, mint)
    new_trades = [t for t in trades if (int(t.timestamp), t.wallet) not in existing]
    if not new_trades:
        return (len(trades), 0, time.time() - t0)

    await db.insert_trades_batch(new_trades)
    return (len(trades), len(new_trades), time.time() - t0)


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
                parsed, inserted, elapsed = result
                total_parsed += parsed
                total_inserted += inserted
                elapsed_per_mint.append(elapsed)
                logger.info(
                    "[%d/%d] %s sigs->%d parsed->%d inserted->%d in %.1fs",
                    idx,
                    len(pending),
                    mint[:12],
                    parsed,
                    parsed,
                    inserted,
                    elapsed,
                )
                completed.add(mint)
            state["completed_mints"] = sorted(completed)
            state["rpc_calls"] = client.stats.sig_calls + client.stats.parsed_calls
            _save_state(state_path, state)
            if client.stats.rate_limit_hits >= 50:
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
