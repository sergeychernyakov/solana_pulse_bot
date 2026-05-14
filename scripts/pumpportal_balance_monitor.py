#!/usr/bin/env python3
# scripts/pumpportal_balance_monitor.py
"""Watch PumpPortal wallet for unauthorised outflows.

Runs as a systemd-timer cron (every 10 min). Compares current balance to
last-seen value; on any drop, walks recent signatures, identifies the
exact outgoing transfer(s) and writes a structured alert. The bot itself
never touches this wallet, so any outgoing SOL means somebody else holds
the privkey (PumpPortal's Lightning service is the prime suspect).

State:    data/pumpportal_balance_state.json
History:  logs/pumpportal_balance.log
Alerts:   logs/pumpportal_drain_alerts.log
Flag:     ~/.PUMPPORTAL_DRAIN_FLAG  (touched on every drain, never auto-cleared)
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

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature

DEFAULT_WALLET = "GHf7v3Kx9r9VA58UE47k8FouHzyrsCtWH3gx8VWTdK2v"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "data" / "pumpportal_balance_state.json"
HISTORY_LOG = PROJECT_ROOT / "logs" / "pumpportal_balance.log"
ALERT_LOG = PROJECT_ROOT / "logs" / "pumpportal_drain_alerts.log"
FLAG_PATH = Path.home() / ".PUMPPORTAL_DRAIN_FLAG"

LAMPORTS_PER_SOL = 1_000_000_000
MAX_SIG_LOOKBACK = 25

logger = logging.getLogger("pumpportal_balance_monitor")


@dataclass
class State:
    balance_lamports: int = 0
    last_signature: str | None = None
    last_check_ts: float = 0.0

    @classmethod
    def load(cls) -> "State":
        if not STATE_PATH.exists():
            return cls()
        try:
            data = json.loads(STATE_PATH.read_text())
            return cls(
                balance_lamports=int(data.get("balance_lamports") or 0),
                last_signature=data.get("last_signature"),
                last_check_ts=float(data.get("last_check_ts") or 0.0),
            )
        except Exception as exc:
            logger.warning("state load failed (%s) — starting fresh", exc)
            return cls()

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "balance_lamports": self.balance_lamports,
                    "last_signature": self.last_signature,
                    "last_check_ts": self.last_check_ts,
                },
                indent=2,
            )
        )


def _rpc_url() -> str:
    key = os.environ.get("HELIUS_API_KEY") or os.environ.get(
        "HELIUS_API_KEYS", ""
    ).split(",")[0]
    if not key:
        raise RuntimeError("HELIUS_API_KEY not set in env")
    return f"https://mainnet.helius-rpc.com/?api-key={key.strip()}"


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(line.rstrip() + "\n")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _outgoing_transfers(
    client: AsyncClient, wallet: Pubkey, since_signature: str | None
) -> list[dict[str, Any]]:
    """Return outgoing transfers (lamports decrease) since the last seen sig."""

    until = Signature.from_string(since_signature) if since_signature else None
    sig_resp = await client.get_signatures_for_address(
        wallet, limit=MAX_SIG_LOOKBACK, until=until
    )
    sigs = list(sig_resp.value or [])
    if not sigs:
        return []

    outgoing: list[dict[str, Any]] = []
    for sig_info in reversed(sigs):
        if sig_info.err is not None:
            continue
        sig_str = str(sig_info.signature)
        try:
            tx_resp = await client.get_transaction(
                sig_info.signature,
                encoding="json",
                max_supported_transaction_version=0,
                commitment=Confirmed,
            )
        except Exception as exc:
            logger.warning("get_transaction(%s) failed: %s", sig_str, exc)
            continue
        tx = tx_resp.value
        if tx is None or tx.transaction is None or tx.transaction.meta is None:
            continue
        meta = tx.transaction.meta
        msg = tx.transaction.transaction.message
        try:
            account_keys = list(msg.account_keys)
        except Exception:
            account_keys = []
        wallet_idx = next(
            (i for i, k in enumerate(account_keys) if str(k) == str(wallet)),
            None,
        )
        if wallet_idx is None:
            continue
        try:
            pre = meta.pre_balances[wallet_idx]
            post = meta.post_balances[wallet_idx]
        except Exception:
            continue
        delta = post - pre
        if delta >= 0:
            continue
        # locate destinations: accounts whose balance increased
        destinations = []
        for i, ak in enumerate(account_keys):
            if i == wallet_idx:
                continue
            try:
                d = meta.post_balances[i] - meta.pre_balances[i]
            except Exception:
                continue
            if d > 0:
                destinations.append({"address": str(ak), "lamports": int(d)})
        outgoing.append(
            {
                "signature": sig_str,
                "block_time": int(sig_info.block_time or 0),
                "slot": int(sig_info.slot or 0),
                "delta_lamports": int(delta),
                "destinations": destinations,
            }
        )
    return outgoing


async def _check(threshold_lamports: int, dry_run: bool = False) -> int:
    state = State.load()
    rpc_url = _rpc_url()
    wallet = Pubkey.from_string(DEFAULT_WALLET)

    async with AsyncClient(rpc_url, commitment=Confirmed) as client:
        balance_resp = await client.get_balance(wallet)
        current = int(balance_resp.value)
        outgoing = await _outgoing_transfers(client, wallet, state.last_signature)

    delta = current - state.balance_lamports if state.balance_lamports else 0
    history_line = (
        f"{_ts()}  balance={current/LAMPORTS_PER_SOL:.6f}  "
        f"delta={delta/LAMPORTS_PER_SOL:+.6f}  "
        f"outgoing_txs={len(outgoing)}"
    )
    _append_line(HISTORY_LOG, history_line)
    logger.info(history_line)

    drained = bool(outgoing) or (
        state.balance_lamports and current < state.balance_lamports
    )
    below_threshold = current < threshold_lamports

    if drained or below_threshold:
        for tx in outgoing:
            payload = {
                "ts": _ts(),
                "balance_sol": current / LAMPORTS_PER_SOL,
                "drain_sol": -tx["delta_lamports"] / LAMPORTS_PER_SOL,
                "signature": tx["signature"],
                "block_time": tx["block_time"],
                "slot": tx["slot"],
                "destinations": [
                    {
                        "address": d["address"],
                        "sol": d["lamports"] / LAMPORTS_PER_SOL,
                    }
                    for d in tx["destinations"]
                ],
            }
            _append_line(ALERT_LOG, json.dumps(payload, ensure_ascii=False))
            logger.error("DRAIN %s", json.dumps(payload, ensure_ascii=False))
        if not outgoing and below_threshold:
            payload = {
                "ts": _ts(),
                "balance_sol": current / LAMPORTS_PER_SOL,
                "reason": "balance_below_threshold",
                "threshold_sol": threshold_lamports / LAMPORTS_PER_SOL,
            }
            _append_line(ALERT_LOG, json.dumps(payload, ensure_ascii=False))
            logger.error("LOW %s", json.dumps(payload, ensure_ascii=False))
        try:
            FLAG_PATH.touch()
        except Exception as exc:
            logger.warning("could not touch flag file: %s", exc)

    if not dry_run:
        state.balance_lamports = current
        state.last_check_ts = time.time()
        if outgoing:
            state.last_signature = outgoing[-1]["signature"]
        state.save()

    if drained or below_threshold:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold-sol",
        type=float,
        default=0.025,
        help="Alert if balance drops below this many SOL (default 0.025).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run a single check and print the result without writing state.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    threshold_lamports = int(args.threshold_sol * LAMPORTS_PER_SOL)
    return asyncio.run(_check(threshold_lamports, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
