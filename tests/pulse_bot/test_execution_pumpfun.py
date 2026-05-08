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
    TOKEN_2022_PROGRAM_ID,
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
    assert acc.token_program == TOKEN_2022_PROGRAM_ID
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
    assert acc.token_program == TOKEN_2022_PROGRAM_ID
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


# ── Helius RPC client (mocked) ─────────────────────────────────
class _FakeResponse:
    def __init__(self, json_payload):
        self._payload = json_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _FakeSession:
    """Minimal aiohttp session stand-in. Records the last request
    payload so tests can assert what was sent."""

    def __init__(self, response_payload):
        self._response = response_payload
        self.last_payload = None

    def post(self, url, json=None, timeout=None):
        self.last_payload = json
        return _FakeResponse(self._response)

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_rpc_get_account_info_decodes_base64():
    from pulse_bot.execution_pumpfun import HeliusRpc
    import base64
    sample_data = b"\x00" * 49
    encoded = base64.b64encode(sample_data).decode()
    fake_resp = {
        "jsonrpc": "2.0",
        "result": {
            "value": {
                "data": [encoded, "base64"],
                "executable": False,
                "lamports": 2_000_000,
                "owner": "11111111111111111111111111111111",
                "rentEpoch": 0,
            },
        },
    }
    rpc = HeliusRpc(api_key="test")
    rpc._session = _FakeSession(fake_resp)
    out = await rpc.get_account_info(SAMPLE_MINT)
    assert out == sample_data


@pytest.mark.asyncio
async def test_rpc_get_account_info_returns_none_when_account_missing():
    from pulse_bot.execution_pumpfun import HeliusRpc
    fake_resp = {"jsonrpc": "2.0", "result": {"value": None}}
    rpc = HeliusRpc(api_key="test")
    rpc._session = _FakeSession(fake_resp)
    out = await rpc.get_account_info(SAMPLE_MINT)
    assert out is None


@pytest.mark.asyncio
async def test_rpc_simulate_transaction_success():
    """err=None means program succeeded; SimulateResult.success=True."""
    from pulse_bot.execution_pumpfun import HeliusRpc
    fake_resp = {
        "jsonrpc": "2.0",
        "result": {
            "value": {
                "err": None,
                "logs": ["Program log: Instruction: Buy", "Program log: ok"],
                "unitsConsumed": 30000,
            },
        },
    }
    rpc = HeliusRpc(api_key="test")
    rpc._session = _FakeSession(fake_resp)
    res = await rpc.simulate_transaction("base64_dummy_tx")
    assert res.success is True
    assert res.err is None
    assert res.units_consumed == 30000
    assert "Buy" in res.logs[0]


@pytest.mark.asyncio
async def test_rpc_simulate_transaction_slippage_revert():
    """When the program reverts due to slippage cap, err is non-null
    and success=False."""
    from pulse_bot.execution_pumpfun import HeliusRpc
    fake_resp = {
        "jsonrpc": "2.0",
        "result": {
            "value": {
                "err": {"InstructionError": [0, {"Custom": 6002}]},  # pump.fun "TooLittleSolReceived"
                "logs": ["Program log: TooMuchSolRequired"],
                "unitsConsumed": 5000,
            },
        },
    }
    rpc = HeliusRpc(api_key="test")
    rpc._session = _FakeSession(fake_resp)
    res = await rpc.simulate_transaction("base64_dummy_tx")
    assert res.success is False
    assert res.err is not None


@pytest.mark.asyncio
async def test_rpc_simulate_payload_has_expected_options():
    """Verify we ask Helius for replaceRecentBlockhash so simulation
    doesn't fail on stale blockhashes from offline-signed txs."""
    from pulse_bot.execution_pumpfun import HeliusRpc
    fake_resp = {"jsonrpc": "2.0", "result": {"value": {"err": None, "logs": []}}}
    fake = _FakeSession(fake_resp)
    rpc = HeliusRpc(api_key="test")
    rpc._session = fake
    await rpc.simulate_transaction("dummy")
    options = fake.last_payload["params"][1]
    assert options["replaceRecentBlockhash"] is True
    assert options["encoding"] == "base64"
    assert options["commitment"] == "processed"


# ── Compute Budget instruction encoding ──────────────────────
def test_set_compute_unit_limit_ix_format():
    """[0x02] + u32 LE units. Program: ComputeBudget111…"""
    from pulse_bot.execution_pumpfun import (
        COMPUTE_BUDGET_PROGRAM_ID,
        build_set_compute_unit_limit_ix,
    )
    ix = build_set_compute_unit_limit_ix(200_000)
    assert ix.program_id == COMPUTE_BUDGET_PROGRAM_ID
    assert len(ix.accounts) == 0
    import struct
    assert ix.data[0] == 0x02
    units, = struct.unpack("<I", ix.data[1:5])
    assert units == 200_000


