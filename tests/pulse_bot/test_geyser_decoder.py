# tests/pulse_bot/test_geyser_decoder.py
"""Yellowstone gRPC pump.fun transaction decoder — covers create/buy/sell
detection, edge cases (failed tx, no pump.fun, no mint, only WSOL) and the
mint-direction selection rules.

These tests construct minimal protobuf fixtures rather than mocking, so they
exercise the same field access paths that production protobuf decoding does
— a missing or renamed field surfaces here, not in production.
"""

from __future__ import annotations

import base58

from pulse_bot.launchpads.geyser import (
    PUMPFUN_PROGRAM_ID,
    WSOL_MINT,
    _decode_pumpfun_event,
)
from pulse_bot.launchpads.yellowstone_proto import (
    geyser_pb2 as pb,
    solana_storage_pb2 as ss,
)


def _b58_to_bytes(s: str) -> bytes:
    return base58.b58decode(s)


def _make_info(
    *,
    signer_pubkey: str = "Bn7iLNqfPqwz4Au5jufHGE3wMpNoFLsxhwzCYx5xc6oF",
    sig: str = "5JZ" + "K" * 84,
    log_messages: list[str],
    pre_token_balances: list[ss.TokenBalance] | None = None,
    post_token_balances: list[ss.TokenBalance] | None = None,
    pre_balances: list[int] | None = None,
    post_balances: list[int] | None = None,
    err: bool = False,
) -> pb.SubscribeUpdateTransactionInfo:
    """Build a minimal SubscribeUpdateTransactionInfo for decoder tests."""
    msg = ss.Message(
        account_keys=[
            _b58_to_bytes(signer_pubkey),
            # second key — pump.fun program (not strictly needed by decoder
            # but documents the shape we'd see in production).
            _b58_to_bytes(PUMPFUN_PROGRAM_ID),
        ],
    )
    transaction = ss.Transaction(message=msg)
    meta = ss.TransactionStatusMeta(
        log_messages=log_messages,
        pre_token_balances=pre_token_balances or [],
        post_token_balances=post_token_balances or [],
        pre_balances=pre_balances or [],
        post_balances=post_balances or [],
    )
    if err:
        meta.err.err = b"\x01"
    info = pb.SubscribeUpdateTransactionInfo(
        signature=_b58_to_bytes(sig)[:64] if len(sig) >= 64 else _b58_to_bytes(sig),
        transaction=transaction,
        meta=meta,
    )
    return info


def _tb(account_index: int, mint: str, ui_amount: float) -> ss.TokenBalance:
    return ss.TokenBalance(
        account_index=account_index,
        mint=mint,
        ui_token_amount=ss.UiTokenAmount(ui_amount=ui_amount, decimals=6),
    )


# ── create ──────────────────────────────────────────────────────────────


def test_decode_create_event() -> None:
    info = _make_info(
        log_messages=[
            "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
            "Program log: Instruction: Create",
            "Program log: Initialized",
        ],
        post_token_balances=[_tb(2, "MintAaaa", 1_000_000.0)],
    )
    ev = _decode_pumpfun_event(info)
    assert ev is not None
    assert ev["kind"] == "create"
    assert ev["mint"] == "MintAaaa"
    assert ev["creator"]  # base58 of signer
    assert "signature" in ev


# ── buy / sell with proper amounts ──────────────────────────────────────


def test_decode_buy_event_extracts_amounts() -> None:
    # Trader buys: spends ~1 SOL (lamports delta on bonding-curve account)
    # Receives ~12345 of mint MintXxxx.
    info = _make_info(
        log_messages=[
            "Program 6EF8r... invoke",
            "Program log: Instruction: Buy",
        ],
        pre_balances=[10_000_000_000, 5_000_000_000, 0],
        post_balances=[8_999_900_000, 6_000_000_000, 0],  # signer −1.0 (fee+sol),
        # bonding curve account[1] +1.0 SOL → biggest non-signer delta
        pre_token_balances=[_tb(2, "MintXxxx", 0.0)],
        post_token_balances=[_tb(2, "MintXxxx", 12345.0)],
    )
    ev = _decode_pumpfun_event(info)
    assert ev is not None
    assert ev["kind"] == "buy"
    assert ev["tx_type"] == "buy"
    assert ev["mint"] == "MintXxxx"
    assert abs(ev["sol_amount"] - 1.0) < 1e-6
    assert ev["token_amount"] == 12345.0


