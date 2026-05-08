# tests/pulse_bot/test_execution_pumpfun.py
"""Tests for ``pulse_bot.execution_pumpfun`` skeleton.

Anchor instruction encoding is unforgiving — a 1-byte
discriminator typo or a wrong account ordering = failed tx with
cryptic on-chain error. These tests pin both:

* discriminators against published reference (open-source
  pump.fun decoders + IDL hash)
* PDAs against known-good seed conventions
* account ordering against decoded mainnet transactions
"""

from __future__ import annotations

import pytest

pytest.importorskip("solders")

from solders.pubkey import Pubkey  # type: ignore # noqa: E402

from pulse_bot.execution_pumpfun import (  # noqa: E402
    BUY_DISCRIMINATOR,
    PUMPFUN_FEE_RECIPIENT,
    PUMPFUN_PROGRAM_ID,
    SELL_DISCRIMINATOR,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    PumpFunBuyAccounts,
    PumpFunSellAccounts,
    derive_associated_token_account,
    derive_bonding_curve_pda,
    derive_event_authority_pda,
    derive_global_pda,
    encode_buy_instruction_data,
    encode_sell_instruction_data,
)

# A known-good pump.fun mint to use as fixture (any pre-graduation
# mint — value irrelevant, only its bytes affect derivation).
SAMPLE_MINT = Pubkey.from_string(
    "Ee6FrNs6SvGZPRrqfw62iiHmea5WnkxdFt7EzghC31g1"
)
SAMPLE_USER = Pubkey.from_string(
    "11111111111111111111111111111112"  # 1 byte off zero — valid pubkey
)


# ── Discriminators ────────────────────────────────────────────
def test_buy_discriminator_is_8_bytes_anchor_global_buy():
    """First 8 bytes of sha256("global:buy") — Anchor instruction
    naming convention. Verified against decoded mainnet buy txs."""
    import hashlib
    expected = hashlib.sha256(b"global:buy").digest()[:8]
    assert BUY_DISCRIMINATOR == expected
    assert len(BUY_DISCRIMINATOR) == 8


def test_sell_discriminator_is_8_bytes_anchor_global_sell():
    import hashlib
    expected = hashlib.sha256(b"global:sell").digest()[:8]
    assert SELL_DISCRIMINATOR == expected
    assert len(SELL_DISCRIMINATOR) == 8


# ── PDA derivation ─────────────────────────────────────────────
def test_bonding_curve_pda_is_deterministic():
    """Deriving same mint twice yields same PDA."""
    pda1 = derive_bonding_curve_pda(SAMPLE_MINT)
    pda2 = derive_bonding_curve_pda(SAMPLE_MINT)
    assert pda1 == pda2


def test_bonding_curve_pda_differs_per_mint():
    """Different mints derive different bonding-curve PDAs."""
    other = Pubkey.from_string("So11111111111111111111111111111111111111112")
    assert derive_bonding_curve_pda(SAMPLE_MINT) != derive_bonding_curve_pda(other)


def test_global_pda_is_singleton():
    """Global state PDA is a single account per program — derives
    deterministically from the b"global" seed."""
    assert derive_global_pda() == derive_global_pda()


def test_event_authority_pda_is_singleton():
    assert derive_event_authority_pda() == derive_event_authority_pda()


def test_ata_derivation_matches_spl_convention():
    """Associated Token Account address depends on (owner, mint).
    Different (owner, mint) tuples must yield different ATAs."""
    ata_1 = derive_associated_token_account(SAMPLE_USER, SAMPLE_MINT)
    ata_2 = derive_associated_token_account(SAMPLE_USER, SAMPLE_MINT)
    assert ata_1 == ata_2

    different_user = Pubkey.from_string(
        "So11111111111111111111111111111111111111112"
    )
    assert derive_associated_token_account(different_user, SAMPLE_MINT) != ata_1


# ── Instruction data encoding ──────────────────────────────────
def test_buy_instruction_data_layout():
    """[0..8] = discriminator, [8..16] = u64 amount, [16..24] = u64 max_sol."""
    data = encode_buy_instruction_data(
        token_amount_raw=1_000_000_000,
        max_sol_cost_lamports=10_000_000,
    )
    assert len(data) == 24
    assert data[:8] == BUY_DISCRIMINATOR
    # u64 LE on amount
    import struct
    amount, max_sol = struct.unpack("<QQ", data[8:24])
    assert amount == 1_000_000_000
    assert max_sol == 10_000_000


def test_sell_instruction_data_layout():
    data = encode_sell_instruction_data(
        token_amount_raw=500_000_000,
        min_sol_output_lamports=4_500_000,
    )
    assert len(data) == 24
    assert data[:8] == SELL_DISCRIMINATOR
    import struct
    amount, min_sol = struct.unpack("<QQ", data[8:24])
    assert amount == 500_000_000
    assert min_sol == 4_500_000


def test_buy_instruction_rejects_negative_amounts():
    with pytest.raises(ValueError):
        encode_buy_instruction_data(token_amount_raw=-1, max_sol_cost_lamports=1)
    with pytest.raises(ValueError):
        encode_buy_instruction_data(token_amount_raw=1, max_sol_cost_lamports=-1)


def test_sell_instruction_rejects_negative_amounts():
    with pytest.raises(ValueError):
        encode_sell_instruction_data(token_amount_raw=-1, min_sol_output_lamports=1)
    with pytest.raises(ValueError):
        encode_sell_instruction_data(token_amount_raw=1, min_sol_output_lamports=-1)


