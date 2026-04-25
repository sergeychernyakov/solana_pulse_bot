# tests/pulse_bot/test_helius_backfill.py
"""Synthetic-payload parsing tests for ``scripts/helius_backfill_graduated``.

We don't hit the live Helius API in unit tests — we feed the parser a
hand-crafted payload that mirrors the real ``v0/transactions`` shape
observed against three graduated pump.fun mints (April 2026) and assert
the resulting ``Trade`` matches expected fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from helius_backfill_graduated import parse_pump_swap  # noqa: E402

MINT = "12C1rSuuBBZqtpZHHLvVGwFLGSajhLuURGt3Z8eFpump"
SIGNER = "AjU2EZjzUJ1w9Mrvc7XmaFAc9QdMgNqiNfLeU1cEy7aN"
BONDING_CURVE = "7aYCKsSnoCVBKFdjejAUU3WYob9vYGsmHTWR2wi8TT73"


def _swap_payload(direction: str, sol_lamports: int, token_amount: float) -> dict:
    """Construct a minimal Helius parsed-tx that mirrors a pump.fun swap.

    Direction is encoded in the tokenTransfer (signer = receiver for
    BUY, signer = sender for SELL) and in the sign of the bonding-curve
    nativeBalanceChange (negative on BUY = curve sends SOL in? no — on
    BUY the curve RECEIVES SOL → positive nbc; on SELL the curve
    sends SOL → negative nbc). Magnitude is what we extract.
    """
    if direction == "buy":
        from_user, to_user = BONDING_CURVE, SIGNER
        # signer paid SOL → curve nbc positive
        bc_nbc = +sol_lamports
        signer_nbc = -sol_lamports - 5_000  # rough fee
        from_user, to_user = SIGNER, SIGNER  # placeholder, overridden below
        from_acct, to_acct = "from_token_acct", "signer_token_acct"
    else:
        bc_nbc = -sol_lamports
        signer_nbc = +sol_lamports - 5_000
        from_acct, to_acct = "signer_token_acct", "to_token_acct"

    # Token transfer: for BUY signer is toUserAccount; for SELL signer is fromUserAccount.
    if direction == "buy":
        token_transfer = {
            "fromTokenAccount": "curve_token_acct",
            "toTokenAccount": "signer_token_acct",
            "fromUserAccount": BONDING_CURVE,
            "toUserAccount": SIGNER,
            "tokenAmount": token_amount,
            "mint": MINT,
            "tokenStandard": "Fungible",
        }
    else:
        token_transfer = {
            "fromTokenAccount": "signer_token_acct",
            "toTokenAccount": "curve_token_acct",
            "fromUserAccount": SIGNER,
            "toUserAccount": BONDING_CURVE,
            "tokenAmount": token_amount,
            "mint": MINT,
            "tokenStandard": "Fungible",
        }

    return {
        "signature": "test_sig_" + direction,
        "timestamp": 1776826687,
        "type": "SWAP",
        "source": "PUMP_FUN",
        "feePayer": SIGNER,
        "tokenTransfers": [token_transfer],
        "nativeTransfers": [],  # parser uses accountData, not these
        "accountData": [
            {"account": SIGNER, "nativeBalanceChange": signer_nbc},
            {"account": BONDING_CURVE, "nativeBalanceChange": bc_nbc},
            {"account": "fee_collector", "nativeBalanceChange": 1_000},
        ],
        "instructions": [],
        "events": {},
    }


def test_parse_buy_extracts_expected_fields() -> None:
    """A pump.fun BUY parses into a Trade with tx_type=buy, correct
    sol_amount (from bonding curve nbc), token_amount, and signer wallet."""
    sol_lamports = 2_500_000_000  # 2.5 SOL
    token_amount = 51_736_632.42

    payload = _swap_payload("buy", sol_lamports, token_amount)
    trade = parse_pump_swap(payload, MINT)

    assert trade is not None
    assert trade.mint == MINT
    assert trade.tx_type == "buy"
    assert trade.wallet == SIGNER
    assert abs(trade.sol_amount - 2.5) < 1e-9
    assert abs(trade.token_amount - token_amount) < 1e-3
    assert trade.timestamp == 1776826687.0
    # Curve state is not parsable from the parsed-tx payload — must be 0.
    assert trade.market_cap_sol == 0.0
    assert trade.v_sol_in_bonding_curve == 0.0


def test_parse_sell_inverts_direction() -> None:
    """A pump.fun SELL parses with tx_type=sell and the same magnitude
    sol_amount (drawn from the bonding curve account, not signer)."""
    sol_lamports = 420_128_307  # 0.42 SOL
    token_amount = 10_558_522.33

    payload = _swap_payload("sell", sol_lamports, token_amount)
    trade = parse_pump_swap(payload, MINT)

    assert trade is not None
    assert trade.tx_type == "sell"
    assert abs(trade.sol_amount - 0.420128307) < 1e-9
    assert abs(trade.token_amount - token_amount) < 1e-3


def test_non_pumpfun_returns_none() -> None:
    """Helius marks non-pump.fun swaps with a different ``source`` —
    parser must reject them."""
    payload = _swap_payload("buy", 1_000_000_000, 1.0)
    payload["source"] = "RAYDIUM"
    assert parse_pump_swap(payload, MINT) is None


def test_unrelated_mint_returns_none() -> None:
    """When the requested mint isn't in tokenTransfers, parser bails."""
    payload = _swap_payload("buy", 1_000_000_000, 1.0)
    assert parse_pump_swap(payload, "OtherMintAddressXyzabc") is None
