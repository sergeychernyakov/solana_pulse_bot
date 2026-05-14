# pulse_bot/llm/streamlit_app.py
# ruff: noqa: E402 — Streamlit runs this file directly, so the repo-root
# sys.path injection (below) must happen before any pulse_bot.* import.
"""Interactive Streamlit UI for the LangChain/LangGraph trade-explainer.

Run::

    .venv/bin/streamlit run pulse_bot/llm/streamlit_app.py

Three tabs:

1. **LCEL chain** — pick a trade row, watch the prompt render in real
   time (with placeholders filled in), invoke the chain, render the
   parsed :class:`TradeExplanation` as a styled card.
2. **LangGraph workflow** — same row, run through the multi-node
   StateGraph; show the analyst → critic → synthesizer trace
   step-by-step with each node's output expandable.
3. **Architecture** — Mermaid diagrams of both layers so the audience
   sees the wiring at a glance.

LLM provider is selectable in the sidebar:
* **Stub** (default) — :class:`FakeListChatModel` with canned outputs;
  no API key, no network, instant. Perfect for a screenshare demo.
* **Real** — :class:`ChatAnthropic` (Haiku 4.5). Requires
  ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

# Streamlit launches the script directly, so the repo root isn't on
# sys.path the way it would be under ``python -m``. Inject it before
# any ``pulse_bot.*`` imports.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate

from pulse_bot.llm.analysis_graph import build_analysis_graph
from pulse_bot.llm.explainer import (
    _SYSTEM_PROMPT,
    _USER_PROMPT,
    TradeExplanation,
    build_trade_explainer_chain,
)

FIXTURE_PATH = REPO_ROOT / "pulse_bot" / "llm" / "fixtures" / "trades_sample.json"


# ── Page config ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Trade Explainer — LangChain × LangGraph",
    page_icon="🧠",
    layout="wide",
)


# ── Helpers ──────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_trades() -> list[dict[str, Any]]:
    if not FIXTURE_PATH.exists():
        return []
    return json.loads(FIXTURE_PATH.read_text())


def stub_response_for(trade: Mapping[str, Any]) -> str:
    """Same canned-response logic as scripts/demo_trade_explainer.py
    so the Streamlit demo and the CLI demo show identical outputs."""
    pnl = float(trade.get("pnl_sol") or 0.0)
    exit_reason = (trade.get("exit_reason") or "").strip()
    hold = float(trade.get("hold_seconds") or 0.0)

    is_fee_bleed = (
        exit_reason == "survival_predict" and 90 < hold < 110 and -0.008 < pnl < -0.006
    )

    if is_fee_bleed:
        payload = {
            "entry_thesis": (
                f"ML override BUY at score {trade.get('entry_score', '?')}, "
                "fast-filter conviction above the rules baseline."
            ),
            "exit_assessment": (
                "Survival hazard fired at ~97s with predicted_remaining=25s "
                "and pnl=-7% — fee-only signature kill, token had no time "
                "to develop."
            ),
            "quality_grade": "bad",
            "follow_up": (
                "Disable survival or retrain with different loss; current "
                "model is degenerate."
            ),
        }
    elif exit_reason == "dead_token" and pnl > 0:
        payload = {
            "entry_thesis": (
                f"FAST_BUY conviction at mcap ~{trade.get('entry_mcap_sol', '?')} "
                "SOL; ML override lifted what rules had marked SKIP."
            ),
            "exit_assessment": (
                f"Token went inactive after {hold:.0f}s of holding; bot "
                "exited cleanly with positive PnL — exactly how dead_token "
                "exit is supposed to work."
            ),
            "quality_grade": "good",
            "follow_up": "Monitor only; this is the healthy exit path.",
        }
    elif exit_reason == "hard_stop":
        payload = {
            "entry_thesis": "Standard ml_override entry at the configured score threshold.",
            "exit_assessment": (
                f"Stop-loss fired at hold={hold:.0f}s after pnl_pct fell "
                "below -15% — matches exit_hard_stop_loss_pct config."
            ),
            "quality_grade": "neutral",
            "follow_up": "Monitor only; SL working as configured.",
        }
    else:
        payload = {
            "entry_thesis": (
                f"Entry via {trade.get('entry_type', 'rules')} at score "
                f"{trade.get('entry_score', '?')}."
            ),
            "exit_assessment": (
                f"Exit reason '{exit_reason or 'open'}' at hold "
                f"{hold:.0f}s with pnl={pnl:+.4f} SOL."
            ),
            "quality_grade": "neutral",
            "follow_up": "Monitor; need more samples to grade.",
        }
    return json.dumps(payload)


def stub_chain_for(trade: Mapping[str, Any]):
    parser = PydanticOutputParser(pydantic_object=TradeExplanation)
    prompt = ChatPromptTemplate.from_messages(
        [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
    ).partial(format_instructions=parser.get_format_instructions())
    fake = FakeListChatModel(responses=[stub_response_for(trade)])
    return prompt | fake | parser


def payload_for(trade: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
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
    return {k: trade.get(k) if trade.get(k) is not None else "" for k in keys}


def grade_to_color(grade: str) -> tuple[str, str]:
    g = (grade or "").lower()
    return {
        "good": ("🟢", "#1f8f4f"),
        "neutral": ("🟡", "#c89a1a"),
        "bad": ("🔴", "#c8341a"),
    }.get(g, ("⚪", "#888"))


# ── Sidebar ──────────────────────────────────────────────────────
trades = load_trades()
if not trades:
    st.error(
        f"No fixture found at {FIXTURE_PATH}. "
        "Run scripts/demo_trade_explainer.py once to generate it."
    )
    st.stop()

st.sidebar.title("⚙️ Config")

provider = st.sidebar.radio(
    "LLM provider",
    options=["Stub (no API)", "Real (Anthropic)"],
    index=0,
    help=(
        "Stub uses FakeListChatModel — instant, no key, no spend. "
        "Real uses claude-haiku-4-5 via ANTHROPIC_API_KEY."
    ),
)

if provider == "Real (Anthropic)" and not os.environ.get("ANTHROPIC_API_KEY"):
    st.sidebar.warning("ANTHROPIC_API_KEY not set — falling back to stub mode.")
    provider = "Stub (no API)"

trade_label = st.sidebar.selectbox(
    "Pick a paper-trade",
    options=range(len(trades)),
    format_func=lambda i: (
        f"#{trades[i].get('id')} {trades[i].get('symbol', '?')} "
        f"({trades[i].get('exit_reason', 'open')}, "
        f"{trades[i].get('pnl_sol', 0):+.4f} SOL)"
    ),
)
trade = trades[trade_label]

st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Repo files:**\n"
    "- `pulse_bot/llm/explainer.py` — LCEL chain\n"
    "- `pulse_bot/llm/analysis_graph.py` — LangGraph\n"
    "- `tests/pulse_bot/test_llm_explainer.py` — 5 tests\n"
    "- `docs/llm_demo/README.md` — portfolio writeup"
)


# ── Main ─────────────────────────────────────────────────────────
st.title("🧠 Trade Explainer — LangChain × LangGraph")
st.caption(
    "Post-hoc LLM analysis of pulse_bot paper-trades. "
    "OFF the hot path (LLM latency 500-1500 ms is too slow for "
    "T+30 entry decisions); intended for dashboard tooltips and "
    "model-debugging review."
)

# Trade summary card
emoji, color = grade_to_color(
    "bad"
    if (
        trade.get("exit_reason") == "survival_predict"
        and -0.008 < float(trade.get("pnl_sol") or 0) < -0.006
    )
    else "good" if float(trade.get("pnl_sol") or 0) > 0 else "neutral"
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Symbol", trade.get("symbol", "?"))
c2.metric("Exit reason", trade.get("exit_reason") or "(open)")
c3.metric("Hold (s)", f"{float(trade.get('hold_seconds') or 0):.0f}")
c4.metric(
    "PnL (SOL)",
    f"{float(trade.get('pnl_sol') or 0):+.4f}",
    delta=f"{float(trade.get('pnl_pct') or 0):+.1f}%",
)


tab_chain, tab_graph, tab_arch = st.tabs(
    ["1. LCEL chain", "2. LangGraph workflow", "3. Architecture"]
)


# ── Tab 1: LCEL chain ────────────────────────────────────────────
with tab_chain:
    st.subheader("LangChain LCEL pipeline")
    st.code(
        "chain = prompt | llm | parser\n"
        "result: TradeExplanation = chain.invoke(trade_row)",
        language="python",
    )

    with st.expander("🪧 Rendered prompt (what the LLM actually sees)"):
        parser = PydanticOutputParser(pydantic_object=TradeExplanation)
        prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM_PROMPT), ("user", _USER_PROMPT)]
        ).partial(format_instructions=parser.get_format_instructions())
        rendered = prompt.invoke(payload_for(trade))
        for msg in rendered.to_messages():
            st.markdown(f"**[{msg.type.upper()}]**")
            st.code(msg.content, language="markdown")

    if st.button("▶️ Run chain", type="primary", key="run_chain"):
        with st.spinner("Invoking chain…"):
            t0 = time.perf_counter()
            if provider == "Real (Anthropic)":
                chain = build_trade_explainer_chain()
            else:
                chain = stub_chain_for(trade)
            result: TradeExplanation = chain.invoke(payload_for(trade))
            elapsed_ms = (time.perf_counter() - t0) * 1000

        st.success(f"Chain completed in {elapsed_ms:.0f} ms")

        emoji, _ = grade_to_color(result.quality_grade)

        st.markdown(f"### {emoji} Grade: **{result.quality_grade.upper()}**")
        st.markdown(f"**Entry thesis** — {result.entry_thesis}")
        st.markdown(f"**Exit assessment** — {result.exit_assessment}")
        st.markdown(f"**Follow-up** — {result.follow_up}")

        with st.expander("Raw Pydantic output"):
            st.json(result.model_dump())


# ── Tab 2: LangGraph workflow ────────────────────────────────────
with tab_graph:
    st.subheader("LangGraph multi-node workflow")
    st.markdown(
        "Three nodes — **analyst** → **critic** → **synthesizer** — "
        "with a conditional edge after `analyst` that **skips the "
        "critic for open trades** (nothing to second-guess yet)."
    )

    if provider == "Stub (no API)":
        st.info(
            "ℹ️ The graph demo requires real LLM calls so analyst, "
            "critic and synthesizer can produce *different* outputs. "
            "Switch to **Real (Anthropic)** in the sidebar after "
            "setting `ANTHROPIC_API_KEY`."
        )

    if st.button("▶️ Run graph", type="primary", key="run_graph"):
        if provider != "Real (Anthropic)":
            st.warning("Stub provider — can't show analyst/critic divergence.")
        else:
            with st.spinner("Walking the StateGraph…"):
                graph = build_analysis_graph()
                t0 = time.perf_counter()
                state = graph.invoke({"trade": trade})
                elapsed_ms = (time.perf_counter() - t0) * 1000

            st.success(f"Graph completed in {elapsed_ms:.0f} ms")

            with st.expander("📋 Analyst view (node 1)", expanded=True):
                st.write(state.get("analyst_view", "(empty)"))

            critic_view = state.get("critic_view")
            if critic_view:
                with st.expander("🔎 Critic view (node 2)", expanded=True):
                    st.write(critic_view)
            else:
                st.info("Critic node skipped — open trade.")

            with st.expander(
                "🎯 Synthesizer (node 3) — final verdict",
                expanded=True,
            ):
                st.write(state.get("final_verdict", "(empty)"))
                emoji, _ = grade_to_color(state.get("quality_grade", "neutral"))
                st.markdown(
                    f"### {emoji} Grade: **{state.get('quality_grade', 'neutral').upper()}**"
                )

            with st.expander("Raw AnalysisState"):
                st.json({k: v for k, v in state.items() if k != "trade"})


# ── Tab 3: Architecture ──────────────────────────────────────────
with tab_arch:
    st.subheader("Layer 1 — LangChain LCEL chain")
    st.markdown(
        """
        ```
        ChatPromptTemplate  →  ChatAnthropic(haiku-4.5)  →  PydanticOutputParser(TradeExplanation)
        ```
        Single-shot. Used for batch generation of dashboard tooltips.
        """
    )

    st.subheader("Layer 2 — LangGraph state machine")
    graph = build_analysis_graph()
    st.markdown(f"```mermaid\n{graph.get_graph().draw_mermaid()}\n```")
    st.caption(
        "Conditional edge after `analyst` routes open trades "
        "directly to `synthesizer`, closed trades through `critic`."
    )

    st.subheader("State (LangGraph TypedDict)")
    st.code(
        """class AnalysisState(TypedDict, total=False):
    trade: Mapping[str, Any]
    analyst_view: str
    critic_view: str
    final_verdict: str
    quality_grade: Literal["good", "neutral", "bad"]""",
        language="python",
    )

    st.subheader("Test isolation")
    st.markdown(
        "All 5 tests in `tests/pulse_bot/test_llm_explainer.py` "
        "run in **~0.6 s** with **zero network calls** by swapping "
        "the LLM for `FakeListChatModel`. CI never burns API spend."
    )
