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

import asyncio
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
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
# Pump.fun has two fee_recipient pools, picked by coin's mayhem flag:
#   * normal (Global.fee_recipient + fee_recipients[7]) — for standard coins
#   * reserved (Global.reserved_fee_recipient + reserved_fee_recipients[7]) — for mayhem coins
# Picking the wrong pool reverts with 6000 NotAuthorized at fee_recipient.rs:19.
PUMPFUN_NORMAL_FEE_RECIPIENT = Pubkey.from_string(
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"  # Global.fee_recipient (primary)
)
PUMPFUN_RESERVED_FEE_RECIPIENT = Pubkey.from_string(
    "8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6"  # reserved_fee_recipients[3]
)
# Backwards-compatible alias — call sites that don't know mayhem yet.
PUMPFUN_FEE_RECIPIENT = PUMPFUN_NORMAL_FEE_RECIPIENT


def pick_fee_recipient(is_mayhem_mode: bool) -> Pubkey:
    """Return the right fee_recipient for the coin's mayhem mode.

    Pump.fun's `fee_recipient.rs:19` check requires that the recipient
    we pass is in the pool matching the coin's mayhem flag — wrong
    pool → on-chain revert with custom error 6000 (NotAuthorized).
    """
    if is_mayhem_mode:
        return PUMPFUN_RESERVED_FEE_RECIPIENT
    return PUMPFUN_NORMAL_FEE_RECIPIENT


