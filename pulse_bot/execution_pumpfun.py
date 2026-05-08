# pulse_bot/execution_pumpfun.py
"""Direct pump.fun bonding-curve execution (Path B).

Bypasses PumpPortal's 1 % trading fee by interacting with the
pump.fun on-chain program directly. Builds, signs, and submits
buy / sell instructions against the mainnet program ID.

Status (2026-05-08): skeleton + PDA derivation + tests. NOT yet
production-ready. Subsequent commits add:
  1. ``buy_instruction`` / ``sell_instruction`` data encoding
  2. ATA (associated token account) creation/fetch
  3. ``PumpFunExecution`` class with sign + submit
  4. Helius RPC error handling + retry logic
  5. Dry-run mode + tests against known-good transactions

Why direct vs PumpPortal API:
  * **No 1 % fee** per side (~$1.77 / 0.10-SOL position retained)
  * Lower latency (no PumpPortal proxy)
  * Self-custodial (no third-party API rate limit)

Why the slow build:
  * Solana instruction encoding is unforgiving — wrong byte order,
    wrong account ordering, or off-by-one PDA bump = failed tx with
    cryptic errors.
  * Each instruction needs to be unit-tested against a known-good
    reference transaction before it touches real SOL.
  * Bonding-curve state changes mid-flight; slippage protection
    must be tight enough not to revert AND loose enough to fill.
"""

from __future__ import annotations

import base64
import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from solders.hash import Hash  # type: ignore
from solders.instruction import AccountMeta, Instruction  # type: ignore
from solders.message import MessageV0  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

if TYPE_CHECKING:
    from solders.keypair import Keypair  # noqa: F401

logger = logging.getLogger(__name__)

# System programs used in transaction assembly.
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)

# Pump.fun trading fee in basis points (1 % paid into the bonding
# curve to the protocol on every buy and every sell). Hardcoded in
# the on-chain program; not configurable.
PUMPFUN_TRADE_FEE_BPS = 100  # 1.00 %

# Minimum lamports needed to keep an ATA rent-exempt. Used when we
# need to simulate ATA creation cost on the very first buy.
ATA_RENT_LAMPORTS = 2_039_280  # standard SPL Token ATA rent-exempt minimum

# Solana base fee per signature.
TX_BASE_FEE_LAMPORTS = 5000

# Default priority fee for pump.fun sniping. Conservative — most
# successful entries clear at 100k microlamports/CU × ~30k CU =
# ~0.0001 SOL = $0.009. Hot mints may need 5-10× this; tunable
# via env (``PULSE_LIVE_PRIORITY_FEE_LAMPORTS``).
DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU = 100_000  # microlamports per CU
DEFAULT_COMPUTE_UNIT_LIMIT = 200_000


# ── Pump.fun program constants ─────────────────────────────────
# Source: https://solscan.io/account/6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
PUMPFUN_PROGRAM_ID = Pubkey.from_string(
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
)
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string(
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"
)
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
ATA_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
RENT_SYSVAR_ID = Pubkey.from_string(
    "SysvarRent111111111111111111111111111111111"
)

# Anchor instruction discriminators — first 8 bytes of
# ``sha256("global:buy")`` and ``sha256("global:sell")``. These are
# stable across pump.fun program upgrades; verified against multiple
# successful mainnet transactions.
BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])


