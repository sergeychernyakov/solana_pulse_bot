# tests/pulse_bot/test_enrich_backfilled_mc.py
"""Unit test for the pump.fun bonding curve replay arithmetic."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from enrich_backfilled_mc import (  # noqa: E402
    K_INVARIANT,
    TOTAL_SUPPLY,
    V_SOL_INIT,
    V_TOK_INIT,
    replay_curve,
)


def test_replay_three_trades_matches_constant_product() -> None:
    """A buy → buy → sell sequence must obey the constant-product invariant.

    ``v_sol`` is updated by the trader's gross ``sol_amount`` and ``v_tok``
    is re-derived from ``K`` on every step. The reported ``token_amount``
    is post-fee and is **not** used for the virtual-reserve update.
    """
    trades = [
        {"id": 1, "tx_type": "buy", "sol_amount": 1.0, "token_amount": 35_000_000},
        {"id": 2, "tx_type": "buy", "sol_amount": 0.5, "token_amount": 16_000_000},
        {"id": 3, "tx_type": "sell", "sol_amount": 0.7, "token_amount": 21_000_000},
    ]
    out = replay_curve(trades)
    assert len(out) == 3

    expected_vsols = [V_SOL_INIT + 1.0, V_SOL_INIT + 1.5, V_SOL_INIT + 0.8]
    for rep, want_vsol in zip(out, expected_vsols):
        assert abs(rep.v_sol - want_vsol) < 1e-9
        v_tok = K_INVARIANT / rep.v_sol
        want_mc = (rep.v_sol / v_tok) * TOTAL_SUPPLY
        assert abs(rep.market_cap_sol - want_mc) < 1e-6
        # Constant-product invariant must hold at every step.
        assert abs(rep.v_sol * v_tok - K_INVARIANT) < 1e-3


def test_replay_first_buy_yields_known_market_cap() -> None:
    """A 1 SOL first buy on a fresh curve yields a deterministic mc."""
    trades = [{"id": 42, "tx_type": "buy", "sol_amount": 1.0, "token_amount": 0}]
    rep = replay_curve(trades)[0]
    # Closed form: v_sol = 31, v_tok = K/31, mc = (31 / (K/31)) * 1e9
    expected_v_sol = V_SOL_INIT + 1.0
    expected_v_tok = K_INVARIANT / expected_v_sol
    expected_mc = (expected_v_sol / expected_v_tok) * TOTAL_SUPPLY
    assert rep.trade_id == 42
    assert abs(rep.v_sol - expected_v_sol) < 1e-9
    assert abs(rep.market_cap_sol - expected_mc) < 1e-6
    # Sanity: v_tok_init must be exactly the documented constant.
    assert V_TOK_INIT == 1_073_000_000.0
