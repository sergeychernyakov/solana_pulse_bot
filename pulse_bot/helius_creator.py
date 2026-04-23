# pulse_bot/helius_creator.py
"""Creator enrichment pipeline (#48).

Produces append-only snapshots of a creator's aggregate history at the time
of observation. Backtest reads filter by ``observed_at <= ref_ts`` to avoid
leakage (see codex v5 review).

Phase 1 (this module): aggregates come from our own ``tokens`` /
``token_scores`` data — no Helius API calls. This is sufficient for
creators the bot has already seen. Unknown creators return ``None`` and
the scorer degrades to "no creator feature" rather than blocking. A real
Helius fetch hook will plug in here in Phase 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Time (seconds) since a creator's last observed activity after which we
# re-query. Live-only — backtest never refreshes.
DEFAULT_TTL_FRESH_SEC = 6 * 3600
DEFAULT_TTL_STALE_SEC = 24 * 3600

# Heuristic: a token is "rugged" if its last observed price/market cap fell
# to near-zero within ``RUG_WINDOW_SEC`` of creation and it never graduated.
RUG_PEAK_MC_THRESHOLD_SOL = 1.5
RUG_WINDOW_SEC = 600.0


@dataclass
class CreatorSnapshot:
    """Aggregate view of a creator's prior activity at a point in time."""

    creator: str
    observed_at: float
    computed_through_ts: float
    api_source: str  # 'local' | 'helius' | 'backfill'
    total_prior_tokens: int = 0
    rug_count: int = 0
    graduated_count: int = 0
    median_peak_mc_sol: float = 0.0
    avg_ttl_sec: float = 0.0
    inter_token_interval_sec: float = 0.0
    creator_age_days: float = 0.0
    creator_balance_sol: float = 0.0
    feature_version: int = 1

    @property
    def rug_rate(self) -> float:
        return (
            self.rug_count / self.total_prior_tokens if self.total_prior_tokens else 0.0
        )

    @property
    def graduation_rate(self) -> float:
        return (
            self.graduated_count / self.total_prior_tokens
            if self.total_prior_tokens
            else 0.0
        )


class SnapshotSource(Protocol):
    """Pluggable snapshot producer. Phase 1 uses LocalSnapshotSource;
    Phase 2 will add a Helius-backed implementation."""

    async def compute(self, creator: str, ref_ts: float) -> CreatorSnapshot | None: ...


