# pulse_bot/llm/analysis_graph.py
"""LangGraph state-machine for multi-step paper_trade analysis.

The single-shot :class:`TradeExplanation` chain in
:mod:`pulse_bot.llm.explainer` is great for one-off summaries, but
real review workflows benefit from *separation of concerns*:

1. **Analyst** — interpret the trade in isolation (what happened?).
2. **Critic** — challenge the analyst (is this really 'bad' or just
   noise? does the verdict over-weight one signal?).
3. **Synthesizer** — reconcile analyst + critic into a final verdict
   that's defensible and concise.

That's the classic *reflection* pattern from the LangGraph docs,
applied to trading-bot paper-trade review. The state graph also
demonstrates **conditional edges**: if a trade is still open
(no exit_reason), we skip the critic step entirely — there's
nothing to second-guess yet.

Graph topology::

       ┌─────────┐
       │  START  │
       └────┬────┘
            │
            ▼
       ┌─────────┐
       │ analyst │
       └────┬────┘
            │ closed?  ──── no ──┐
            │ yes               │
            ▼                    │
       ┌─────────┐              │
       │ critic  │              │
       └────┬────┘              │
            │                    │
            ▼                    │
       ┌──────────┐              │
       │synthesize│ ◄────────────┘
       └────┬─────┘
            │
            ▼
       ┌─────────┐
       │   END   │
       └─────────┘
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, Mapping, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


# ── Graph state ──────────────────────────────────────────────────
class AnalysisState(TypedDict, total=False):
    """Shared blackboard each node reads/writes.

    LangGraph's typed-dict state is the canonical way to thread
    intermediate results through nodes without resorting to globals
    or closures. Each field is updated by exactly one node — this
    keeps the dataflow obvious when a graph is rendered with
    :meth:`langgraph.graph.StateGraph.draw_mermaid`.
    """

    trade: Mapping[str, Any]
    analyst_view: str
    critic_view: str
    final_verdict: str
    quality_grade: Literal["good", "neutral", "bad"]


# ── Node implementations ─────────────────────────────────────────
def _make_llm() -> ChatAnthropic:
    return ChatAnthropic(
        model="claude-haiku-4-5",
        temperature=0.0,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        max_tokens=400,
    )


def analyst_node(state: AnalysisState) -> AnalysisState:
    """First-pass interpretation. No second-guessing yet."""
    trade = state["trade"]
    llm = _make_llm()
    msg = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a junior trading analyst. Given one "
                    "paper-trade row, produce a 2-sentence "
                    "interpretation: (1) why the bot entered, (2) "
                    "how the trade developed and why it exited. "
                    "Do not grade or recommend yet."
                )
            ),
            HumanMessage(content=f"Trade: {dict(trade)}"),
        ]
    )
    return {"analyst_view": str(msg.content).strip()}


def critic_node(state: AnalysisState) -> AnalysisState:
    """Steel-man the opposite view of the analyst.

    This node only fires for closed trades — see the conditional
    edge in :func:`build_analysis_graph`. The point is to surface
    blind spots: a trade with pnl_sol = -0.007 looks like a kill
    switch, but a one-off may also be entry-noise; the critic
    forces the synthesizer to consider both possibilities.
    """
    trade = state["trade"]
    analyst_view = state.get("analyst_view", "")
    llm = _make_llm()
    msg = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a senior trading reviewer. The junior "
                    "analyst gave the interpretation below. Your "
                    "job: in 2 sentences, challenge it. What blind "
                    "spot or alternative explanation did the "
                    "analyst miss? If you agree fully, say so "
                    "explicitly and stop."
                )
            ),
            HumanMessage(
                content=(
                    f"Trade: {dict(trade)}\n\n"
                    f"Analyst view: {analyst_view}"
                )
            ),
        ]
    )
    return {"critic_view": str(msg.content).strip()}


def synthesizer_node(state: AnalysisState) -> AnalysisState:
    """Combine analyst + critic (or analyst alone if open trade)
    into a final verdict + quality grade."""
    trade = state["trade"]
    analyst_view = state.get("analyst_view", "")
    critic_view = state.get("critic_view", "")
    llm = _make_llm()

    # Build the prompt depending on which views we have.
    bullets = [f"Analyst: {analyst_view}"]
    if critic_view:
        bullets.append(f"Critic: {critic_view}")
    body = "\n".join(bullets)

    msg = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You synthesize prior reviews into a final "
                    "verdict for the trades dashboard.\n\n"
                    "Return EXACTLY this format on three lines:\n"
                    "VERDICT: <2-3 sentences>\n"
                    "GRADE: <one of: good, neutral, bad>\n"
                    "FOLLOW_UP: <one short action or 'monitor only'>"
                )
            ),
            HumanMessage(
                content=f"Trade: {dict(trade)}\n\n{body}"
            ),
        ]
    )
    raw = str(msg.content).strip()

    verdict = raw
    grade: Literal["good", "neutral", "bad"] = "neutral"
    for line in raw.splitlines():
        line = line.strip()
        if line.upper().startswith("GRADE:"):
            tag = line.split(":", 1)[1].strip().lower()
            if tag in ("good", "neutral", "bad"):
                grade = tag  # type: ignore[assignment]

    return {"final_verdict": verdict, "quality_grade": grade}


# ── Conditional edge ─────────────────────────────────────────────
def _route_after_analyst(state: AnalysisState) -> str:
    """Skip critic for trades that haven't closed yet."""
    trade = state.get("trade") or {}
    exit_reason = (trade.get("exit_reason") or "").strip()
    if not exit_reason:
        return "synthesizer"
    return "critic"


# ── Graph builder ────────────────────────────────────────────────
def build_analysis_graph() -> Any:
    """Compile the StateGraph into a runnable.

    Returns:
        The compiled graph. Call ``.invoke({"trade": <row>})`` to
        execute end-to-end and receive the populated
        :class:`AnalysisState`.
    """
    graph = StateGraph(AnalysisState)
    graph.add_node("analyst", analyst_node)
    graph.add_node("critic", critic_node)
    graph.add_node("synthesizer", synthesizer_node)

    graph.add_edge(START, "analyst")
    graph.add_conditional_edges(
        "analyst",
        _route_after_analyst,
        {"critic": "critic", "synthesizer": "synthesizer"},
    )
    graph.add_edge("critic", "synthesizer")
    graph.add_edge("synthesizer", END)

    return graph.compile()


def run_analysis(trade: Mapping[str, Any]) -> AnalysisState:
    """One-shot helper for callers who don't want to manage the
    graph object.

    Args:
        trade: A paper_trade row as a mapping.

    Returns:
        The final :class:`AnalysisState` after all nodes have run.
    """
    g = build_analysis_graph()
    return g.invoke({"trade": trade})
