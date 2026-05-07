# scripts/validator_catchup_monitor.py
"""Watchdog: report local Solana validator catchup progress.

Polls ``http://127.0.0.1:8899/getHealth`` + ``getSlot`` +
``getFirstAvailableBlock`` every N seconds and prints a one-line
status. Designed to run in a tmux pane during validator recovery,
or as a one-shot to check current state.

When ``numSlotsBehind < 100`` the validator is "caught up" and the
script exits with code 0. Otherwise stays alive printing progress.

Usage::

    python scripts/validator_catchup_monitor.py            # poll forever
    python scripts/validator_catchup_monitor.py --once     # one-shot
    python scripts/validator_catchup_monitor.py --interval 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

RPC = "http://127.0.0.1:8899"
CAUGHT_UP_THRESHOLD = 100  # slots behind — under this we declare success


def _rpc(method: str, params: list | None = None) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode()
    req = urllib.request.Request(
        RPC, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": {"message": str(exc), "code": -1}}


def _snapshot() -> tuple[int | None, int | None, int | None, str]:
    """Return (slot, behind, first_available, label)."""
    health = _rpc("getHealth")
    slot_resp = _rpc("getSlot")
    fab_resp = _rpc("getFirstAvailableBlock")

    slot = slot_resp.get("result")
    fab = fab_resp.get("result")

    err = health.get("error")
    if err is None:
        return slot, 0, fab, "ok"
    msg = err.get("message", "")
    if "behind" in msg.lower():
        # Numeric extraction from data
        data = err.get("data") or {}
        behind = int(data.get("numSlotsBehind") or 0)
        return slot, behind, fab, "behind"
    return slot, None, fab, f"error: {msg[:60]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    last_slot = None
    last_t = time.time()
    while True:
        slot, behind, fab, label = _snapshot()
        now = time.time()
        # Velocity: how fast is the slot advancing?
        if last_slot is not None and slot is not None and now > last_t:
            dv = (slot - last_slot) / (now - last_t)
            velocity = f"{dv:+.2f} slot/s"
        else:
            velocity = "—"
        last_slot = slot
        last_t = now

        ts = time.strftime("%H:%M:%S")
        if label == "ok":
            print(f"[{ts}] CAUGHT UP  slot={slot}  fab={fab}  velocity={velocity}")
        elif label == "behind":
            ledger_window = (slot - fab) if (slot and fab) else None
            print(
                f"[{ts}] BEHIND     slot={slot}  behind={behind:>7d}  fab={fab}  "
                f"window={ledger_window}  velocity={velocity}"
            )
            if behind is not None and behind < CAUGHT_UP_THRESHOLD:
                print(f"[{ts}] ✅ within {CAUGHT_UP_THRESHOLD} slots — declaring caught up.")
                return 0
        else:
            print(f"[{ts}] {label}")

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