# ── PDA derivation ─────────────────────────────────────────────
def derive_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    """Derive the bonding-curve PDA for a given mint.

    Seed: ``[b"bonding-curve", mint.to_bytes()]`` under
    ``PUMPFUN_PROGRAM_ID``. Returns the PDA only (drop the bump);
    callers that need the bump should use ``find_program_address``
    directly.
    """
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_global_pda() -> Pubkey:
    """Derive the global state PDA (single per-program account)."""
    pda, _bump = Pubkey.find_program_address(
        [b"global"],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_event_authority_pda() -> Pubkey:
    """Derive the event-authority PDA used for emitted CPI events."""
    pda, _bump = Pubkey.find_program_address(
        [b"__event_authority"],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_associated_token_account(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the ATA (Associated Token Account) for an owner+mint.

    Seed: ``[owner, TOKEN_PROGRAM, mint]`` under
    ``ATA_PROGRAM_ID``. Standard SPL Token ATA derivation —
    every wallet's pump.fun token holdings live at this address.
    """
    pda, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ATA_PROGRAM_ID,
    )
    return pda


# ── Instruction data encoding ──────────────────────────────────
def encode_buy_instruction_data(
    token_amount_raw: int,
    max_sol_cost_lamports: int,
) -> bytes:
    """Pack the buy-instruction data payload.

    Layout (Anchor convention):
        [0..8]   discriminator (8 bytes, fixed)
        [8..16]  amount (u64 LE) — token amount we want, raw integer
        [16..24] max_sol_cost (u64 LE) — slippage cap in lamports

    ``amount`` is the EXACT token output target. The program reverts
    if the SOL cost exceeds ``max_sol_cost`` (slippage protection).
    """
    if token_amount_raw < 0:
        raise ValueError(f"token_amount_raw must be >= 0, got {token_amount_raw}")
    if max_sol_cost_lamports < 0:
        raise ValueError(
            f"max_sol_cost_lamports must be >= 0, got {max_sol_cost_lamports}"
        )
    return BUY_DISCRIMINATOR + struct.pack(
        "<QQ", token_amount_raw, max_sol_cost_lamports
    )


def encode_sell_instruction_data(
    token_amount_raw: int,
    min_sol_output_lamports: int,
) -> bytes:
    """Pack the sell-instruction data payload.

    Layout:
        [0..8]   discriminator
        [8..16]  amount (u64 LE) — tokens to sell
        [16..24] min_sol_output (u64 LE) — minimum SOL we'll accept

    The program reverts if SOL out is below ``min_sol_output``.
    """
    if token_amount_raw < 0:
        raise ValueError(f"token_amount_raw must be >= 0, got {token_amount_raw}")
    if min_sol_output_lamports < 0:
        raise ValueError(
            f"min_sol_output_lamports must be >= 0, got {min_sol_output_lamports}"
        )
    return SELL_DISCRIMINATOR + struct.pack(
        "<QQ", token_amount_raw, min_sol_output_lamports
    )


# ── Account-list builders (instruction needs accounts in exact order) ─
@dataclass
class PumpFunBuyAccounts:
    """Account ordering for the buy instruction.

    Must match the on-chain program's expected order exactly.
    Verified against multiple decoded mainnet buy txs.
    """

    global_pda: Pubkey
    fee_recipient: Pubkey
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    associated_user: Pubkey
    user: Pubkey
    system_program: Pubkey
    token_program: Pubkey
    rent_sysvar: Pubkey
    event_authority: Pubkey
    program: Pubkey

    @classmethod
    def for_user_and_mint(cls, user: Pubkey, mint: Pubkey) -> "PumpFunBuyAccounts":
        """Build a complete account list given just the user wallet
        and the target mint. All PDAs derived; SPL/system/rent
        constants pulled from module-level."""
        bonding = derive_bonding_curve_pda(mint)
        return cls(
            global_pda=derive_global_pda(),
            fee_recipient=PUMPFUN_FEE_RECIPIENT,
            mint=mint,
            bonding_curve=bonding,
            associated_bonding_curve=derive_associated_token_account(bonding, mint),
            associated_user=derive_associated_token_account(user, mint),
            user=user,
            system_program=SYSTEM_PROGRAM_ID,
            token_program=TOKEN_PROGRAM_ID,
            rent_sysvar=RENT_SYSVAR_ID,
            event_authority=derive_event_authority_pda(),
            program=PUMPFUN_PROGRAM_ID,
        )


# ── Bonding-curve state ───────────────────────────────────────
@dataclass
class BondingCurveState:
    """On-chain bonding-curve account state.

    Account layout (Anchor):
        [0..8]   discriminator (8 bytes — fixed for the account type)
        [8..16]  virtual_token_reserves (u64 LE)
        [16..24] virtual_sol_reserves (u64 LE) — in lamports
        [24..32] real_token_reserves (u64 LE)
        [32..40] real_sol_reserves (u64 LE) — in lamports
        [40..48] token_total_supply (u64 LE)
        [48]     complete (bool, 1 byte) — true after graduation
    """

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool

    @classmethod
    def from_account_data(cls, data: bytes) -> "BondingCurveState":
        """Parse the 8-byte discriminator + Anchor-encoded fields.

        Raises ``ValueError`` on malformed buffers — short accounts
        usually mean the bonding-curve PDA doesn't exist yet (token
        was created off pump.fun, or RPC returned stale state).
        """
        if len(data) < 49:
            raise ValueError(
                f"bonding-curve account too short: {len(data)} bytes "
                f"(need >=49)"
            )
        # Skip the 8-byte Anchor discriminator.
        body = data[8:]
        v_tok, v_sol, r_tok, r_sol, supply = struct.unpack_from("<QQQQQ", body, 0)
        complete = bool(body[40])
        return cls(
            virtual_token_reserves=v_tok,
            virtual_sol_reserves=v_sol,
            real_token_reserves=r_tok,
            real_sol_reserves=r_sol,
            token_total_supply=supply,
            complete=complete,
        )


def estimate_buy_output_tokens(
    sol_amount_lamports: int,
    state: BondingCurveState,
    fee_bps: int = PUMPFUN_TRADE_FEE_BPS,
) -> int:
    """Constant-product AMM math: how many raw tokens for ``sol_amount``.

    Pump.fun applies a fee on the **input** SOL (not the output). The
    remaining post-fee SOL goes into the curve, and the corresponding
    delta in tokens is the buyer's output.

        sol_in_post_fee = sol_amount × (1 − fee)
        new_v_sol       = v_sol + sol_in_post_fee
        new_v_tokens    = v_sol × v_tokens / new_v_sol     (constant product)
        tokens_out      = v_tokens − new_v_tokens

    Returns raw token integer (no decimals adjustment — pump.fun
    tokens use 6 decimals; UI conversions belong upstream).
    """
    if sol_amount_lamports <= 0:
        return 0
    if state.complete:
        # Post-graduation: bonding curve no longer accepts buys.
        return 0
    fee_factor_num = 10_000 - fee_bps
    sol_in_post_fee = (sol_amount_lamports * fee_factor_num) // 10_000
    if sol_in_post_fee <= 0:
        return 0
    new_v_sol = state.virtual_sol_reserves + sol_in_post_fee
    new_v_tokens = (state.virtual_sol_reserves * state.virtual_token_reserves) // new_v_sol
    tokens_out = state.virtual_token_reserves - new_v_tokens
    # Cap at real token reserves — can't sell more than the curve actually holds.
    return max(0, min(tokens_out, state.real_token_reserves))


# ── Helius RPC client (read-only + simulate) ──────────────────
@dataclass
class SimulateResult:
    """Outcome of a ``simulateTransaction`` call.

    ``success=False`` means the program would have reverted (e.g.
    slippage cap tripped, insufficient funds, race-conditioned
    bonding-curve state). ``logs`` contains the on-chain log lines
    we can scan for emitted events (post-trade token balance, etc).
    """

    success: bool
    err: Any | None = None
    logs: list[str] | None = None
    units_consumed: int | None = None
    raw_response: dict[str, Any] | None = None


class HeliusRpc:
    """Minimal async RPC client around Helius mainnet endpoint.

    Wraps two endpoints we need for pump.fun simulation:
      * ``getAccountInfo`` — fetch the bonding-curve account state
      * ``simulateTransaction`` — dry-execute a signed tx

    Uses ``aiohttp`` so the live trading hot path doesn't block the
    bot's pulse loop. Reuses one ``ClientSession`` per instance;
    callers should ``await rpc.close()`` on shutdown.
    """

    def __init__(self, api_key: str, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = (
            base_url or "https://mainnet.helius-rpc.com"
        )
        self._url = f"{self._base_url}/?api-key={self._api_key}"
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None:
            import aiohttp  # type: ignore
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, payload: dict, timeout_sec: float = 5.0) -> dict:
        sess = await self._ensure_session()
        async with sess.post(
            self._url,
            json=payload,
            timeout=timeout_sec,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_account_info(
        self, pubkey: Pubkey, encoding: str = "base64"
    ) -> bytes | None:
        """Return the raw account-data bytes, or None if account
        doesn't exist (most pre-graduation pump.fun mints don't have
        the bonding curve initialised until the first buy)."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                str(pubkey),
                {"encoding": encoding, "commitment": "processed"},
            ],
        }
        resp = await self._post(payload)
        value = resp.get("result", {}).get("value")
        if value is None:
            return None
        data_field = value.get("data")
        if not data_field or not isinstance(data_field, list):
            return None
        # data = [base64_string, "base64"]
        try:
            return base64.b64decode(data_field[0])
        except Exception as exc:
            logger.warning("getAccountInfo decode failed for %s: %s", pubkey, exc)
            return None

    async def fetch_bonding_curve_state(
        self, mint: Pubkey
    ) -> BondingCurveState | None:
        """Convenience wrapper: PDA-derive + getAccountInfo + parse."""
        pda = derive_bonding_curve_pda(mint)
        data = await self.get_account_info(pda)
        if data is None:
            return None
        try:
            return BondingCurveState.from_account_data(data)
        except ValueError as exc:
            logger.warning(
                "bonding curve %s parse failed (mint=%s): %s",
                pda, mint, exc,
            )
            return None

    async def get_latest_blockhash(self) -> Hash | None:
        """Fetch the cluster's most recent blockhash for tx signing.

        Returns ``None`` if the RPC call fails — caller should
        retry or skip.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "processed"}],
        }
        try:
            resp = await self._post(payload)
        except Exception as exc:
            logger.warning("getLatestBlockhash failed: %s", exc)
            return None
        value = resp.get("result", {}).get("value")
        if not value:
            return None
        bh_str = value.get("blockhash")
        if not bh_str:
            return None
        try:
            return Hash.from_string(bh_str)
        except Exception as exc:
            logger.warning("blockhash decode failed (%s): %s", bh_str, exc)
            return None

    async def simulate_transaction(
        self,
        signed_tx_base64: str,
        *,
        replace_recent_blockhash: bool = True,
        sig_verify: bool = False,
    ) -> SimulateResult:
        """Run ``simulateTransaction`` on a serialised, signed tx.

        Args:
            signed_tx_base64: Base64-encoded serialised transaction.
            replace_recent_blockhash: Tell Helius to swap in a fresh
                blockhash. Without this, simulation often fails with
                "BlockhashNotFound" because we sign offline.
            sig_verify: Whether the simulator should verify the
                signature. Default False — we can simulate against a
                tx signed with a stale blockhash too.

        Returns:
            :class:`SimulateResult`. ``success=True`` iff the program
            instruction would have completed without reverting.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                signed_tx_base64,
                {
                    "encoding": "base64",
                    "commitment": "processed",
                    "sigVerify": sig_verify,
                    "replaceRecentBlockhash": replace_recent_blockhash,
                },
            ],
        }
        try:
            resp = await self._post(payload, timeout_sec=10.0)
        except Exception as exc:
            return SimulateResult(success=False, err=str(exc))
        result = resp.get("result", {}).get("value", {})
        err = result.get("err")
        return SimulateResult(
            success=(err is None),
            err=err,
            logs=result.get("logs"),
            units_consumed=result.get("unitsConsumed"),
            raw_response=resp,
        )