class LocalSnapshotSource:
    """Compute snapshot from our own ``tokens`` and ``token_scores`` tables.

    Strictly respects ``ref_ts``: only tokens with ``created_at < ref_ts``
    contribute. This is the deterministic fallback and the default source
    for backfilling creators the bot has already seen.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def compute(self, creator: str, ref_ts: float) -> CreatorSnapshot | None:
        return await asyncio.to_thread(self._compute_sync, creator, ref_ts)

    def _compute_sync(self, creator: str, ref_ts: float) -> CreatorSnapshot | None:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Per-token TTL = last observed trade minus created_at. Proxy
            # for "how long did this token stay alive?". 0 = no trades ever
            # (rug before any volume). Subquery is cheap — trades are
            # indexed by mint.
            rows = conn.execute(
                """
                SELECT t.mint, t.created_at,
                       COALESCE(ts.market_cap_sol, 0.0)     AS mc_at_score,
                       COALESCE(ts.curve_progress_pct, 0.0) AS curve_pct,
                       COALESCE(
                         (SELECT MAX(tr.timestamp) FROM trades tr
                          WHERE tr.mint = t.mint),
                         t.created_at
                       ) AS last_trade_ts
                FROM tokens t
                LEFT JOIN token_scores ts
                       ON ts.mint = t.mint AND ts.source = 'live'
                WHERE t.creator = ?
                  AND t.created_at IS NOT NULL
                  AND t.created_at < ?
                ORDER BY t.created_at ASC
                """,
                (creator, ref_ts),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return None

        n = len(rows)
        mcs: list[float] = []
        ttls: list[float] = []
        intervals: list[float] = []
        rug_count = 0
        graduated_count = 0
        prev_created: float | None = None

        for r in rows:
            mc = float(r["mc_at_score"] or 0.0)
            curve = float(r["curve_pct"] or 0.0)
            created = float(r["created_at"])
            last_trade = float(r["last_trade_ts"] or created)
            ttl = max(0.0, last_trade - created)
            if mc > 0:
                mcs.append(mc)
            if curve >= 100.0:
                graduated_count += 1
            if mc > 0 and mc < RUG_PEAK_MC_THRESHOLD_SOL and curve < 100.0:
                rug_count += 1
            if prev_created is not None:
                intervals.append(created - prev_created)
            prev_created = created
            ttls.append(ttl)

        earliest = float(rows[0]["created_at"])
        creator_age_days = max(0.0, (ref_ts - earliest) / 86400.0)

        return CreatorSnapshot(
            creator=creator,
            observed_at=ref_ts,
            computed_through_ts=ref_ts,
            api_source="local",
            total_prior_tokens=n,
            rug_count=rug_count,
            graduated_count=graduated_count,
            median_peak_mc_sol=statistics.median(mcs) if mcs else 0.0,
            avg_ttl_sec=(statistics.mean(ttls) if ttls else 0.0),
            inter_token_interval_sec=(statistics.mean(intervals) if intervals else 0.0),
            creator_age_days=creator_age_days,
            creator_balance_sol=0.0,  # requires Helius getBalance; Phase 2
        )


class HeliusSnapshotSource:
    """Phase 2 source (#48): enriches the local snapshot with on-chain data.

    Composition over inheritance: delegates the aggregate computation to
    a ``LocalSnapshotSource`` and adds a single RPC call for
    ``creator_balance_sol`` (SOL balance of the creator wallet).

    Live-path only. Backtest never instantiates this class — point-in-time
    on-chain state cannot be reconstructed from an API call at replay time.
    """

    RPC_URL_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"

    def __init__(
        self,
        local: LocalSnapshotSource,
        api_key: str,
        timeout_sec: float = 2.0,
    ) -> None:
        self._local = local
        self._api_key = api_key
        self._timeout_sec = timeout_sec

    async def compute(self, creator: str, ref_ts: float) -> CreatorSnapshot | None:
        snap = await self._local.compute(creator, ref_ts)
        if snap is None:
            return None
        balance = await self._fetch_balance(creator)
        if balance is not None:
            snap.creator_balance_sol = balance
            snap.api_source = "helius"
        return snap

    async def _fetch_balance(self, wallet: str) -> float | None:
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available — skipping Helius balance")
            return None

        url = self.RPC_URL_TEMPLATE.format(key=self._api_key)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [wallet],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Helius getBalance status=%d for %s",
                            resp.status,
                            wallet[:12],
                        )
                        return None
                    data = await resp.json()
                    lamports = data.get("result", {}).get("value")
                    if lamports is None:
                        return None
                    return float(lamports) / 1_000_000_000.0
        except TimeoutError:
            logger.warning("Helius getBalance timed out for %s", wallet[:12])
            return None
        except Exception:
            logger.exception("Helius getBalance failed for %s", wallet[:12])
            return None


class CreatorSnapshotService:
    """Cache-first lookup with single-flight fetch and live/backtest split.

    - Live path (``now`` close to wall-clock): consult latest snapshot;
      refresh via ``source`` if older than TTL_STALE. Refresh is async
      and single-flighted per creator. Never blocks longer than
      ``fetch_timeout_sec``.
    - Backtest path (``ref_ts`` in the past): read-only. Returns the most
      recent snapshot with ``observed_at <= ref_ts``. Never triggers a
      refresh, never calls the source.
    """

    def __init__(
        self,
        db: object,  # pulse_bot.db.Database (typed lazily)
        source: SnapshotSource,
        ttl_fresh_sec: float = DEFAULT_TTL_FRESH_SEC,
        ttl_stale_sec: float = DEFAULT_TTL_STALE_SEC,
        fetch_timeout_sec: float = 2.0,
    ) -> None:
        self._db = db
        self._source = source
        self._ttl_fresh = ttl_fresh_sec
        self._ttl_stale = ttl_stale_sec
        self._fetch_timeout = fetch_timeout_sec
        self._inflight: dict[str, asyncio.Task] = {}

    def get_for_backtest(self, creator: str, ref_ts: float) -> dict | None:
        """Point-in-time lookup. No side effects, no network."""
        return self._db.get_creator_snapshot_as_of(creator, ref_ts)  # type: ignore[attr-defined]

    async def get_for_live(
        self,
        creator: str,
        now: float | None = None,
    ) -> dict | None:
        """Serve cached snapshot if fresh; else fetch synchronously with
        timeout. On fetch failure, degrades to whatever snapshot exists
        (even stale) or ``None``.
        """
        now = now if now is not None else time.time()
        latest = self._db.get_creator_snapshot_latest(creator)  # type: ignore[attr-defined]

        if latest is not None:
            age = now - float(latest["observed_at"])
            if age <= self._ttl_fresh:
                return latest
            if age <= self._ttl_stale:
                self._schedule_refresh(creator, now)
                return latest

        snap = await self._fetch_with_timeout(creator, now)
        if snap is not None:
            return self._persist(snap)
        return latest  # may be stale; may be None

    def _schedule_refresh(self, creator: str, now: float) -> None:
        if creator in self._inflight and not self._inflight[creator].done():
            return
        task = asyncio.create_task(self._refresh(creator, now))
        self._inflight[creator] = task

    async def _refresh(self, creator: str, now: float) -> None:
        try:
            snap = await self._fetch_with_timeout(creator, now)
            if snap is not None:
                self._persist(snap)
        except Exception:
            logger.exception("creator refresh failed for %s", creator)
        finally:
            self._inflight.pop(creator, None)

    async def _fetch_with_timeout(
        self, creator: str, now: float
    ) -> CreatorSnapshot | None:
        try:
            return await asyncio.wait_for(
                self._source.compute(creator, now),
                timeout=self._fetch_timeout,
            )
        except TimeoutError:
            logger.warning("creator snapshot fetch timed out: %s", creator)
            return None
        except Exception:
            logger.exception("creator snapshot fetch failed: %s", creator)
            return None

    def _persist(self, snap: CreatorSnapshot) -> dict:
        data_json = json.dumps(
            {k: v for k, v in asdict(snap).items() if k not in {"creator"}}
        )
        self._db.save_creator_snapshot(  # type: ignore[attr-defined]
            creator=snap.creator,
            observed_at=snap.observed_at,
            computed_through_ts=snap.computed_through_ts,
            api_source=snap.api_source,
            total_prior_tokens=snap.total_prior_tokens,
            rug_count=snap.rug_count,
            graduated_count=snap.graduated_count,
            median_peak_mc_sol=snap.median_peak_mc_sol,
            avg_ttl_sec=snap.avg_ttl_sec,
            inter_token_interval_sec=snap.inter_token_interval_sec,
            creator_age_days=snap.creator_age_days,
            creator_balance_sol=snap.creator_balance_sol,
            feature_version=snap.feature_version,
            data_json=data_json,
        )
        return self._db.get_creator_snapshot_latest(snap.creator)  # type: ignore[attr-defined]