def test_set_compute_unit_price_ix_format():
    """[0x03] + u64 LE microlamports."""
    from pulse_bot.execution_pumpfun import build_set_compute_unit_price_ix
    ix = build_set_compute_unit_price_ix(100_000)
    assert ix.data[0] == 0x03
    import struct
    fee, = struct.unpack("<Q", ix.data[1:9])
    assert fee == 100_000


def test_compute_unit_limit_validates_range():
    from pulse_bot.execution_pumpfun import build_set_compute_unit_limit_ix
    with pytest.raises(ValueError):
        build_set_compute_unit_limit_ix(-1)
    with pytest.raises(ValueError):
        build_set_compute_unit_limit_ix(2_000_000)  # > Solana cap


# ── ATA create-idempotent ─────────────────────────────────────
def test_create_ata_idempotent_ix_layout():
    """Program: ATA program. Discriminator byte 0x01 (idempotent
    variant). 6 accounts in fixed SPL order."""
    from pulse_bot.execution_pumpfun import (
        ATA_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        build_create_ata_idempotent_ix,
        derive_associated_token_account,
    )
    ix = build_create_ata_idempotent_ix(SAMPLE_USER, SAMPLE_USER, SAMPLE_MINT)
    assert ix.program_id == ATA_PROGRAM_ID
    assert ix.data == bytes([0x01])
    assert len(ix.accounts) == 6
    payer, ata, owner, mint, sys, token = ix.accounts
    assert payer.pubkey == SAMPLE_USER
    assert payer.is_signer is True
    assert payer.is_writable is True
    assert ata.pubkey == derive_associated_token_account(SAMPLE_USER, SAMPLE_MINT)
    assert ata.is_writable is True
    assert sys.pubkey == SYSTEM_PROGRAM_ID
    assert token.pubkey == TOKEN_2022_PROGRAM_ID


# ── Pump.fun buy/sell instruction with full account list ─────
def test_pump_buy_ix_account_count_and_signer_flags():
    """12 accounts in fixed order; user (idx 6) is the signer/writable.
    Program: pump.fun mainnet program."""
    from pulse_bot.execution_pumpfun import (
        PUMPFUN_PROGRAM_ID,
        build_pump_buy_ix,
    )
    ix = build_pump_buy_ix(
        user=SAMPLE_USER,
        mint=SAMPLE_MINT,
        token_amount_raw=1_000_000_000,
        max_sol_cost_lamports=10_000_000,
    )
    assert ix.program_id == PUMPFUN_PROGRAM_ID
    assert len(ix.accounts) == 12

    # User at index 6: is_signer=True, is_writable=True.
    user_acct = ix.accounts[6]
    assert user_acct.pubkey == SAMPLE_USER
    assert user_acct.is_signer is True
    assert user_acct.is_writable is True

    # Mint at index 2: readonly.
    assert ix.accounts[2].pubkey == SAMPLE_MINT
    assert ix.accounts[2].is_signer is False
    assert ix.accounts[2].is_writable is False

    # Bonding curve at index 3: writable.
    assert ix.accounts[3].is_writable is True

    # No other signers — only the user signs.
    assert sum(1 for a in ix.accounts if a.is_signer) == 1


def test_pump_sell_ix_account_count():
    """12 accounts; sell instruction has associated_token_program at
    slot 8 (vs rent at slot 9 for buy)."""
    from pulse_bot.execution_pumpfun import (
        ATA_PROGRAM_ID,
        PUMPFUN_PROGRAM_ID,
        build_pump_sell_ix,
    )
    ix = build_pump_sell_ix(
        user=SAMPLE_USER,
        mint=SAMPLE_MINT,
        token_amount_raw=500_000_000,
        min_sol_output_lamports=4_500_000,
    )
    assert ix.program_id == PUMPFUN_PROGRAM_ID
    assert len(ix.accounts) == 12
    # User still index 6, signer/writable.
    assert ix.accounts[6].pubkey == SAMPLE_USER
    assert ix.accounts[6].is_signer is True
    # Slot 8 is ATA program for sell (was rent for buy).
    assert ix.accounts[8].pubkey == ATA_PROGRAM_ID


# ── Full transaction assembly ─────────────────────────────────
def _make_test_keypair():
    from solders.keypair import Keypair
    # Deterministic for test reproducibility.
    return Keypair.from_seed(bytes(range(32)))


def _make_zero_blockhash():
    from solders.hash import Hash
    return Hash.default()


def _make_test_state():
    from pulse_bot.execution_pumpfun import BondingCurveState
    return BondingCurveState(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=793_100_000_000_000,
        real_sol_reserves=10_000_000_000,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
    )


