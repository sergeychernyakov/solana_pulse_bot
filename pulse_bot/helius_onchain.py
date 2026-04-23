# pulse_bot/helius_onchain.py
"""On-chain token state via Helius ``getAccountInfo`` RPC.

Fetches the SPL Token mint account and parses two rug-relevant flags:

* ``mint_authority_revoked`` — creator can no longer print supply;
  set ``1`` when the mint_authority COption tag is ``0`` (None).
* ``freeze_authority_revoked`` — creator can no longer freeze trading
  on any account; same COption test on the freeze_authority field.

Layout reference (SPL Token v3 Mint struct, 82 bytes):

    offset  bytes  field
     0      4      mint_authority COption tag (0=None, 1=Some)
     4      32     mint_authority pubkey (ignored if tag=0)
    36      8      supply (u64 LE)
    44      1      decimals
    45      1      is_initialized
    46      4      freeze_authority COption tag
    50      32     freeze_authority pubkey (ignored if tag=0)

We only look at the two COption tags; everything else is for sanity.

Separate module (not extending ``HeliusHolderClient``) because:
  - different RPC method (``getAccountInfo`` vs ``getTokenLargestAccounts``)
  - one-shot at token creation, not time-series like holder snapshots
  - failure mode is different — mint data doesn't drift, so no retry
    policy needs the "age" bookkeeping that holder snapshots have.
"""

from __future__ import annotations

import base64
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RPC_URL_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"
DEFAULT_TIMEOUT_SEC = 1.5

SPL_MINT_ACCOUNT_SIZE = 82
MINT_AUTHORITY_TAG_OFFSET = 0
FREEZE_AUTHORITY_TAG_OFFSET = 46
SUPPLY_OFFSET = 36
DECIMALS_OFFSET = 44


@dataclass
class MintState:
    """Parsed SPL Mint state. Booleans are null in DB if fetch fails."""

    mint: str
    observed_at: float
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    supply_raw: int
    decimals: int
    # Set when the account exists but layout is wrong (wrong program etc).
    parse_error: str | None = None


def parse_mint_account_data(data_b64: str) -> MintState | None:
    """Decode a base64 SPL mint account blob → MintState (without mint).

    Returns None for anything that doesn't look like a valid mint (wrong
    size, unexpected tag values). ``mint`` is filled in by the caller.
    """
    try:
        raw = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        return None
    if len(raw) < SPL_MINT_ACCOUNT_SIZE:
        return None
    mint_tag = struct.unpack_from("<I", raw, MINT_AUTHORITY_TAG_OFFSET)[0]
    freeze_tag = struct.unpack_from("<I", raw, FREEZE_AUTHORITY_TAG_OFFSET)[0]
    supply = struct.unpack_from("<Q", raw, SUPPLY_OFFSET)[0]
    decimals = raw[DECIMALS_OFFSET]
    # COption tags are 0 (None) or 1 (Some). Any other value = corrupt.
    if mint_tag not in (0, 1) or freeze_tag not in (0, 1):
        return MintState(
            mint="",
            observed_at=time.time(),
            mint_authority_revoked=False,
            freeze_authority_revoked=False,
            supply_raw=supply,
            decimals=decimals,
            parse_error=f"bad_tags:{mint_tag}/{freeze_tag}",
        )
    return MintState(
        mint="",
        observed_at=time.time(),
        mint_authority_revoked=(mint_tag == 0),
        freeze_authority_revoked=(freeze_tag == 0),
        supply_raw=supply,
        decimals=decimals,
    )


class HeliusOnchainClient:
    """Wrapper over ``getAccountInfo`` for mint authority/freeze checks."""

    def __init__(
        self,
        api_key: str,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._timeout_sec = timeout_sec
        self._session: Any = None
        self.on_failure = None  # optional callback (mint, error_type, detail)

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

    async def fetch_mint_state(self, mint: str) -> MintState | None:
        """Fetch + parse mint state. None on hard failure (network/parse)."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp missing — skip mint_state fetch")
            return None

        session = await self._get_session()
        url = RPC_URL_TEMPLATE.format(key=self._api_key)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "base64", "commitment": "confirmed"}],
        }
        try:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
            ) as resp:
                if resp.status != 200:
                    await self._report(mint, "http_error", f"status={resp.status}")
                    return None
                data = await resp.json()
        except TimeoutError:
            await self._report(mint, "timeout", f"{self._timeout_sec}s")
            return None
        except Exception as exc:
            await self._report(mint, "exception", type(exc).__name__)
            return None
        value = (data.get("result") or {}).get("value")
        if value is None:
            await self._report(mint, "parse_error", "no account")
            return None
        data_field = value.get("data")
        if not data_field or not isinstance(data_field, list) or len(data_field) < 1:
            await self._report(mint, "parse_error", "no data field")
            return None
        parsed = parse_mint_account_data(data_field[0])
        if parsed is None:
            await self._report(mint, "parse_error", "layout mismatch")
            return None
        parsed.mint = mint
        return parsed

    async def _report(self, mint: str, error_type: str, detail: str) -> None:
        cb = self.on_failure
        if cb is None:
            return
        try:
            res = cb(mint, error_type, detail)
            if hasattr(res, "__await__"):
                await res
        except Exception:
            logger.debug("on_failure callback failed", exc_info=True)