def test_decode_sell_event() -> None:
    info = _make_info(
        log_messages=["Program log: Instruction: Sell"],
        pre_balances=[10_000_000_000, 5_000_000_000, 0],
        post_balances=[10_499_900_000, 4_500_000_000, 0],
        pre_token_balances=[_tb(2, "MintYyyy", 1_000.0)],
        post_token_balances=[_tb(2, "MintYyyy", 0.0)],
    )
    ev = _decode_pumpfun_event(info)
    assert ev is not None
    assert ev["kind"] == "sell"
    assert ev["mint"] == "MintYyyy"
    assert ev["token_amount"] == 1_000.0


# ── edge cases: must return None ────────────────────────────────────────


def test_decode_skips_failed_tx() -> None:
    info = _make_info(
        log_messages=["Program log: Instruction: Buy"],
        pre_token_balances=[_tb(2, "MintZzzz", 0.0)],
        post_token_balances=[_tb(2, "MintZzzz", 100.0)],
        err=True,
    )
    assert _decode_pumpfun_event(info) is None


def test_decode_skips_non_pumpfun_logs() -> None:
    info = _make_info(
        log_messages=[
            "Program XXX invoke",
            "Program log: Instruction: Transfer",  # not pump.fun
        ],
        post_token_balances=[_tb(2, "MintAaaa", 100.0)],
    )
    assert _decode_pumpfun_event(info) is None


def test_decode_skips_when_no_mint_found() -> None:
    info = _make_info(
        log_messages=["Program log: Instruction: Buy"],
        # only WSOL balances → no real mint to track
        pre_token_balances=[_tb(2, WSOL_MINT, 0.0)],
        post_token_balances=[_tb(2, WSOL_MINT, 1.0)],
    )
    assert _decode_pumpfun_event(info) is None


def test_decode_skips_when_zero_token_amount() -> None:
    """Buy log present but no token movement → not a real swap."""
    info = _make_info(
        log_messages=["Program log: Instruction: Buy"],
        pre_balances=[10_000_000_000, 5_000_000_000],
        post_balances=[8_999_900_000, 5_000_000_000],
        pre_token_balances=[_tb(2, "MintQ", 100.0)],
        post_token_balances=[_tb(2, "MintQ", 100.0)],  # same balance
    )
    assert _decode_pumpfun_event(info) is None


def test_decode_skips_empty_account_keys() -> None:
    info = _make_info(log_messages=["Program log: Instruction: Buy"])
    info.transaction.message.ClearField("account_keys")
    info.transaction.message.account_keys.append(b"")  # only an empty key
    info.transaction.message.ClearField("account_keys")
    assert _decode_pumpfun_event(info) is None


def test_decode_picks_non_wsol_mint_when_both_present() -> None:
    """Pump.fun trades touch both WSOL and the meme-token account; must
    return the meme token, not WSOL."""
    info = _make_info(
        log_messages=["Program log: Instruction: Buy"],
        pre_balances=[10_000_000_000, 5_000_000_000],
        post_balances=[8_999_900_000, 6_000_000_000],
        pre_token_balances=[_tb(1, WSOL_MINT, 0.0), _tb(2, "MemeTokenMint", 0.0)],
        post_token_balances=[_tb(1, WSOL_MINT, 1.0), _tb(2, "MemeTokenMint", 5000.0)],
    )
    ev = _decode_pumpfun_event(info)
    assert ev is not None
    assert ev["mint"] == "MemeTokenMint"