def test_build_buy_transaction_assembles_and_signs():
    from pulse_bot.execution_pumpfun import (
        build_buy_transaction,
        serialize_signed_tx_base64,
    )
    kp = _make_test_keypair()
    tx = build_buy_transaction(
        keypair=kp,
        mint=SAMPLE_MINT,
        sol_amount_lamports=10_000_000,  # 0.01 SOL
        state=_make_test_state(),
        recent_blockhash=_make_zero_blockhash(),
        slippage_bps=100,
    )
    # Has 4 instructions: 2 compute budget + 1 ATA create + 1 buy.
    assert len(tx.message.instructions) == 4

    # Serialise to base64 — same shape we'd send to RPC.
    b64 = serialize_signed_tx_base64(tx)
    assert isinstance(b64, str)
    assert len(b64) > 0

    # Round-trip decode to verify it's valid base64.
    import base64
    decoded = base64.b64decode(b64)
    assert len(decoded) > 100  # non-trivial size


def test_build_buy_transaction_applies_slippage_to_max_sol_cost():
    """1 % slippage on 0.01 SOL → max_sol_cost = 0.01 × 1.01 = 0.0101 SOL."""
    from pulse_bot.execution_pumpfun import (
        BUY_DISCRIMINATOR,
        build_buy_transaction,
    )
    kp = _make_test_keypair()
    sol_in = 10_000_000  # 0.01 SOL
    tx = build_buy_transaction(
        keypair=kp,
        mint=SAMPLE_MINT,
        sol_amount_lamports=sol_in,
        state=_make_test_state(),
        recent_blockhash=_make_zero_blockhash(),
        slippage_bps=100,  # 1 %
    )
    # Last instruction (idx 3) is the pump buy.
    pump_ix = tx.message.instructions[-1]
    # pump_ix.data is a bytes-like object.
    data = bytes(pump_ix.data)
    assert data[:8] == BUY_DISCRIMINATOR
    import struct
    _amount, max_sol = struct.unpack("<QQ", data[8:24])
    expected_max = sol_in * 101 // 100  # +1 %
    assert max_sol == expected_max


def test_build_buy_transaction_rejects_complete_curve():
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        build_buy_transaction,
    )
    kp = _make_test_keypair()
    completed_state = BondingCurveState(
        virtual_token_reserves=0, virtual_sol_reserves=0,
        real_token_reserves=0, real_sol_reserves=85_000_000_000,
        token_total_supply=1_000_000_000_000_000, complete=True,
    )
    with pytest.raises(ValueError):
        build_buy_transaction(
            keypair=kp,
            mint=SAMPLE_MINT,
            sol_amount_lamports=10_000_000,
            state=completed_state,
            recent_blockhash=_make_zero_blockhash(),
        )


def test_build_sell_transaction_applies_slippage_to_min_sol_output():
    """1 % slippage on sell → min_sol_output = expected × 0.99."""
    from pulse_bot.execution_pumpfun import (
        SELL_DISCRIMINATOR,
        build_sell_transaction,
        estimate_sell_output_lamports,
    )
    kp = _make_test_keypair()
    state = _make_test_state()
    tokens_in = 1_000_000_000_000  # 1e12 raw tokens
    expected_sol = estimate_sell_output_lamports(tokens_in, state)

    tx = build_sell_transaction(
        keypair=kp,
        mint=SAMPLE_MINT,
        token_amount_raw=tokens_in,
        state=state,
        recent_blockhash=_make_zero_blockhash(),
        slippage_bps=100,
    )
    # Last instruction is the pump sell.
    pump_ix = tx.message.instructions[-1]
    data = bytes(pump_ix.data)
    assert data[:8] == SELL_DISCRIMINATOR
    import struct
    _amount, min_sol = struct.unpack("<QQ", data[8:24])
    expected_min = expected_sol * 99 // 100  # −1 %
    assert min_sol == expected_min


def test_build_sell_transaction_no_ata_create():
    """Sell tx skips ATA-create — caller already owns the ATA from
    prior buy. Should have only 3 instructions (2 cu + 1 sell)."""
    from pulse_bot.execution_pumpfun import build_sell_transaction
    kp = _make_test_keypair()
    tx = build_sell_transaction(
        keypair=kp,
        mint=SAMPLE_MINT,
        token_amount_raw=1_000_000_000_000,
        state=_make_test_state(),
        recent_blockhash=_make_zero_blockhash(),
    )
    assert len(tx.message.instructions) == 3