# ── Instruction builders ───────────────────────────────────────
def build_set_compute_unit_limit_ix(units: int) -> Instruction:
    """Compute Budget program: cap the compute units this tx can use.

    Discriminator byte 0x02 + u32 LE units. Default budget is 200k
    CU; sniping txs need ~30-50k for the buy itself plus ATA creation.
    """
    if units < 0 or units > 1_400_000:
        raise ValueError(f"compute units out of range: {units}")
    data = bytes([0x02]) + struct.pack("<I", units)
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM_ID,
        accounts=[],
        data=data,
    )


def build_set_compute_unit_price_ix(microlamports_per_cu: int) -> Instruction:
    """Compute Budget program: priority fee in microlamports per CU.

    Discriminator byte 0x03 + u64 LE microlamports. Total priority
    fee = micro × cu_limit / 1_000_000. At 100k microlamports/CU and
    200k CU, that's ~0.00002 SOL per attempt — enough for typical
    pump.fun blocks but bumpable on hot mints.
    """
    if microlamports_per_cu < 0:
        raise ValueError(f"microlamports must be >= 0, got {microlamports_per_cu}")
    data = bytes([0x03]) + struct.pack("<Q", microlamports_per_cu)
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM_ID,
        accounts=[],
        data=data,
    )


def build_create_ata_idempotent_ix(
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
) -> Instruction:
    """Associated Token Account program: create-if-not-exists.

    Idempotent variant (instruction byte = 0x01) is safe to issue
    on every buy — if the ATA already exists, the program returns
    success without spending rent. Costs ~5k CU.

    Account order is fixed by the SPL ATA program:
      0. payer (signer, writable)              — funds rent if creating
      1. ata (writable)                        — the ATA being created
      2. owner (readonly)                      — the wallet owning the ATA
      3. mint (readonly)
      4. system_program (readonly)
      5. token_program (readonly)
    """
    ata = derive_associated_token_account(owner, mint)
    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(
        program_id=ATA_PROGRAM_ID,
        accounts=accounts,
        data=bytes([0x01]),  # CreateIdempotent
    )