# Fee program — pump.fun delegates fee accounting to a separate
# program (pfeeUx...). Lives in slot [15] of buy / [13] of sell.
FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
# 32-byte constant seed for the fee_config PDA. Hardcoded in the
# on-chain program; pulled from the IDL. Equivalent to
# bytes(PUMPFUN_PROGRAM_ID).
FEE_CONFIG_CONST_SEED = bytes(
    [
        1,
        86,
        224,
        246,
        147,
        102,
        90,
        207,
        68,
        219,
        21,
        104,
        191,
        23,
        91,
        170,
        81,
        137,
        203,
        151,
        245,
        210,
        255,
        59,
        101,
        93,
        43,
        182,
        253,
        109,
        24,
        176,
    ]
)
# Buyback fee recipients — eight of them in pump.fun's Global state.
# Caller selects one for each buy/sell. Must match a recipient in the
# `Global.buyback_fee_recipients` array; pump.fun rotates this set, so
# pull from Global if a recipient becomes invalid.
PUMPFUN_BUYBACK_FEE_RECIPIENTS = [
    Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    Pubkey.from_string("9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7"),
    Pubkey.from_string("GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    Pubkey.from_string("3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR"),
    Pubkey.from_string("5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"),
    Pubkey.from_string("EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    Pubkey.from_string("5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    Pubkey.from_string("A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW"),
]
# Default pick — index [1] which has been observed in the highest
# share of recent mainnet buys (rotation should be bot-side; using
# index 0 in production sweeps would be fine too).
PUMPFUN_DEFAULT_BUYBACK_FEE_RECIPIENT = PUMPFUN_BUYBACK_FEE_RECIPIENTS[1]
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
# Token-2022 program — pump.fun (as of 2026) mints all tokens under
# Token-2022, NOT the legacy SPL Token program. Verified empirically
# 2026-05-08: 10/10 sampled mints owned by Token-2022. Using legacy
# Token program produces ``IncorrectProgramId`` at simulation time
# because the ATA derivation seed differs and the on-chain ATA
# program rejects the mismatch.
TOKEN_2022_PROGRAM_ID = Pubkey.from_string(
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
)
ATA_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

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


def derive_bonding_curve_v2_pda(mint: Pubkey) -> Pubkey:
    """Derive the bonding-curve-v2 PDA. Per the official pump-rust-client
    SDK, this is appended as a remaining_account on legacy buy/sell —
    seed = ['bonding-curve-v2', mint] under the pump.fun program.
    """
    pda, _bump = Pubkey.find_program_address(
        [b"bonding-curve-v2", bytes(mint)],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_creator_vault_pda(creator: Pubkey) -> Pubkey:
    """Derive the creator-vault PDA. Seed = ['creator-vault', creator].

    The creator is the wallet that originally minted the token. It is
    stored in the bonding-curve account state — pass
    ``BondingCurveState.creator`` here, NOT the user wallet.
    """
    pda, _bump = Pubkey.find_program_address(
        [b"creator-vault", bytes(creator)],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_global_volume_accumulator_pda() -> Pubkey:
    """Derive the program-wide volume accumulator PDA (constant)."""
    pda, _bump = Pubkey.find_program_address(
        [b"global_volume_accumulator"],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_user_volume_accumulator_pda(user: Pubkey) -> Pubkey:
    """Derive the per-user volume accumulator PDA. Created on a
    wallet's first buy, holds aggregated trade volume for that user."""
    pda, _bump = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)],
        PUMPFUN_PROGRAM_ID,
    )
    return pda


def derive_fee_config_pda() -> Pubkey:
    """Derive the fee-config PDA. Lives under FEE_PROGRAM_ID, NOT
    the main pump.fun program. Constant per program version."""
    pda, _bump = Pubkey.find_program_address(
        [b"fee_config", FEE_CONFIG_CONST_SEED],
        FEE_PROGRAM_ID,
    )
    return pda


def derive_associated_token_account(
    owner: Pubkey,
    mint: Pubkey,
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
) -> Pubkey:
    """Derive the ATA (Associated Token Account) for an owner+mint.

    Seed: ``[owner, token_program, mint]`` under ``ATA_PROGRAM_ID``.

    ``token_program`` defaults to **Token-2022** because that's what
    pump.fun mints use as of 2026 (verified by sampling on-chain).
    Pass ``TOKEN_PROGRAM_ID`` explicitly for legacy SPL-Token mints.
    """
    pda, _bump = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ATA_PROGRAM_ID,
    )
    return pda


# ── Instruction data encoding ──────────────────────────────────
def encode_buy_instruction_data(
    token_amount_raw: int,
    max_sol_cost_lamports: int,
) -> bytes:
    """Pack the buy-instruction data payload.

    Layout (Anchor convention) — **24 bytes**:
        [0..8]   discriminator (8 bytes, fixed)
        [8..16]  amount (u64 LE) — token amount we want, raw integer
        [16..24] max_sol_cost (u64 LE) — slippage cap in lamports

    2026-05-14: the previous code appended a 25th ``track_volume`` byte.
    A decoded reference buy from the *current* on-chain program (slot
    419594334, sig af4miEhv…) is exactly 24 bytes with no trailing byte
    — the 25th byte was the "protocol-version mismatch" that got every
    direct buy rejected. The current program no longer takes the
    OptionBool arg on the legacy ``buy`` discriminator.
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
    """Account ordering for the buy instruction (16 accounts).

    Order matches the canonical on-chain IDL (pulled live from the
    program's IDL PDA). Slot meanings:
        [ 0] global                    — PDA(['global'])
        [ 1] fee_recipient             — input (program maintains a list)
        [ 2] mint
        [ 3] bonding_curve             — PDA(['bonding-curve', mint])
        [ 4] associated_bonding_curve  — ATA(bonding, token_program, mint)
        [ 5] associated_user           — ATA(user, token_program, mint)
        [ 6] user                      — signer, mut
        [ 7] system_program
        [ 8] token_program             — Token-2022
        [ 9] creator_vault             — PDA(['creator-vault', creator])
        [10] event_authority           — PDA(['__event_authority'])
        [11] program                   — self
        [12] global_volume_accumulator — PDA(['global_volume_accumulator'])
        [13] user_volume_accumulator   — PDA(['user_volume_accumulator', user])
        [14] fee_config                — PDA under FEE_PROGRAM
        [15] fee_program               — pfeeUx...
    """

    global_pda: Pubkey  # [ 0]
    fee_recipient: Pubkey  # [ 1]
    mint: Pubkey  # [ 2]
    bonding_curve: Pubkey  # [ 3]
    associated_bonding_curve: Pubkey  # [ 4]
    associated_user: Pubkey  # [ 5]
    user: Pubkey  # [ 6]
    system_program: Pubkey  # [ 7]
    token_program: Pubkey  # [ 8]
    creator_vault: Pubkey  # [ 9]
    event_authority: Pubkey  # [10]
    program: Pubkey  # [11]
    global_volume_accumulator: Pubkey  # [12]
    user_volume_accumulator: Pubkey  # [13]
    fee_config: Pubkey  # [14]
    fee_program: Pubkey  # [15]
    bonding_curve_v2: Pubkey  # [16] — IDL-undocumented remaining account
    buyback_fee_recipient: Pubkey  # [17] — IDL-undocumented remaining account

    @classmethod
    def for_user_mint_creator(
        cls,
        user: Pubkey,
        mint: Pubkey,
        creator: Pubkey,
        fee_recipient: Pubkey | None = None,
        buyback_fee_recipient: Pubkey | None = None,
        token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
    ) -> "PumpFunBuyAccounts":
        """Build a complete 18-account list (16 from IDL + 2 remaining
        accounts not in the public IDL: ``bonding_curve_v2`` and
        ``buyback_fee_recipient``).

        ``creator`` MUST come from the bonding-curve account state
        (``BondingCurveState.creator``), not the user wallet —
        creator-vault PDA is derived from it.

        ``token_program`` controls both the ATA derivation seed and the
        ``token_program`` account slot. Pass the actual mint owner —
        Token-2022 for most pump.fun mints, legacy SPL Token for the
        minority that use it. Wrong choice → ``IncorrectProgramId``.

        ``fee_recipient`` and ``buyback_fee_recipient`` must be in
        pump.fun's Global state recipient lists (see
        ``PUMPFUN_BUYBACK_FEE_RECIPIENTS`` for the 8 valid buyback
        recipients).
        """
        bonding = derive_bonding_curve_pda(mint)
        return cls(
            global_pda=derive_global_pda(),
            fee_recipient=fee_recipient or PUMPFUN_FEE_RECIPIENT,
            mint=mint,
            bonding_curve=bonding,
            associated_bonding_curve=derive_associated_token_account(
                bonding, mint, token_program=token_program
            ),
            associated_user=derive_associated_token_account(
                user, mint, token_program=token_program
            ),
            user=user,
            system_program=SYSTEM_PROGRAM_ID,
            token_program=token_program,
            creator_vault=derive_creator_vault_pda(creator),
            event_authority=derive_event_authority_pda(),
            program=PUMPFUN_PROGRAM_ID,
            global_volume_accumulator=derive_global_volume_accumulator_pda(),
            user_volume_accumulator=derive_user_volume_accumulator_pda(user),
            fee_config=derive_fee_config_pda(),
            fee_program=FEE_PROGRAM_ID,
            bonding_curve_v2=derive_bonding_curve_v2_pda(mint),
            buyback_fee_recipient=(
                buyback_fee_recipient or PUMPFUN_DEFAULT_BUYBACK_FEE_RECIPIENT
            ),
        )


# ── Bonding-curve state ───────────────────────────────────────
@dataclass
class BondingCurveState:
    """On-chain bonding-curve account state.

    Account layout (Anchor) per pump-rust-client IDL:
        [0..8]    discriminator
        [8..16]   virtual_token_reserves (u64 LE)
        [16..24]  virtual_quote_reserves (u64 LE) — formerly virtual_sol_reserves
        [24..32]  real_token_reserves (u64 LE)
        [32..40]  real_quote_reserves (u64 LE) — formerly real_sol_reserves
        [40..48]  token_total_supply (u64 LE)
        [48]      complete (bool) — true after graduation
        [49..81]  creator (Pubkey, 32B) — drives creator_vault PDA
        [81]      is_mayhem_mode (bool) — selects normal vs reserved fee_recipient
        [82]      is_cashback_coin (bool) — sell appends UVA when true
        [83..115] quote_mint (Pubkey, 32B) — SOL for legacy coins, USDC for some
    """

    virtual_token_reserves: int
    virtual_sol_reserves: int  # legacy alias for virtual_quote_reserves
    real_token_reserves: int
    real_sol_reserves: int  # legacy alias for real_quote_reserves
    token_total_supply: int
    complete: bool
    creator: Pubkey
    is_mayhem_mode: bool = False
    is_cashback_coin: bool = True
    quote_mint: Pubkey | None = None

    @classmethod
    def from_account_data(cls, data: bytes) -> "BondingCurveState":
        """Parse the 8-byte discriminator + Anchor-encoded fields.

        Older bonding-curve accounts (pre-mayhem) only have 81 bytes
        (no mayhem/cashback/quote_mint fields). Newer ones have 115+.
        We tolerate the legacy length and default the new flags so
        the parser keeps working on stale accounts.
        """
        if len(data) < 81:
            raise ValueError(
                f"bonding-curve account too short: {len(data)} bytes (need >=81)"
            )
        body = data[8:]
        v_tok, v_sol, r_tok, r_sol, supply = struct.unpack_from("<QQQQQ", body, 0)
        complete = bool(body[40])
        creator = Pubkey(body[41:73])
        # New fields (post-mayhem rollout). Default safely if account is short.
        is_mayhem_mode = bool(body[73]) if len(body) >= 74 else False
        is_cashback_coin = bool(body[74]) if len(body) >= 75 else True
        quote_mint: Pubkey | None = None
        if len(body) >= 107:
            qm_bytes = body[75:107]
            # All-zero quote_mint means default (= SOL).
            if qm_bytes != bytes(32):
                quote_mint = Pubkey(qm_bytes)
        return cls(
            virtual_token_reserves=v_tok,
            virtual_sol_reserves=v_sol,
            real_token_reserves=r_tok,
            real_sol_reserves=r_sol,
            token_total_supply=supply,
            complete=complete,
            creator=creator,
            is_mayhem_mode=is_mayhem_mode,
            is_cashback_coin=is_cashback_coin,
            quote_mint=quote_mint,
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
    new_v_tokens = (
        state.virtual_sol_reserves * state.virtual_token_reserves
    ) // new_v_sol
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
        self._base_url = base_url or "https://mainnet.helius-rpc.com"
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

    async def fetch_bonding_curve_state(self, mint: Pubkey) -> BondingCurveState | None:
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
                pda,
                mint,
                exc,
            )
            return None

    async def fetch_mint_token_program(self, mint: Pubkey) -> Pubkey:
        """Return the token-program owner of the mint account.

        pump.fun mints are *usually* Token-2022, but a non-trivial
        minority are legacy SPL Token. Building the buy/sell tx with
        the wrong program triggers an ``IncorrectProgramId`` revert at
        ATA-derivation (different seeds: ``[owner, token_program,
        mint]``).

        Falls back to Token-2022 on RPC failure — that matches the
        pre-fix behaviour and is correct for the majority of mints.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                str(mint),
                {"encoding": "base64", "commitment": "processed"},
            ],
        }
        try:
            resp = await self._post(payload)
        except Exception as exc:
            logger.warning(
                "fetch_mint_token_program(%s) failed: %s — defaulting to Token-2022",
                mint,
                exc,
            )
            return TOKEN_2022_PROGRAM_ID
        value = resp.get("result", {}).get("value")
        if not value:
            return TOKEN_2022_PROGRAM_ID
        owner_str = value.get("owner")
        if not owner_str:
            return TOKEN_2022_PROGRAM_ID
        try:
            owner_pk = Pubkey.from_string(owner_str)
        except Exception:
            return TOKEN_2022_PROGRAM_ID
        if owner_pk == TOKEN_PROGRAM_ID:
            return TOKEN_PROGRAM_ID
        return TOKEN_2022_PROGRAM_ID

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
            return SimulateResult(
                success=False, err=f"rpc_post_failed:{type(exc).__name__}:{exc}"
            )
        if resp.get("error"):
            return SimulateResult(success=False, err=f"rpc_error:{resp['error']}")
        result = resp.get("result", {}).get("value", {})
        err = result.get("err")
        return SimulateResult(
            success=(err is None),
            err=err,
            logs=result.get("logs"),
            units_consumed=result.get("unitsConsumed"),
            raw_response=resp,
        )

    async def send_transaction(
        self,
        signed_tx_base64: str,
        *,
        skip_preflight: bool = False,
        max_retries: int = 0,
    ) -> str:
        """Submit a signed transaction. Returns the signature on
        success; raises on RPC error.

        ``skip_preflight=False`` keeps Helius's simulate-then-submit
        guard, which catches account-list bugs before broadcasting
        and saves the priority fee on definite-fail txs.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "preflightCommitment": "processed",
                    "maxRetries": max_retries,
                },
            ],
        }
        resp = await self._post(payload, timeout_sec=15.0)
        if "error" in resp:
            raise RuntimeError(f"sendTransaction RPC error: {resp['error']}")
        sig = resp.get("result")
        if not sig:
            raise RuntimeError(f"sendTransaction returned no signature: {resp}")
        return sig

    async def get_signature_statuses(self, sigs: list[str]) -> list[dict | None]:
        """Look up confirmation statuses for an array of signatures."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [sigs, {"searchTransactionHistory": False}],
        }
        resp = await self._post(payload, timeout_sec=10.0)
        return resp.get("result", {}).get("value", [None] * len(sigs))

    async def confirm_signature(
        self,
        sig: str,
        *,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 1.0,
    ) -> dict:
        """Poll ``getSignatureStatuses`` until the tx is confirmed
        (or until timeout). Returns the final status dict; the caller
        inspects ``confirmationStatus`` and ``err``."""
        import time

        deadline = time.monotonic() + timeout_sec
        last: dict | None = None
        while time.monotonic() < deadline:
            (last,) = await self.get_signature_statuses([sig])
            if last is not None:
                cs = last.get("confirmationStatus")
                if cs in ("confirmed", "finalized"):
                    return last
                if last.get("err"):
                    return last
            await asyncio.sleep(poll_interval_sec)
        return last or {"err": "confirmation_timeout"}


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
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
) -> Instruction:
    """Associated Token Account program: create-if-not-exists.

    Delegates to the canonical SPL helper. ``token_program`` defaults
    to Token-2022 (pump.fun's typical choice) but legacy SPL Token
    mints exist too — pass the actual mint owner program here.
    """
    from spl.token.instructions import (  # type: ignore
        create_idempotent_associated_token_account,
    )

    return create_idempotent_associated_token_account(
        payer=payer,
        owner=owner,
        mint=mint,
        token_program_id=token_program,
    )


