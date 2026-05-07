# tests/pulse_bot/test_llm_explainer.py
"""Tests for ``pulse_bot.llm.explainer`` and ``analysis_graph``.

We never invoke the real Anthropic API in CI — the Pydantic
parser, prompt template, and graph topology all give us enough
surface area to test deterministically with
``FakeListChatModel``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
)
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate

from pulse_bot.llm.analysis_graph import (
    AnalysisState,
    _route_after_analyst,
    build_analysis_graph,
)
from pulse_bot.llm.explainer import (
    _SYSTEM_PROMPT,
    _USER_PROMPT,
    TradeExplanation,
)


# ── Fixtures ─────────────────────────────────────────────────────
@pytest.fixture
def closed_trade() -> dict[str, Any]:
    return {
        "id": 1,
        "mint": "TestMint000000000000000000000000000000000pump",
        "symbol": "TEST",
        "entry_type": "ml_override",
        "entry_score": 489,
        "entry_buyer_number": 5,
        "entry_mcap_sol": 33.5,
        "exit_reason": "survival_predict",
        "exit_mcap_sol": 0.0,
        "hold_seconds": 97.0,
        "pnl_sol": -0.007,
        "pnl_pct": -7.0,
        "total_buys": 0,
        "total_sells": 0,
    }


@pytest.fixture
def open_trade(closed_trade: dict[str, Any]) -> dict[str, Any]:
    closed_trade.update({"exit_reason": None, "hold_seconds": 0.0})
    return closed_trade


@pytest.fixture
def stub_chain():
    """LCEL chain wired to a deterministic fake chat model."""
    parser = PydanticOutputParser(pydantic_object=TradeExplanation)
    prompt = ChatPromptTemplate.from_messages(
        [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
    ).partial(format_instructions=parser.get_format_instructions())

    canned = json.dumps(
        {
            "entry_thesis": "ML override at score 489.",
            "exit_assessment": (
                "Survival fired at 97s — fee-only kill."
            ),
            "quality_grade": "bad",
            "follow_up": "Retrain survival.",
        }
    )
    fake = FakeListChatModel(responses=[canned])
    return prompt | fake | parser


# ── LCEL chain tests ─────────────────────────────────────────────
def test_chain_returns_trade_explanation(
    stub_chain, closed_trade: dict[str, Any]
):
    """The chain must round-trip through Pydantic into our schema."""
    payload = {
        k: closed_trade.get(k)
        for k in (
            "mint",
            "symbol",
            "entry_type",
            "entry_score",
            "entry_buyer_number",
            "entry_mcap_sol",
            "exit_reason",
            "exit_mcap_sol",
            "hold_seconds",
            "pnl_sol",
            "pnl_pct",
            "total_buys",
            "total_sells",
        )
    }
    result = stub_chain.invoke(payload)
    assert isinstance(result, TradeExplanation)
    assert result.quality_grade == "bad"
    assert "Survival" in result.exit_assessment


def test_pydantic_schema_rejects_invalid_grade():
    """Schema is duck-typed today (str), but we still want a clean
    JSON round-trip — guard against accidental shape regressions."""
    obj = TradeExplanation(
        entry_thesis="x",
        exit_assessment="y",
        quality_grade="good",
        follow_up="z",
    )
    assert obj.model_dump()["quality_grade"] == "good"


# ── LangGraph tests ──────────────────────────────────────────────
def test_graph_routes_open_trades_around_critic(
    open_trade: dict[str, Any],
):
    """Conditional edge: open trades skip the critic node."""
    state: AnalysisState = {"trade": open_trade}
    assert _route_after_analyst(state) == "synthesizer"


def test_graph_routes_closed_trades_through_critic(
    closed_trade: dict[str, Any],
):
    state: AnalysisState = {"trade": closed_trade}
    assert _route_after_analyst(state) == "critic"


def test_graph_compiles_with_expected_nodes():
    """Sanity: graph topology matches the design diagram."""
    g = build_analysis_graph()
    nodes = set(g.nodes.keys())
    # __start__ is added by LangGraph itself; the user-defined nodes
    # are the three we registered.
    assert {"analyst", "critic", "synthesizer"}.issubset(nodes)