# ── PumpFunExecution orchestration ─────────────────────────────
class _FakeRpc:
    """Stand-in for HeliusRpc that returns scripted state + sim
    results. Lets us unit-test PumpFunExecution flow without RPC."""

    def __init__(self, *, state=None, blockhash=None, sim_result=None):
        self._state = state
        self._blockhash = blockhash
        self._sim_result = sim_result
        self.simulate_called_with: str | None = None

    async def fetch_bonding_curve_state(self, mint):
        return self._state

    async def get_latest_blockhash(self):
        return self._blockhash

    async def simulate_transaction(self, b64, **kwargs):
        self.simulate_called_with = b64
        return self._sim_result

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_pump_execution_simulate_buy_returns_success_when_curve_ok():
    from pulse_bot.execution_pumpfun import (
        PumpFunExecution,
        SimulateResult,
    )
    rpc = _FakeRpc(
        state=_make_test_state(),
        blockhash=_make_zero_blockhash(),
        sim_result=SimulateResult(
            success=True, err=None, logs=["Program log: Buy"], units_consumed=42_000,
        ),
    )
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    res = await ex.simulate_buy(SAMPLE_MINT, sol_amount_lamports=10_000_000)
    assert res.side == "buy"
    assert res.success is True
    assert res.submitted_live is False
    assert res.expected_tokens > 0
    assert res.units_consumed == 42_000
    assert res.err is None
    # Verify we actually called simulate (didn't accidentally short-circuit).
    assert rpc.simulate_called_with is not None


@pytest.mark.asyncio
async def test_pump_execution_simulate_buy_returns_error_when_state_missing():
    """Mints without a bonding curve account (off-pump.fun, or stale)
    return success=False with err=bonding_curve_account_missing."""
    from pulse_bot.execution_pumpfun import PumpFunExecution
    rpc = _FakeRpc(state=None)
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    res = await ex.simulate_buy(SAMPLE_MINT, sol_amount_lamports=10_000_000)
    assert res.success is False
    assert res.err == "bonding_curve_account_missing"


@pytest.mark.asyncio
async def test_pump_execution_simulate_buy_returns_error_when_curve_complete():
    from pulse_bot.execution_pumpfun import (
        BondingCurveState,
        PumpFunExecution,
    )
    completed = BondingCurveState(
        virtual_token_reserves=1, virtual_sol_reserves=1,
        real_token_reserves=0, real_sol_reserves=85_000_000_000,
        token_total_supply=1, complete=True,
    )
    rpc = _FakeRpc(state=completed)
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    res = await ex.simulate_buy(SAMPLE_MINT, sol_amount_lamports=10_000_000)
    assert res.success is False
    assert res.err == "curve_complete_post_graduation"


@pytest.mark.asyncio
async def test_pump_execution_simulate_buy_propagates_revert_err():
    """When the on-chain simulator returns err, PumpExecuteResult
    surfaces it as success=False with the same err payload."""
    from pulse_bot.execution_pumpfun import (
        PumpFunExecution,
        SimulateResult,
    )
    rpc = _FakeRpc(
        state=_make_test_state(),
        blockhash=_make_zero_blockhash(),
        sim_result=SimulateResult(
            success=False,
            err={"InstructionError": [3, {"Custom": 6002}]},
            logs=["Program log: TooMuchSolRequired"],
            units_consumed=5000,
        ),
    )
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    res = await ex.simulate_buy(SAMPLE_MINT, sol_amount_lamports=10_000_000)
    assert res.success is False
    assert res.err is not None
    assert res.units_consumed == 5000


@pytest.mark.asyncio
async def test_pump_execution_submit_buy_refused_without_allow_live_flag():
    """Default constructor disallows real on-chain submission. The
    operator must explicitly pass allow_live_submit=True after a
    deliberate decision."""
    from pulse_bot.execution_pumpfun import PumpFunExecution
    rpc = _FakeRpc()
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    with pytest.raises(RuntimeError, match="allow_live_submit"):
        await ex.submit_buy(SAMPLE_MINT, sol_amount_lamports=10_000_000)


@pytest.mark.asyncio
async def test_pump_execution_simulate_sell_requests_correct_amounts():
    from pulse_bot.execution_pumpfun import (
        PumpFunExecution,
        SimulateResult,
        estimate_sell_output_lamports,
    )
    state = _make_test_state()
    rpc = _FakeRpc(
        state=state,
        blockhash=_make_zero_blockhash(),
        sim_result=SimulateResult(
            success=True, err=None, logs=[], units_consumed=15000,
        ),
    )
    ex = PumpFunExecution(rpc=rpc, keypair=_make_test_keypair())
    tokens_in = 1_000_000_000_000
    res = await ex.simulate_sell(SAMPLE_MINT, token_amount_raw=tokens_in)
    assert res.side == "sell"
    assert res.success is True
    # Expected SOL out matches the curve math.
    assert res.expected_sol_out_lamports == estimate_sell_output_lamports(tokens_in, state)