def build_pump_buy_ix(
    user: Pubkey,
    mint: Pubkey,
    creator: Pubkey,
    token_amount_raw: int,
    max_sol_cost_lamports: int,
    fee_recipient: Pubkey | None = None,
    buyback_fee_recipient: Pubkey | None = None,
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
) -> Instruction:
    """Build the pump.fun buy instruction (18 accounts, 24-byte data).

    16 accounts come from the on-chain IDL; pump.fun's program also
    consumes 2 trailing remaining_accounts that aren't in the IDL:
    ``bonding_curve_v2`` (readonly) and ``buyback_fee_recipient``
    (writable). Per the official ``pump-rust-client`` SDK
    (`buy_instruction` in `pump_legacy.rs`), this is the canonical
    layout for buys against the legacy bonding-curve discriminator.

    ``creator`` MUST come from the bonding-curve account state
    (``BondingCurveState.creator``); it drives the creator-vault PDA.
    """
    accounts_obj = PumpFunBuyAccounts.for_user_mint_creator(
        user,
        mint,
        creator,
        fee_recipient=fee_recipient,
        buyback_fee_recipient=buyback_fee_recipient,
        token_program=token_program,
    )
    # IDL signer/mut flags (slots 0..15): mut at [1,3,4,5,6,9,13],
    # signer only at [6]. Trailing remaining_accounts: [16] readonly,
    # [17] writable per pump-rust-client SDK source.
    accounts = [
        AccountMeta(pubkey=accounts_obj.global_pda, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.fee_recipient, is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=accounts_obj.mint, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.bonding_curve, is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=accounts_obj.associated_bonding_curve,
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(
            pubkey=accounts_obj.associated_user, is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=accounts_obj.user, is_signer=True, is_writable=True),
        AccountMeta(
            pubkey=accounts_obj.system_program, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.token_program, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.creator_vault, is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=accounts_obj.event_authority, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=accounts_obj.program, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.global_volume_accumulator,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=accounts_obj.user_volume_accumulator,
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(pubkey=accounts_obj.fee_config, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.fee_program, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.bonding_curve_v2, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.buyback_fee_recipient, is_signer=False, is_writable=True
        ),
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
    creator: Pubkey,
    token_amount_raw: int,
    min_sol_output_lamports: int,
    is_cashback_coin: bool = True,
    fee_recipient: Pubkey | None = None,
    buyback_fee_recipient: Pubkey | None = None,
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
) -> Instruction:
    """Build the pump.fun sell instruction (16 or 17 accounts).

    14 IDL accounts + 2-3 trailing remaining_accounts:
      * if ``is_cashback_coin`` is True: append user_volume_accumulator
      * always: append ``bonding_curve_v2`` (readonly) and
        ``buyback_fee_recipient`` (writable)

    Per pump-rust-client SDK source (`pump_legacy.rs::sell_instruction`).
    Default is cashback=True since pump.fun's Global has
    ``is_cashback_enabled=1`` (verified 2026-05-08).

    Sell IDL slot ordering DIFFERS from buy:
      * creator_vault at [8] (buy has it at [9])
      * token_program at [9] (buy has it at [8])
    """
    accounts_obj = PumpFunSellAccounts.for_user_mint_creator(
        user,
        mint,
        creator,
        fee_recipient=fee_recipient,
        buyback_fee_recipient=buyback_fee_recipient,
        token_program=token_program,
    )
    # IDL signer/mut flags (slots 0..13): mut at [1,3,4,5,6,8],
    # signer only at [6]. All other IDL slots ro/non-signer.
    accounts = [
        AccountMeta(pubkey=accounts_obj.global_pda, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.fee_recipient, is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=accounts_obj.mint, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.bonding_curve, is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=accounts_obj.associated_bonding_curve,
            is_signer=False,
            is_writable=True,
        ),
        AccountMeta(
            pubkey=accounts_obj.associated_user, is_signer=False, is_writable=True
        ),
        AccountMeta(pubkey=accounts_obj.user, is_signer=True, is_writable=True),
        AccountMeta(
            pubkey=accounts_obj.system_program, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.creator_vault, is_signer=False, is_writable=True
        ),
        AccountMeta(
            pubkey=accounts_obj.token_program, is_signer=False, is_writable=False
        ),
        AccountMeta(
            pubkey=accounts_obj.event_authority, is_signer=False, is_writable=False
        ),
        AccountMeta(pubkey=accounts_obj.program, is_signer=False, is_writable=False),
        AccountMeta(pubkey=accounts_obj.fee_config, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=accounts_obj.fee_program, is_signer=False, is_writable=False
        ),
    ]
    if is_cashback_coin:
        accounts.append(
            AccountMeta(
                pubkey=accounts_obj.user_volume_accumulator,
                is_signer=False,
                is_writable=True,
            )
        )
    accounts.extend(
        [
            AccountMeta(
                pubkey=accounts_obj.bonding_curve_v2, is_signer=False, is_writable=False
            ),
            AccountMeta(
                pubkey=accounts_obj.buyback_fee_recipient,
                is_signer=False,
                is_writable=True,
            ),
        ]
    )
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
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
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
    max_sol_cost = (sol_amount_lamports * (10_000 + slippage_bps)) // 10_000
    instructions = [
        build_set_compute_unit_limit_ix(compute_unit_limit),
        build_set_compute_unit_price_ix(priority_fee_microlamports_per_cu),
        build_create_ata_idempotent_ix(
            payer=user, owner=user, mint=mint, token_program=token_program
        ),
        build_pump_buy_ix(
            user=user,
            mint=mint,
            creator=state.creator,
            token_amount_raw=expected_tokens,
            max_sol_cost_lamports=max_sol_cost,
            fee_recipient=pick_fee_recipient(state.is_mayhem_mode),
            token_program=token_program,
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
    token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
) -> VersionedTransaction:
    """Build a sell transaction. Same pattern as buy, no ATA-create
    (we already own the ATA from the prior buy)."""
    user = keypair.pubkey()
    expected_sol = estimate_sell_output_lamports(token_amount_raw, state)
    if expected_sol <= 0:
        raise ValueError(
            "sell would yield zero SOL — bonding curve complete or " "amount too small"
        )
    # Slippage on sell goes the OTHER direction: minimum we'll accept.
    min_sol_output = (expected_sol * (10_000 - slippage_bps)) // 10_000
    instructions = [
        build_set_compute_unit_limit_ix(compute_unit_limit),
        build_set_compute_unit_price_ix(priority_fee_microlamports_per_cu),
        build_pump_sell_ix(
            user=user,
            mint=mint,
            creator=state.creator,
            token_amount_raw=token_amount_raw,
            min_sol_output_lamports=min_sol_output,
            is_cashback_coin=state.is_cashback_coin,
            fee_recipient=pick_fee_recipient(state.is_mayhem_mode),
            token_program=token_program,
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
    signature: str | None = None  # populated only on live submit


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

        ``SOL_WALLET_KEYPAIR`` accepts three formats:
          1. **Path to a JSON keypair file** (Solana CLI convention,
             e.g. ``~/.config/solana/id.json``) — file contains a
             JSON array of 64 byte values.
          2. **JSON array string** (the same content, inline).
          3. **base58 secret-key string** (Phantom export format).

        Detection is by-shape: starts with ``[`` → JSON array;
        ends with ``.json`` → file path; else assume base58.
        """
        import json as _json
        import os as _os

        from solders.keypair import Keypair  # type: ignore

        helius_key = _os.environ.get("HELIUS_API_KEY", "")
        kp_value = _os.environ.get("SOL_WALLET_KEYPAIR", "").strip()
        if not helius_key:
            raise RuntimeError("HELIUS_API_KEY env not set")
        if not kp_value:
            raise RuntimeError("SOL_WALLET_KEYPAIR env not set")

        kp = cls._load_keypair_any(kp_value, _json, Keypair)
        return cls(
            rpc=HeliusRpc(api_key=helius_key),
            keypair=kp,
            allow_live_submit=allow_live_submit,
        )

    @staticmethod
    def _load_keypair_any(value: str, json_module: Any, KeypairCls: Any) -> Any:
        """Detect keypair encoding and load. Raises with a clear
        message on malformed input — easier to debug than the cryptic
        ``InvalidChar`` from the bare base58 path."""
        # 1. JSON array inline.
        if value.startswith("[") and value.endswith("]"):
            try:
                arr = json_module.loads(value)
            except Exception as exc:
                raise RuntimeError(
                    f"SOL_WALLET_KEYPAIR looks like JSON but failed to parse: {exc}"
                ) from exc
            return KeypairCls.from_bytes(bytes(arr))
        # 2. File path (Solana CLI convention).
        if value.endswith(".json") or "/" in value or "\\" in value:
            from pathlib import Path

            p = Path(value).expanduser()
            if not p.exists():
                # Resolve relative to the gg/ repo if not absolute.
                alt = Path(__file__).resolve().parents[1] / value
                if alt.exists():
                    p = alt
                else:
                    raise RuntimeError(
                        f"SOL_WALLET_KEYPAIR points to a file that doesn't exist: {value}"
                    )
            try:
                arr = json_module.loads(p.read_text())
            except Exception as exc:
                raise RuntimeError(
                    f"SOL_WALLET_KEYPAIR file {p} is not valid JSON: {exc}"
                ) from exc
            return KeypairCls.from_bytes(bytes(arr))
        # 3. Fall through: assume base58.
        try:
            return KeypairCls.from_base58_string(value)
        except Exception as exc:
            raise RuntimeError(
                f"SOL_WALLET_KEYPAIR is not a recognised format "
                f"(JSON array / .json file path / base58 string): {exc}"
            ) from exc

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
        # 1. Fetch on-chain bonding-curve state + detect mint's token
        #    program (Token-2022 vs legacy SPL). Wrong program at the
        #    ATA seed → IncorrectProgramId revert.
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
        token_program = await self._rpc.fetch_mint_token_program(mint_pk)
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
                token_program=token_program,
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
            try:
                signature = await self._rpc.send_transaction(b64, skip_preflight=False)
            except Exception as exc:
                return PumpExecuteResult(
                    side="buy",
                    mint=str(mint_pk),
                    submitted_live=True,
                    success=False,
                    sol_amount_lamports=sol_amount_lamports,
                    expected_tokens=expected_tokens,
                    slippage_bps_cap=slippage_bps,
                    err=f"send_failed:{exc}",
                )
            status = await self._rpc.confirm_signature(signature)
            err = status.get("err")
            return PumpExecuteResult(
                side="buy",
                mint=str(mint_pk),
                submitted_live=True,
                success=(err is None),
                sol_amount_lamports=sol_amount_lamports,
                expected_tokens=expected_tokens,
                slippage_bps_cap=slippage_bps,
                err=err,
                signature=signature,
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
        token_program = await self._rpc.fetch_mint_token_program(mint_pk)
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
                token_program=token_program,
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
            try:
                signature = await self._rpc.send_transaction(b64, skip_preflight=False)
            except Exception as exc:
                return PumpExecuteResult(
                    side="sell",
                    mint=str(mint_pk),
                    submitted_live=True,
                    success=False,
                    expected_sol_out_lamports=expected_sol,
                    slippage_bps_cap=slippage_bps,
                    err=f"send_failed:{exc}",
                )
            status = await self._rpc.confirm_signature(signature)
            err = status.get("err")
            return PumpExecuteResult(
                side="sell",
                mint=str(mint_pk),
                submitted_live=True,
                success=(err is None),
                expected_sol_out_lamports=expected_sol,
                slippage_bps_cap=slippage_bps,
                err=err,
                signature=signature,
            )
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
    new_v_sol = (
        state.virtual_sol_reserves * state.virtual_token_reserves
    ) // new_v_tokens
    sol_out_pre_fee = state.virtual_sol_reserves - new_v_sol
    fee_factor_num = 10_000 - fee_bps
    sol_out = (sol_out_pre_fee * fee_factor_num) // 10_000
    return max(0, min(sol_out, state.real_sol_reserves))


# ── Account-list builders (instruction needs accounts in exact order) ─
@dataclass
class PumpFunSellAccounts:
    """Account ordering for the sell instruction (14 accounts).

    Order matches the on-chain IDL. Note slot ordering DIFFERS from
    buy:
      * creator_vault is at slot [8] (was [9] in buy)
      * token_program is at slot [9] (was [8] in buy)
      * No volume_accumulator slots (those are buy-only)
    """

    global_pda: Pubkey  # [ 0]
    fee_recipient: Pubkey  # [ 1]
    mint: Pubkey  # [ 2]
    bonding_curve: Pubkey  # [ 3]
    associated_bonding_curve: Pubkey  # [ 4]
    associated_user: Pubkey  # [ 5]
    user: Pubkey  # [ 6]
    system_program: Pubkey  # [ 7]
    creator_vault: Pubkey  # [ 8]
    token_program: Pubkey  # [ 9]
    event_authority: Pubkey  # [10]
    program: Pubkey  # [11]
    fee_config: Pubkey  # [12]
    fee_program: Pubkey  # [13]
    # Trailing remaining_accounts (not in IDL). For cashback-enabled
    # coins, ``user_volume_accumulator`` is appended first; then
    # ``bonding_curve_v2`` and ``buyback_fee_recipient`` always follow.
    user_volume_accumulator: Pubkey  # [14] when cashback (else skipped)
    bonding_curve_v2: Pubkey  # [14 or 15]
    buyback_fee_recipient: Pubkey  # [15 or 16]

    @classmethod
    def for_user_mint_creator(
        cls,
        user: Pubkey,
        mint: Pubkey,
        creator: Pubkey,
        fee_recipient: Pubkey | None = None,
        buyback_fee_recipient: Pubkey | None = None,
        token_program: Pubkey = TOKEN_2022_PROGRAM_ID,
    ) -> "PumpFunSellAccounts":
        bonding = derive_bonding_curve_pda(mint)
        return cls(
            global_pda=derive_global_pda(),
            fee_recipient=fee_recipient or PUMPFUN_FEE_RECIPIENT,
            mint=mint,
            bonding_curve=bonding,
            associated_bonding_curve=derive_associated_token_account(
                bonding, mint, token_program=token_program
            ),
            associated_user=derive_associated_token_account(
                user, mint, token_program=token_program
            ),
            user=user,
            system_program=SYSTEM_PROGRAM_ID,
            creator_vault=derive_creator_vault_pda(creator),
            token_program=token_program,
            event_authority=derive_event_authority_pda(),
            program=PUMPFUN_PROGRAM_ID,
            fee_config=derive_fee_config_pda(),
            fee_program=FEE_PROGRAM_ID,
            user_volume_accumulator=derive_user_volume_accumulator_pda(user),
            bonding_curve_v2=derive_bonding_curve_v2_pda(mint),
            buyback_fee_recipient=(
                buyback_fee_recipient or PUMPFUN_DEFAULT_BUYBACK_FEE_RECIPIENT
            ),
        )