def build_pump_buy_ix(
    user: Pubkey,
    mint: Pubkey,
    token_amount_raw: int,
    max_sol_cost_lamports: int,
) -> Instruction:
    """Build the pump.fun buy instruction.

    Account ordering matches the published IDL (verified against
    decoded mainnet buy transactions). Each account's signer/writable
    flags are critical — the on-chain program validates them and
    reverts on mismatch.
    """
    accounts_obj = PumpFunBuyAccounts.for_user_and_mint(user, mint)
    # is_signer / is_writable flags from decoded mainnet txs:
    #   1. global                 ro, not signer
    #   2. fee_recipient          rw, not signer
    #   3. mint                   ro, not signer
    #   4. bonding_curve          rw, not signer
    #   5. associated_bonding_curve  rw, not signer
    #   6. associated_user        rw, not signer
    #   7. user                   rw, SIGNER
    #   8. system_program         ro, not signer
    #   9. token_program          ro, not signer
    #  10. rent_sysvar            ro, not signer
    #  11. event_authority        ro, not signer
    #  12. program                ro, not signer
    accounts = [
        AccountMeta(pubkey=accounts_obj.global_pda, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.associated_user, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=accounts_obj.system_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.token_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.rent_sysvar, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.event_authority, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.program, is_signer=False, is_writable=False),
    ]
    data = encode_buy_instruction_data(token_amount_raw, max_sol_cost_lamports)
    return Instruction(
        program_id=PUMPFUN_PROGRAM_ID,
        accounts=accounts,
        data=data,
    )


