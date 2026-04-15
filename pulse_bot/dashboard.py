# pulse_bot/dashboard.py
"""Streamlit dashboard for monitoring Pulse Bot token scoring in real-time."""

from __future__ import annotations

import datetime
import time

import pandas as pd
import streamlit as st

from pulse_bot.config import get_config
from pulse_bot.db import Database

# ── Page config ────────────────────────────────────────────────

st.set_page_config(
    page_title="Pulse Bot",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Compact CSS for mobile
st.markdown(
    """
<style>
    .block-container { padding-top: 0.5rem; padding-bottom: 0rem; padding-left: 0.5rem; padding-right: 0.5rem; }
    [data-testid="stMetric"], [data-testid="stMetricValue"], [data-testid="stMetricLabel"] { display: none; }
    h1, h2, h3 { display: none !important; }
    .stRadio > div { gap: 0.3rem; }
    .stats-bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 8px 0; font-size: 13px; }
    .stats-bar .stat { background: #1e1e1e; padding: 4px 10px; border-radius: 6px; white-space: nowrap; }
    .stats-bar .stat b { color: #ccc; }
    .stat-buy b { color: #4ade80 !important; }
    .stat-skip b { color: #888 !important; }
</style>
""",
    unsafe_allow_html=True,
)


def main() -> None:
    """Streamlit app entry point."""
    config = get_config()
    db = Database(config.db_path)
    db.init_schema()

    # ── Mode selector ──────────────────────────────────────
    mode = st.radio(
        "", ["Live", "History"], horizontal=True, label_visibility="collapsed"
    )

    date_str = ""
    if mode == "History":
        available_dates = db.get_available_dates()
        if available_dates:
            date_str = (
                st.selectbox("Date", available_dates, label_visibility="collapsed")
                or ""
            )
        else:
            st.info("No historical data yet.")
            return

    # ── Load data ──────────────────────────────────────────
    if mode == "Live":
        rows = db.get_recent_scores(limit=200)
        stats = db.get_stats()
    else:
        rows = db.get_scores_by_date(date_str)
        stats = db.get_stats_by_date(date_str)

    # ── Stats as compact HTML bar ──────────────────────────
    render_stats_bar(stats)

    # ── Token table ────────────────────────────────────────
    if not rows:
        st.info("Waiting for data...")
    else:
        render_token_table(rows)

    # ── Auto-refresh ───────────────────────────────────────
    if mode == "Live":
        time.sleep(config.dashboard_refresh_seconds)
        st.rerun()


# ── Components ─────────────────────────────────────────────────


def render_stats_bar(stats: dict) -> None:
    """Compact stats as a single HTML row — works on mobile."""
    total = stats.get("total_seen", 0) or 0
    buy = stats.get("total_buy", 0) or 0
    skip = stats.get("total_skip", 0) or 0
    borderline = stats.get("total_borderline", 0) or 0
    filt = f"{((skip + borderline) / total * 100):.0f}%" if total > 0 else "—"

    fast = stats.get("total_fast_buy", 0) or 0

    st.markdown(
        f"""<div class="stats-bar">
        <div class="stat">Seen <b>{total}</b></div>
        <div class="stat stat-buy">FAST <b>{fast}</b></div>
        <div class="stat stat-buy">BUY <b>{buy}</b></div>
        <div class="stat">BORDER <b>{borderline}</b></div>
        <div class="stat stat-skip">SKIP <b>{skip}</b></div>
        <div class="stat">Filter <b>{filt}</b></div>
        </div>""",
        unsafe_allow_html=True,
    )


def render_token_table(rows: list[dict]) -> None:
    """Render scored tokens as a colored dataframe."""
    df = pd.DataFrame(rows)

    # Computed columns
    z = pd.Series([0.0] * len(df))
    zi = pd.Series([0] * len(df))
    zs = pd.Series([""] * len(df))

    df["time"] = df["scored_at"].apply(format_ts)
    df["mint_s"] = df["mint"].apply(lambda m: m[:6] + "..")
    df["mcap"] = df.get("market_cap_sol", z).apply(fmt_mcap)
    df["price"] = df.get("token_price_sol", z).apply(fmt_price)
    df["vol"] = df["buy_volume_sol"].apply(lambda v: f"{v:.1f}")
    df["curve"] = df["curve_progress_pct"].apply(lambda p: f"{p:.0f}%")
    df["buys"] = df.get("buy_count", zi).apply(lambda x: str(int(x)))
    df["sells"] = df.get("sell_count", zi).apply(lambda x: str(int(x)))

    # Fast phase columns
    df["fast"] = df.get("fast_decision", zs).apply(lambda d: d if d else "—")
    df["f_sc"] = df.get("fast_score", zi).apply(lambda s: f"{int(s):+d}" if s else "—")
    df["f_buys"] = df.get("fast_buy_count", zi).apply(
        lambda x: str(int(x)) if x else "—"
    )
    df["f_rate"] = df.get("fast_buy_rate", z).apply(lambda r: f"{r:.1f}" if r else "—")
    df["pnl_f"] = df.get("pnl_at_fast_entry_pct", z).apply(fmt_pnl)

    # Live P&L
    df["live_pnl"] = df.get("live_pnl_pct", z).apply(fmt_pnl)

    # Full phase P&L
    df["pnl5"] = df.get("pnl_5th_pct", z).apply(fmt_pnl)
    df["pnl10"] = df.get("pnl_10th_pct", z).apply(fmt_pnl)
    df["pnl20"] = df.get("pnl_20th_pct", z).apply(fmt_pnl)
    df["pnl50"] = df.get("pnl_50th_pct", z).apply(fmt_pnl)
    df["pnl100"] = df.get("pnl_100th_pct", z).apply(fmt_pnl)
    df["score_f"] = df["total_score"].apply(lambda s: f"{s:+d}")

    # Select and rename
    display_df = df[
        [
            "time",
            "symbol",
            "mint_s",
            "fast",
            "f_sc",
            "f_buys",
            "f_rate",
            "pnl_f",
            "live_pnl",
            "mcap",
            "unique_buyers",
            "buys",
            "sells",
            "vol",
            "curve",
            "pnl5",
            "pnl10",
            "pnl20",
            "pnl50",
            "pnl100",
            "score_f",
            "decision",
        ]
    ].copy()

    display_df.columns = pd.Index(
        [
            "Time",
            "Sym",
            "Mint",
            "Fast",
            "F.Sc",
            "F.Buys",
            "Rate/s",
            "F.P&L",
            "Live",
            "MCap",
            "Uniq",
            "Buys",
            "Sells",
            "Vol",
            "Curve",
            "~5",
            "~10",
            "~20",
            "~50",
            "~100",
            "Score",
            "Full",
        ]
    )

    # Color by decision + live P&L
    def color_row(row: pd.Series) -> list[str]:
        full = row.get("Full", "")
        fast = row.get("Fast", "")
        live = row.get("Live", "—")

        # Parse live P&L
        is_losing = False
        if live and live != "—":
            try:
                pnl_val = float(live.replace("%", "").replace("+", ""))
                is_losing = pnl_val < 0
            except (ValueError, AttributeError):
                pass

        # BUY losing → red
        if full == "BUY" and is_losing:
            return ["background-color: #3a1a1a; color: #f87171"] * len(row)
        # BUY + FAST_BUY winning → bright green
        if full == "BUY" and fast == "FAST_BUY":
            return ["background-color: #0a4a0a; color: #6eff6e"] * len(row)
        # BUY winning → green
        if full == "BUY":
            return ["background-color: #1a3a1a; color: #4ade80"] * len(row)
        # BORDERLINE → yellow
        if full == "BORDERLINE":
            return ["background-color: #3a3a1a; color: #facc15"] * len(row)
        return [""] * len(row)

    styled = display_df.style.apply(color_row, axis=1)

    st.dataframe(
        styled,
        use_container_width=True,
        height=min(len(display_df) * 35 + 38, 700),
        hide_index=True,
        column_config={
            c: st.column_config.TextColumn(width="small") for c in display_df.columns
        },
    )


# ── Helpers ────────────────────────────────────────────────────


def format_ts(ts: float) -> str:
    """Unix timestamp → HH:MM:SS."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_mcap(val: float) -> str:
    """Format market cap in SOL."""
    if not val or val <= 0:
        return "—"
    if val >= 1000:
        return f"{val / 1000:.1f}K"
    return f"{val:.1f}"


def fmt_pnl(val: float) -> str:
    """Format P&L percentage."""
    if not val:
        return "—"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.0f}%"


def fmt_price(val: float) -> str:
    """Format token price in SOL (very small numbers)."""
    if not val or val <= 0:
        return "—"
    if val >= 0.001:
        return f"{val:.4f}"
    # Count leading zeros after decimal
    s = f"{val:.15f}"
    zeros = 0
    for ch in s[2:]:
        if ch == "0":
            zeros += 1
        else:
            break
    significant = f"{val * 10**(zeros+1):.2f}"
    return f"0.0({zeros}){significant}"


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
