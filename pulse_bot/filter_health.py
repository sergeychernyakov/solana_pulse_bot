# pulse_bot/filter_health.py
"""Scan recent bot.log for filter firing rates.

Surfaces silently-dead gates at startup. Pulse_bot has accumulated
several hard-skip filters (creator blacklist, bot cluster, wash cluster,
T+30 SKIP, survival exit, dynamic max_hold). Without observability,
"0 firings in 4 days" goes unnoticed — exactly how the
`creators.blacklisted` gate stayed dead for weeks.

Called at bot startup; logs a one-screen "FILTER HEALTH" summary so the
operator sees which gates are alive in production.

Counts come from grep on the log file — cheap and survives bot restarts
(unlike in-memory counters). Window defaults to 24h.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Each entry: (display name, regex pattern matched on a log line).
# Patterns are loose to survive log-format drift.
_FILTER_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("creator_blacklist_skip", re.compile(r"CREATOR-BLACKLIST HARD SKIP")),
    ("bot_cluster_skip",       re.compile(r"BOT-CLUSTER HARD SKIP")),
    ("wash_cluster_skip",      re.compile(r"WASH-CLUSTER HARD SKIP")),
    ("survival_predict_exit",  re.compile(r"Survival exit: predicted_remaining")),
    # SKIP_EARLY override fires through decision_service.py:448:
    #   "EARLY OVERRIDE X: rules=BUY → t30_skip=SKIP (proba=Y)"
    # source ∈ {t30, t30_skip, timing, timing_skip}.
    ("t30_skip_early",         re.compile(r"EARLY OVERRIDE .* → t30(_skip)?=SKIP")),
    ("timing_skip_early",      re.compile(r"EARLY OVERRIDE .* → timing(_skip)?=SKIP")),
    ("ml_sl_tightened",        re.compile(r"ml_sl_tightened")),
    ("dynamic_max_hold_used",  re.compile(r"dynamic max_hold predicted=")),
]

_TIMESTAMP_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")


@dataclass(frozen=True)
class FilterCount:
    name: str
    n_firings: int
    last_seen: str | None  # HH:MM:SS or None


def scan_log_for_firings(
    log_path: Path | str = Path("logs/bot.log"),
    max_lines: int = 200_000,
) -> list[FilterCount]:
    """Tail-read up to ``max_lines`` from the log and count per-filter hits.

    Doesn't pre-filter by time — bot's log gets rotated externally, so
    the file usually contains only the last day or so anyway. ``max_lines``
    caps memory in case the file grew unexpectedly.
    """
    p = Path(log_path)
    if not p.exists():
        logger.info("FILTER HEALTH: log file %s missing — skipping", p)
        return []

    counts: dict[str, int] = {name: 0 for name, _ in _FILTER_SIGNATURES}
    last_seen: dict[str, str | None] = {name: None for name, _ in _FILTER_SIGNATURES}

    try:
        with p.open("r", errors="replace") as fh:
            tail: list[str] = []
            for line in fh:
                tail.append(line)
                if len(tail) > max_lines:
                    tail.pop(0)
            for line in tail:
                ts_match = _TIMESTAMP_RE.match(line)
                ts = ts_match.group(0) if ts_match else None
                for name, pat in _FILTER_SIGNATURES:
                    if pat.search(line):
                        counts[name] += 1
                        if ts:
                            last_seen[name] = ts
                        break
    except OSError as exc:
        logger.warning("FILTER HEALTH: failed to read %s: %s", p, exc)
        return []

    return [
        FilterCount(name=name, n_firings=counts[name], last_seen=last_seen[name])
        for name, _ in _FILTER_SIGNATURES
    ]


def log_filter_health_summary(
    log_path: Path | str = Path("logs/bot.log"),
) -> None:
    """Pretty-log filter firing rates so it lands in bot.log right after
    the model registry boot summary. Operator-readable one-screen view.
    """
    started_at = time.time()
    rows = scan_log_for_firings(log_path)
    elapsed_ms = (time.time() - started_at) * 1000

    if not rows:
        return

    logger.info("=" * 78)
    logger.info("FILTER HEALTH — gate firing counts in %s", Path(log_path).name)
    for r in rows:
        flag = "✓" if r.n_firings > 0 else "✗ DEAD"
        last = f"last={r.last_seen}" if r.last_seen else "no fires"
        logger.info(
            "  %-26s n=%-6d %-12s %s",
            r.name, r.n_firings, last, flag,
        )
    logger.info("=" * 78)
    logger.debug("filter_health scan: %.1f ms", elapsed_ms)