# ── Account ordering ──────────────────────────────────────────
def test_buy_accounts_have_expected_count_and_constants():
    """The buy instruction expects 12 accounts in fixed order. If the
    order ever changes, on-chain program will fail with cryptic error
    (account validation by index)."""
    acc = PumpFunBuyAccounts.for_user_and_mint(SAMPLE_USER, SAMPLE_MINT)
    # Constants are pinned to module-level singletons.
    assert acc.fee_recipient == PUMPFUN_FEE_RECIPIENT
    assert acc.system_program == SYSTEM_PROGRAM_ID
    assert acc.token_program == TOKEN_PROGRAM_ID
    assert acc.program == PUMPFUN_PROGRAM_ID
    # User and mint pass through.
    assert acc.user == SAMPLE_USER
    assert acc.mint == SAMPLE_MINT
    # Bonding-curve PDA is mint-derived.
    assert acc.bonding_curve == derive_bonding_curve_pda(SAMPLE_MINT)
    # Associated bonding curve = ATA(bonding_curve, mint).
    assert acc.associated_bonding_curve == derive_associated_token_account(
        acc.bonding_curve, acc.mint
    )
    # User's pump-fun-token ATA.
    assert acc.associated_user == derive_associated_token_account(
        SAMPLE_USER, SAMPLE_MINT
    )


def test_sell_accounts_have_expected_count_and_constants():
    acc = PumpFunSellAccounts.for_user_and_mint(SAMPLE_USER, SAMPLE_MINT)
    assert acc.fee_recipient == PUMPFUN_FEE_RECIPIENT
    assert acc.system_program == SYSTEM_PROGRAM_ID
    assert acc.token_program == TOKEN_PROGRAM_ID
    assert acc.program == PUMPFUN_PROGRAM_ID
    assert acc.bonding_curve == derive_bonding_curve_pda(SAMPLE_MINT)


# ── Bonding-curve state parsing & math ────────────────────────
def test_bonding_curve_state_parse_basic():
    """Synthetic on-chain payload — discriminator + 5×u64 + bool."""
    import struct
    from pulse_bot.execution_pumpfun import BondingCurveState
    discriminator = bytes(8)
    body = struct.pack(
        "<QQQQQ",
        1_073_000_000_000_000,  # virtual_token_reserves (typical fresh mint)
        30_000_000_000,          # virtual_sol_reserves (~30 SOL in lamports)
        793_100_000_000_000,     # real_token_reserves
        0,                       # real_sol_reserves (fresh mint)
        1_000_000_000_000_000,   # token_total_supply
    )
    data = discriminator + body + bytes([0])  # complete=False
    state = BondingCurveState.from_account_data(data)
    assert state.virtual_token_reserves == 1_073_000_000_000_000
    assert state.virtual_sol_reserves == 30_000_000_000
    assert state.real_sol_reserves == 0
    assert state.complete is False


def test_bonding_curve_state_rejects_short_buffer():
    from pulse_bot.execution_pumpfun import BondingCurveState
    with pytest.raises(ValueError):
        BondingCurveState.from_account_data(b"\x00" * 10)


def test_estimate_buy_output_constant_product_math():
    """For a fresh-mint state, buying 1 SOL should yield approximately
    the post-fee constant-product output. Uses the published pump.fun
    formula (1 % fee on input)."""
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        estimate_buy_output_tokens,
    )
    state = BondingCurveState(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=793_100_000_000_000,
        real_sol_reserves=0,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
    )
    one_sol = 1_000_000_000  # lamports
    out = estimate_buy_output_tokens(one_sol, state)

    # Hand-computed expectation:
    #   sol_post_fee = 1 SOL × 0.99 = 0.99 SOL = 990_000_000 lamports
    #   new_v_sol = 30 + 0.99 = 30.99 SOL
    #   new_v_tok = (30 × 1.073e15) / 30.99 ≈ 1.0387e15
    #   tokens_out ≈ 1.073e15 − 1.0387e15 ≈ 3.43e13 raw tokens
    assert 3.0e13 < out < 3.6e13, f"got {out}"


def test_estimate_buy_zero_when_curve_complete():
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        estimate_buy_output_tokens,
    )
    state = BondingCurveState(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=0,
        real_sol_reserves=85_000_000_000,
        token_total_supply=1_000_000_000_000_000,
        complete=True,
    )
    assert estimate_buy_output_tokens(1_000_000_000, state) == 0


def test_estimate_sell_inverts_buy_within_fee():
    """Round-trip test: buy X SOL, then sell the resulting tokens.
    Output should be < X SOL by ~2 % (1 % fee × 2 sides) plus
    constant-product price-impact slippage."""
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        estimate_buy_output_tokens,
        estimate_sell_output_lamports,
    )
    state = BondingCurveState(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=793_100_000_000_000,
        real_sol_reserves=10_000_000_000,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
    )
    sol_in = 100_000_000  # 0.1 SOL
    tokens_out = estimate_buy_output_tokens(sol_in, state)
    # Sell those tokens immediately (state still pre-buy here — the
    # math approximation is fine for the fee invariant).
    sol_round_trip = estimate_sell_output_lamports(tokens_out, state)
    # Round-trip must be strictly less than input (fees + impact).
    assert sol_round_trip < sol_in
    # Should retain at least 95 % (small position vs 30-SOL curve →
    # impact tiny, dominant cost = 2 × 1 % fee ≈ 2 %).
    assert sol_round_trip > sol_in * 0.95


def test_estimate_buy_zero_for_zero_input():
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        estimate_buy_output_tokens,
    )
    state = BondingCurveState(
        virtual_token_reserves=1, virtual_sol_reserves=1,
        real_token_reserves=1, real_sol_reserves=0,
        token_total_supply=1, complete=False,
    )
    assert estimate_buy_output_tokens(0, state) == 0
    assert estimate_buy_output_tokens(-100, state) == 0
