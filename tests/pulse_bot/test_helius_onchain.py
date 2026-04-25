# tests/pulse_bot/test_helius_onchain.py
"""Unit tests for SPL mint account parsing.

No live RPC calls — fabricated account bytes exercise all code paths.
"""

from __future__ import annotations

import base64
import struct

from pulse_bot.helius_onchain import (
    SPL_MINT_ACCOUNT_SIZE,
    MintState,
    parse_mint_account_data,
)

ZERO_PUBKEY = b"\x00" * 32


def _build_mint_blob(
    mint_tag: int = 0,
    freeze_tag: int = 0,
    supply: int = 1_000_000_000_000,
    decimals: int = 6,
) -> str:
    """Construct an SPL Mint layout (82 bytes) as base64."""
    blob = bytearray(SPL_MINT_ACCOUNT_SIZE)
    struct.pack_into("<I", blob, 0, mint_tag)
    blob[4:36] = ZERO_PUBKEY
    struct.pack_into("<Q", blob, 36, supply)
    blob[44] = decimals
    blob[45] = 1  # is_initialized
    struct.pack_into("<I", blob, 46, freeze_tag)
    blob[50:82] = ZERO_PUBKEY
    return base64.b64encode(bytes(blob)).decode()


def test_parse_fully_revoked() -> None:
    state = parse_mint_account_data(_build_mint_blob(mint_tag=0, freeze_tag=0))
    assert state is not None
    assert state.mint_authority_revoked is True
    assert state.freeze_authority_revoked is True
    assert state.parse_error is None
    assert state.supply_raw == 1_000_000_000_000
    assert state.decimals == 6


def test_parse_mint_still_held() -> None:
    state = parse_mint_account_data(_build_mint_blob(mint_tag=1, freeze_tag=0))
    assert state is not None
    assert state.mint_authority_revoked is False
    assert state.freeze_authority_revoked is True


def test_parse_freeze_still_held() -> None:
    state = parse_mint_account_data(_build_mint_blob(mint_tag=0, freeze_tag=1))
    assert state is not None
    assert state.mint_authority_revoked is True
    assert state.freeze_authority_revoked is False


def test_parse_both_held() -> None:
    state = parse_mint_account_data(_build_mint_blob(mint_tag=1, freeze_tag=1))
    assert state is not None
    assert state.mint_authority_revoked is False
    assert state.freeze_authority_revoked is False


def test_parse_corrupt_tag_returns_parse_error() -> None:
    state = parse_mint_account_data(_build_mint_blob(mint_tag=2, freeze_tag=0))
    assert state is not None
    assert state.parse_error is not None
    assert "bad_tags" in state.parse_error


def test_parse_short_blob_returns_none() -> None:
    short = base64.b64encode(b"\x00" * 10).decode()
    assert parse_mint_account_data(short) is None


def test_parse_invalid_base64_returns_none() -> None:
    assert parse_mint_account_data("!!not-base64$$") is None


def test_mint_state_defaults() -> None:
    s = MintState(
        mint="x",
        observed_at=0.0,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        supply_raw=0,
        decimals=0,
    )
    assert s.parse_error is None