def build_pump_sell_ix(
    user: Pubkey,
    mint: Pubkey,
    token_amount_raw: int,
    min_sol_output_lamports: int,
) -> Instruction:
    """Build the pump.fun sell instruction.

    Differs from buy in two account-list slots:
      * No rent_sysvar (pump.fun doesn't reach for rent on sells)
      * associated_token_program slot present (used for ATA close
        on sells of full balance — although we don't close it here)
    """
    accounts_obj = PumpFunSellAccounts.for_user_and_mint(user, mint)
    accounts = [
        AccountMeta(pubkey=accounts_obj.global_pda, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.associated_user, is_signer=False, is_writable=True),
        AccountMeta(pubkey=accounts_obj.user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=accounts_obj.system_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.associated_token_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.token_program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.event_authority, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.program, is_signer=False, is_writable=False),
    ]
    data = encode_sell_instruction_data(token_amount_raw, min_sol_output_lamports)
    return Instruction(
        program_id=PUMPFUN_PROGRAM_ID,
        accounts=accounts,
        data=data,
    )


# ── Transaction assembly ───────────────────────────────────────
def build_buy_transaction(
    keypair: "Keypair",
    mint: Pubkey,
    sol_amount_lamports: int,
    state: BondingCurveState,
    recent_blockhash: Hash,
    slippage_bps: int = 100,
    priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    compute_unit_limit: int = DEFAULT_COMPUTE_UNIT_LIMIT,
) -> VersionedTransaction:
    """Build, sign, and return a complete buy transaction.

    Pipeline:
      1. Estimate token output from (sol_amount, state) via constant
         product — establishes the ``amount`` field on the instr.
      2. Apply slippage: ``max_sol_cost = sol_amount × (1 + slip_bps/10_000)``
      3. Assemble four instructions:
           a. ComputeBudget.SetUnitLimit
           b. ComputeBudget.SetUnitPrice (priority fee)
           c. ATA.CreateIdempotent (no-op if user ATA exists)
           d. PumpFun.Buy
      4. Wrap in MessageV0 with caller-provided recent blockhash
      5. Sign with the wallet keypair → VersionedTransaction

    Returns a fully-signed transaction ready to either ``simulate``
    or ``submit``.
    """
    user = keypair.pubkey()
    expected_tokens = estimate_buy_output_tokens(sol_amount_lamports, state)
    if expected_tokens <= 0:
        raise ValueError(
            "buy would yield zero tokens — bonding curve complete or "
            "sol_amount too small"
        )
    max_sol_cost = (
        sol_amount_lamports * (10_000 + slippage_bps)
    ) // 10_000
    instructions = [
        build_set_compute_unit_limit_ix(compute_unit_limit),
        build_set_compute_unit_price_ix(priority_fee_microlamports_per_cu),
        build_create_ata_idempotent_ix(payer=user, owner=user, mint=mint),
        build_pump_buy_ix(
            user=user,
            mint=mint,
            token_amount_raw=expected_tokens,
            max_sol_cost_lamports=max_sol_cost,
        ),
    ]
    msg = MessageV0.try_compile(
        payer=user,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    return VersionedTransaction(msg, [keypair])


def build_sell_transaction(
    keypair: "Keypair",
    mint: Pubkey,
    token_amount_raw: int,
    state: BondingCurveState,
    recent_blockhash: Hash,
    slippage_bps: int = 100,
    priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    compute_unit_limit: int = DEFAULT_COMPUTE_UNIT_LIMIT,
) -> VersionedTransaction:
    """Build a sell transaction. Same pattern as buy, no ATA-create
    (we already own the ATA from the prior buy)."""
    user = keypair.pubkey()
    expected_sol = estimate_sell_output_lamports(token_amount_raw, state)
    if expected_sol <= 0:
        raise ValueError(
            "sell would yield zero SOL — bonding curve complete or "
            "amount too small"
        )
    # Slippage on sell goes the OTHER direction: minimum we'll accept.
    min_sol_output = (
        expected_sol * (10_000 - slippage_bps)
    ) // 10_000
    instructions = [
        build_set_compute_unit_limit_ix(compute_unit_limit),
        build_set_compute_unit_price_ix(priority_fee_microlamports_per_cu),
        build_pump_sell_ix(
            user=user,
            mint=mint,
            token_amount_raw=token_amount_raw,
            min_sol_output_lamports=min_sol_output,
        ),
    ]
    msg = MessageV0.try_compile(
        payer=user,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    return VersionedTransaction(msg, [keypair])


def serialize_signed_tx_base64(tx: VersionedTransaction) -> str:
    """Serialize a signed VersionedTransaction to the base64 string
    expected by ``simulateTransaction`` / ``sendTransaction`` RPCs."""
    return base64.b64encode(bytes(tx)).decode("ascii")


# ── Orchestration layer ────────────────────────────────────────
@dataclass
class PumpExecuteResult:
    """High-level outcome of a buy/sell attempt or simulation.

    Captures everything needed for slippage analysis: success flag,
    estimated tokens/SOL going in, what we would have received,
    units consumed, and the underlying RPC error if any.
    """

    side: str  # "buy" | "sell"
    mint: str
    submitted_live: bool  # False = simulation, True = real send
    success: bool
    sol_amount_lamports: int = 0
    expected_tokens: int = 0
    expected_sol_out_lamports: int = 0
    slippage_bps_cap: int = 0
    units_consumed: int | None = None
    err: Any | None = None
    logs: list[str] | None = None


class PumpFunExecution:
    """High-level controller — fetches state, builds tx, signs,
    submits to ``simulateTransaction`` (default) or, when explicitly
    enabled, ``sendTransaction``.

    Default mode is **simulate-only**. The constructor accepts
    ``allow_live_submit=False``; flipping it to True is the explicit
    operator action that turns this into real-money trading. The
    method names ``simulate_buy`` / ``simulate_sell`` never submit.
    Submitting requires the separate ``submit_buy`` / ``submit_sell``
    pair, which check the flag and refuse otherwise.
    """

    def __init__(
        self,
        rpc: HeliusRpc,
        keypair: "Keypair",
        *,
        allow_live_submit: bool = False,
    ):
        self._rpc = rpc
        self._keypair = keypair
        self._allow_live = allow_live_submit

    @classmethod
    def from_env(cls, *, allow_live_submit: bool = False) -> "PumpFunExecution":
        """Construct from env: HELIUS_API_KEY + SOL_WALLET_KEYPAIR.

        Same convention as ``LiveExecution.from_env`` — ``SOL_WALLET_KEYPAIR``
        is a base58-encoded 64-byte secret key string.
        """
        import os as _os
        from solders.keypair import Keypair  # type: ignore

        helius_key = _os.environ.get("HELIUS_API_KEY", "")
        kp_b58 = _os.environ.get("SOL_WALLET_KEYPAIR", "")
        if not helius_key:
            raise RuntimeError("HELIUS_API_KEY env not set")
        if not kp_b58:
            raise RuntimeError("SOL_WALLET_KEYPAIR env not set")
        kp = Keypair.from_base58_string(kp_b58)
        return cls(
            rpc=HeliusRpc(api_key=helius_key),
            keypair=kp,
            allow_live_submit=allow_live_submit,
        )

    @property
    def wallet_pubkey(self) -> Pubkey:
        return self._keypair.pubkey()

    async def close(self) -> None:
        await self._rpc.close()

    async def simulate_buy(
        self,
        mint: Pubkey | str,
        sol_amount_lamports: int,
        *,
        slippage_bps: int = 100,
        priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    ) -> PumpExecuteResult:
        """Build the buy tx and run it through ``simulateTransaction``.

        NEVER submits on-chain. Cost: 0 SOL.

        Returns ``PumpExecuteResult`` with:
          * ``success`` — would the program have accepted the buy?
          * ``expected_tokens`` — output computed from current curve
          * ``err`` — on-chain error code if reverted (slippage,
            insufficient funds, race, etc)

        Use case: backfill replay of historical paper_trade entries
        to measure REAL fill rate at the chosen slippage cap, with
        zero financial risk.
        """
        return await self._buy_inner(
            mint=mint,
            sol_amount_lamports=sol_amount_lamports,
            slippage_bps=slippage_bps,
            priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            submit_live=False,
        )

    async def simulate_sell(
        self,
        mint: Pubkey | str,
        token_amount_raw: int,
        *,
        slippage_bps: int = 100,
        priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    ) -> PumpExecuteResult:
        return await self._sell_inner(
            mint=mint,
            token_amount_raw=token_amount_raw,
            slippage_bps=slippage_bps,
            priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            submit_live=False,
        )

    async def submit_buy(
        self,
        mint: Pubkey | str,
        sol_amount_lamports: int,
        *,
        slippage_bps: int = 100,
        priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    ) -> PumpExecuteResult:
        """REAL on-chain submission. Spends SOL on success and on
        failure (priority fee paid regardless). Refuses unless
        ``allow_live_submit=True`` was set at construction time."""
        if not self._allow_live:
            raise RuntimeError(
                "submit_buy refused: allow_live_submit=False at construction. "
                "Use simulate_buy for dry-run, or build with "
                "allow_live_submit=True after deliberate operator approval."
            )
        return await self._buy_inner(
            mint=mint,
            sol_amount_lamports=sol_amount_lamports,
            slippage_bps=slippage_bps,
            priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            submit_live=True,
        )

    async def submit_sell(
        self,
        mint: Pubkey | str,
        token_amount_raw: int,
        *,
        slippage_bps: int = 100,
        priority_fee_microlamports_per_cu: int = DEFAULT_PRIORITY_FEE_MICROLAMPORTS_PER_CU,
    ) -> PumpExecuteResult:
        if not self._allow_live:
            raise RuntimeError(
                "submit_sell refused: allow_live_submit=False at construction"
            )
        return await self._sell_inner(
            mint=mint,
            token_amount_raw=token_amount_raw,
            slippage_bps=slippage_bps,
            priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            submit_live=True,
        )

    # ── Inner shared paths ─────────────────────────────────────
    async def _buy_inner(
        self,
        mint: Pubkey | str,
        sol_amount_lamports: int,
        slippage_bps: int,
        priority_fee_microlamports_per_cu: int,
        submit_live: bool,
    ) -> PumpExecuteResult:
        mint_pk = mint if isinstance(mint, Pubkey) else Pubkey.from_string(mint)
        # 1. Fetch on-chain bonding-curve state.
        state = await self._rpc.fetch_bonding_curve_state(mint_pk)
        if state is None:
            return PumpExecuteResult(
                side="buy",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                sol_amount_lamports=sol_amount_lamports,
                slippage_bps_cap=slippage_bps,
                err="bonding_curve_account_missing",
            )
        if state.complete:
            return PumpExecuteResult(
                side="buy",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                sol_amount_lamports=sol_amount_lamports,
                slippage_bps_cap=slippage_bps,
                err="curve_complete_post_graduation",
            )
        # 2. Get recent blockhash.
        blockhash = await self._rpc.get_latest_blockhash()
        if blockhash is None:
            return PumpExecuteResult(
                side="buy",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                sol_amount_lamports=sol_amount_lamports,
                slippage_bps_cap=slippage_bps,
                err="blockhash_fetch_failed",
            )
        # 3. Build + sign tx.
        try:
            tx = build_buy_transaction(
                keypair=self._keypair,
                mint=mint_pk,
                sol_amount_lamports=sol_amount_lamports,
                state=state,
                recent_blockhash=blockhash,
                slippage_bps=slippage_bps,
                priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            )
        except ValueError as exc:
            return PumpExecuteResult(
                side="buy",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                sol_amount_lamports=sol_amount_lamports,
                slippage_bps_cap=slippage_bps,
                err=f"tx_build_failed:{exc}",
            )
        expected_tokens = estimate_buy_output_tokens(sol_amount_lamports, state)
        b64 = serialize_signed_tx_base64(tx)
        # 4. Simulate (and only submit if explicit live mode).
        if submit_live:
            # Live submission is wired separately; for now we only
            # ship the simulate path — the entire Phase 5 backfill
            # uses simulate. Live submit is added in a later commit
            # after the simulator validates the pipeline end-to-end
            # on real on-chain state.
            raise NotImplementedError(
                "live submit path not yet implemented — "
                "simulate path only in this build"
            )
        sim = await self._rpc.simulate_transaction(b64)
        return PumpExecuteResult(
            side="buy",
            mint=str(mint_pk),
            submitted_live=False,
            success=sim.success,
            sol_amount_lamports=sol_amount_lamports,
            expected_tokens=expected_tokens,
            slippage_bps_cap=slippage_bps,
            units_consumed=sim.units_consumed,
            err=sim.err,
            logs=sim.logs,
        )

    async def _sell_inner(
        self,
        mint: Pubkey | str,
        token_amount_raw: int,
        slippage_bps: int,
        priority_fee_microlamports_per_cu: int,
        submit_live: bool,
    ) -> PumpExecuteResult:
        mint_pk = mint if isinstance(mint, Pubkey) else Pubkey.from_string(mint)
        state = await self._rpc.fetch_bonding_curve_state(mint_pk)
        if state is None:
            return PumpExecuteResult(
                side="sell",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                slippage_bps_cap=slippage_bps,
                err="bonding_curve_account_missing",
            )
        blockhash = await self._rpc.get_latest_blockhash()
        if blockhash is None:
            return PumpExecuteResult(
                side="sell",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                slippage_bps_cap=slippage_bps,
                err="blockhash_fetch_failed",
            )
        try:
            tx = build_sell_transaction(
                keypair=self._keypair,
                mint=mint_pk,
                token_amount_raw=token_amount_raw,
                state=state,
                recent_blockhash=blockhash,
                slippage_bps=slippage_bps,
                priority_fee_microlamports_per_cu=priority_fee_microlamports_per_cu,
            )
        except ValueError as exc:
            return PumpExecuteResult(
                side="sell",
                mint=str(mint_pk),
                submitted_live=submit_live,
                success=False,
                slippage_bps_cap=slippage_bps,
                err=f"tx_build_failed:{exc}",
            )
        expected_sol = estimate_sell_output_lamports(token_amount_raw, state)
        b64 = serialize_signed_tx_base64(tx)
        if submit_live:
            raise NotImplementedError("live submit path not yet implemented")
        sim = await self._rpc.simulate_transaction(b64)
        return PumpExecuteResult(
            side="sell",
            mint=str(mint_pk),
            submitted_live=False,
            success=sim.success,
            expected_sol_out_lamports=expected_sol,
            slippage_bps_cap=slippage_bps,
            units_consumed=sim.units_consumed,
            err=sim.err,
            logs=sim.logs,
        )


def estimate_sell_output_lamports(
    token_amount_raw: int,
    state: BondingCurveState,
    fee_bps: int = PUMPFUN_TRADE_FEE_BPS,
) -> int:
    """Inverse of buy: given tokens to sell, how many lamports out.

        new_v_tokens   = v_tokens + tokens_in
        new_v_sol      = v_sol × v_tokens / new_v_tokens
        sol_out_pre_fee = v_sol − new_v_sol
        sol_out         = sol_out_pre_fee × (1 − fee)
    """
    if token_amount_raw <= 0:
        return 0
    if state.complete:
        return 0
    new_v_tokens = state.virtual_token_reserves + token_amount_raw
    new_v_sol = (state.virtual_sol_reserves * state.virtual_token_reserves) // new_v_tokens
    sol_out_pre_fee = state.virtual_sol_reserves - new_v_sol
    fee_factor_num = 10_000 - fee_bps
    sol_out = (sol_out_pre_fee * fee_factor_num) // 10_000
    return max(0, min(sol_out, state.real_sol_reserves))


# ── Account-list builders (instruction needs accounts in exact order) ─
@dataclass
class PumpFunSellAccounts:
    """Account ordering for the sell instruction.

    Same as buy, minus the rent sysvar. Verified against decoded
    sell txs.
    """

    global_pda: Pubkey
    fee_recipient: Pubkey
    mint: Pubkey
    bonding_curve: Pubkey
    associated_bonding_curve: Pubkey
    associated_user: Pubkey
    user: Pubkey
    system_program: Pubkey
    associated_token_program: Pubkey
    token_program: Pubkey
    event_authority: Pubkey
    program: Pubkey

    @classmethod
    def for_user_and_mint(cls, user: Pubkey, mint: Pubkey) -> "PumpFunSellAccounts":
        bonding = derive_bonding_curve_pda(mint)
        return cls(
            global_pda=derive_global_pda(),
            fee_recipient=PUMPFUN_FEE_RECIPIENT,
            mint=mint,
            bonding_curve=bonding,
            associated_bonding_curve=derive_associated_token_account(bonding, mint),
            associated_user=derive_associated_token_account(user, mint),
            user=user,
            system_program=SYSTEM_PROGRAM_ID,
            associated_token_program=ATA_PROGRAM_ID,
            token_program=TOKEN_PROGRAM_ID,
            event_authority=derive_event_authority_pda(),
            program=PUMPFUN_PROGRAM_ID,
        )
