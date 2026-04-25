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
    h1 { display: none !important; }
    h3 { font-size: 14px !important; margin: 8px 0 4px 0 !important; padding: 0 !important; }
    .stRadio > div { gap: 0.3rem; }
    .stats-bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 8px 0; font-size: 13px; }
    .stats-bar .stat { background: #e8e8e8; color: #1a1a1a; padding: 4px 10px; border-radius: 6px; white-space: nowrap; }
    .stats-bar .stat b { color: #1a1a1a; }
    .stat-buy b { color: #16a34a !important; }
    .stat-skip b { color: #666 !important; }
    [data-testid="stAppViewContainer"][data-theme="dark"] .stats-bar .stat,
    .stApp[data-theme="dark"] .stats-bar .stat,
    [data-theme="dark"] .stats-bar .stat { background: #1e1e1e; color: #e0e0e0; }
    [data-testid="stAppViewContainer"][data-theme="dark"] .stats-bar .stat b,
    .stApp[data-theme="dark"] .stats-bar .stat b,
    [data-theme="dark"] .stats-bar .stat b { color: #e0e0e0; }
    [data-theme="dark"] .stat-buy b { color: #4ade80 !important; }
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
        rows = db.get_scores_last_hours(hours=24)
        stats = db.get_stats()
    else:
        rows = db.get_scores_by_date(date_str)
        stats = db.get_stats_by_date(date_str)

    # ── Stats as compact HTML bar ──────────────────────────
    render_stats_bar(stats)

    # ── P&L summary for BUY tokens ────────────────────────
    if rows:
        st.subheader("P&L Summary")
        render_pnl_summary(rows)

    # ── Charts ─────────────────────────────────────────────
    if rows:
        render_charts(rows)

    # ── Paper trades ───────────────────────────────────────
    if mode == "Live":
        st.subheader("Paper Trading")
        render_paper_trades(db)

    # ── Token table ────────────────────────────────────────
    if not rows:
        st.info("Waiting for data...")
    else:
        st.subheader("All Tokens")
        render_token_table(rows, db)

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


def render_pnl_summary(rows: list[dict]) -> None:
    """Show P&L summary for BUY tokens as a compact table."""
    buy_rows = [r for r in rows if r.get("decision") == "BUY"]
    if not buy_rows:
        return

    labels = [
        ("~5", "pnl_5th_pct"),
        ("~10", "pnl_10th_pct"),
        ("~20", "pnl_20th_pct"),
        ("~50", "pnl_50th_pct"),
        ("~100", "pnl_100th_pct"),
    ]

    pnl_rows = []
    for label, col in labels:
        vals = [r.get(col, 0) or 0 for r in buy_rows if r.get(col, 0)]
        if not vals:
            pnl_rows.append(
                {"Entry": label, "Win": 0, "Lose": 0, "W%": "—", "Net%": "—"}
            )
            continue
        n_win = sum(1 for v in vals if v > 0)
        n_lose = sum(1 for v in vals if v < 0)
        pos = sum(v for v in vals if v > 0)
        neg = sum(v for v in vals if v < 0)
        net = pos + neg
        wr = f"{n_win / len(vals) * 100:.0f}%" if vals else "—"
        pnl_rows.append(
            {
                "Entry": label,
                "Win": n_win,
                "Lose": n_lose,
                "W%": wr,
                "+Sum%": f"+{pos:.0f}%",
                "-Sum%": f"{neg:.0f}%",
                "Net%": f"{net:+.0f}%",
            }
        )

    pnl_df = pd.DataFrame(pnl_rows)
    st.dataframe(
        pnl_df, use_container_width=True, height=38 + 35 * len(pnl_df), hide_index=True
    )


def render_charts(rows: list[dict]) -> None:
    """Compact charts row: P&L distribution, decisions breakdown, cumulative P&L."""
    buy_rows = [r for r in rows if r.get("decision") == "BUY" and r.get("pnl_5th_pct")]

    c1, c2, c3 = st.columns(3)

    # Chart 1: P&L@5 distribution — green for wins, red for losses
    with c1:
        if buy_rows:
            pnl_vals = [r["pnl_5th_pct"] for r in buy_rows]
            wins = [v if v > 0 else 0 for v in pnl_vals]
            losses = [v if v < 0 else 0 for v in pnl_vals]
            chart_df = pd.DataFrame(
                {"win": wins, "loss": losses}, index=range(len(pnl_vals))
            )
            st.bar_chart(chart_df, height=180, color=["#4ade80", "#f87171"])
        else:
            st.caption("P&L@5 (no BUY data)")

    # Chart 2: Decision breakdown
    with c2:
        decisions: dict[str, int] = {}
        for r in rows:
            d = r.get("decision", "SKIP")
            decisions[d] = decisions.get(d, 0) + 1
        dec_df = pd.DataFrame(
            {"decision": list(decisions.keys()), "count": list(decisions.values())}
        )
        st.bar_chart(dec_df, x="decision", y="count", height=180, color="#6366f1")

    # Chart 3: Cumulative P&L over time (BUY tokens only)
    with c3:
        if buy_rows:
            sorted_buys = sorted(buy_rows, key=lambda r: r.get("scored_at", 0))
            cum_pnl = []
            total = 0.0
            for r in sorted_buys:
                total += r["pnl_5th_pct"]
                cum_pnl.append(total)
            cum_df = pd.DataFrame(
                {"token#": range(1, len(cum_pnl) + 1), "Cum P&L%": cum_pnl}
            )
            st.line_chart(cum_df, x="token#", y="Cum P&L%", height=180, color="#facc15")
        else:
            st.caption("Cumulative P&L (no BUY data)")


def render_paper_trades(db: Database) -> None:
    """Show balance summary, open positions, and closed trades tables."""
    open_trades = db.get_paper_trades(status="open")
    closed_trades = db.get_paper_trades(status="closed")

    if not open_trades and not closed_trades:
        return

    config = get_config()

    # ── Balance summary bar ───────────────────────────────
    initial_sol = config.portfolio_initial_sol
    closed_pnl_sol = sum(t.get("pnl_sol", 0) or 0 for t in closed_trades)
    open_invested = sum(t.get("buy_amount_sol", 0) or 0 for t in open_trades)
    current_sol = initial_sol + closed_pnl_sol - open_invested
    total_pnl_sol = closed_pnl_sol
    total_pnl_pct = (total_pnl_sol / initial_sol * 100) if initial_sol > 0 else 0

    wins = sum(1 for t in closed_trades if (t.get("pnl_pct", 0) or 0) > 0)
    wr = (wins / len(closed_trades) * 100) if closed_trades else 0
    pnl_color = "#16a34a" if total_pnl_sol >= 0 else "#dc2626"

    st.markdown(
        f"""<div class="stats-bar">
        <div class="stat">Start <b>{initial_sol:.3f} SOL</b></div>
        <div class="stat">Balance <b>{current_sol:.4f} SOL</b></div>
        <div class="stat">P&L <b style="color:{pnl_color}">{total_pnl_sol:+.4f} SOL ({total_pnl_pct:+.1f}%)</b></div>
        <div class="stat">Trades <b>{len(closed_trades)}</b> WR <b>{wr:.0f}%</b> ({wins}/{len(closed_trades)})</div>
        <div class="stat">Open <b>{len(open_trades)}</b></div>
        </div>""",
        unsafe_allow_html=True,
    )

    # Load scoring data for all paper trade mints
    all_mints = [t["mint"] for t in open_trades + closed_trades if t.get("mint")]
    scores_by_mint = _load_scores_for_mints(db, all_mints)

    # ── Open positions table ──────────────────────────────
    if open_trades:
        st.caption(f"Open Positions ({len(open_trades)})")
        now = time.time()
        open_rows = []
        for t in open_trades:
            pnl = t.get("current_pnl_pct", 0) or 0
            hold = now - (t.get("entry_time", 0) or 0)
            sc = scores_by_mint.get(t.get("mint", ""), {})
            open_rows.append(
                {
                    "Sym": t.get("symbol", "?"),
                    "Type": t.get("entry_type", "?"),
                    "Score": t.get("entry_score", 0),
                    "Fast": sc.get("fast_decision", "—"),
                    "Full": sc.get("decision", "—"),
                    "Uniq": sc.get("unique_buyers", 0),
                    "Vol": f"{sc.get('buy_volume_sol', 0) or 0:.1f}",
                    "Entry$": fmt_price(t.get("entry_price", 0) or 0),
                    "Buyer#": t.get("entry_buyer_number", 0),
                    "Now$": fmt_price(t.get("current_price", 0) or 0),
                    "P&L%": f"{pnl:+.1f}%",
                    "Hold": fmt_age(hold),
                    "B/S": f"{t.get('total_buys', 0)}/{t.get('total_sells', 0)}",
                    "SOL": f"{t.get('buy_amount_sol', 0):.3f}",
                }
            )
        open_df = pd.DataFrame(open_rows)

        def _color_open(row: pd.Series) -> list[str]:
            pnl_val = float(row["P&L%"].replace("%", "").replace("+", ""))
            if pnl_val > 0:
                i = min(pnl_val / 100, 1.0)
                return [
                    f"background-color: rgba(34,197,94,{0.08 + 0.22 * i}); color: #166534"
                ] * len(row)
            if pnl_val < 0:
                i = min(abs(pnl_val) / 100, 1.0)
                return [
                    f"background-color: rgba(239,68,68,{0.08 + 0.22 * i}); color: #991b1b"
                ] * len(row)
            return [""] * len(row)

        st.dataframe(
            open_df.style.apply(_color_open, axis=1),
            use_container_width=True,
            height=min(len(open_df) * 35 + 38, 300),
            hide_index=True,
        )

    # ── Closed trades table ───────────────────────────────
    if closed_trades:
        st.caption(f"Closed Trades ({len(closed_trades)})")
        closed_rows = []
        for t in closed_trades[:50]:
            pnl = t.get("pnl_pct", 0) or 0
            pnl_s = t.get("pnl_sol", 0) or 0
            hold = t.get("hold_seconds", 0) or 0
            sc = scores_by_mint.get(t.get("mint", ""), {})
            closed_rows.append(
                {
                    "Sym": t.get("symbol", "?"),
                    "Type": t.get("entry_type", "?"),
                    "Score": t.get("entry_score", 0),
                    "Fast": sc.get("fast_decision", "—"),
                    "Full": sc.get("decision", "—"),
                    "Uniq": sc.get("unique_buyers", 0),
                    "Vol": f"{sc.get('buy_volume_sol', 0) or 0:.1f}",
                    "Entry$": fmt_price(t.get("entry_price", 0) or 0),
                    "In#": t.get("entry_buyer_number", 0),
                    "Exit$": fmt_price(t.get("exit_price", 0) or 0),
                    "Out#": t.get("exit_buyer_number", 0),
                    "Reason": t.get("exit_reason", "?"),
                    "P&L%": f"{pnl:+.1f}%",
                    "P&L SOL": f"{pnl_s:+.5f}",
                    "Hold": fmt_age(hold),
                }
            )
        closed_df = pd.DataFrame(closed_rows)

        def _color_closed(row: pd.Series) -> list[str]:
            pnl_val = float(row["P&L%"].replace("%", "").replace("+", ""))
            if pnl_val > 0:
                i = min(pnl_val / 100, 1.0)
                return [
                    f"background-color: rgba(34,197,94,{0.08 + 0.22 * i}); color: #166534"
                ] * len(row)
            if pnl_val < 0:
                i = min(abs(pnl_val) / 100, 1.0)
                return [
                    f"background-color: rgba(239,68,68,{0.08 + 0.22 * i}); color: #991b1b"
                ] * len(row)
            return [""] * len(row)

        st.dataframe(
            closed_df.style.apply(_color_closed, axis=1),
            use_container_width=True,
            height=min(len(closed_df) * 35 + 38, 500),
            hide_index=True,
        )


def render_token_table(rows: list[dict], db: Database | None = None) -> None:
    """Render scored tokens as a colored dataframe with filter + pagination."""
    total_rows = len(rows)
    rows = sorted(rows, key=lambda r: r.get("scored_at", 0) or 0, reverse=True)

    f_col, p_col, info_col = st.columns([2, 1, 2])
    with f_col:
        decision_filter = st.selectbox(
            "Decision",
            ["All", "BUY", "BORDERLINE", "SKIP"],
            index=0,
            label_visibility="collapsed",
        )
    with p_col:
        page_size_label = st.selectbox(
            "Page",
            ["50", "200", "500", "2000", "All"],
            index=1,
            label_visibility="collapsed",
        )

    if decision_filter != "All":
        rows = [r for r in rows if r.get("decision") == decision_filter]

    filtered_count = len(rows)
    page_size = filtered_count if page_size_label == "All" else int(page_size_label)
    rows = rows[:page_size]

    with info_col:
        st.caption(
            f"Showing {len(rows)} of {filtered_count} filtered ({total_rows} total)"
        )

    if not rows:
        st.info("No tokens match the current filter.")
        return

    df = pd.DataFrame(rows)

    # Computed columns
    z = pd.Series([0.0] * len(df))
    zi = pd.Series([0] * len(df))
    zs = pd.Series([""] * len(df))

    now = time.time()
    SOL_USD = 130  # approximate SOL price, update as needed

    df["age"] = df["scored_at"].apply(lambda t: fmt_age(now - t) if t else "—")
    df["link"] = df["mint"].apply(lambda m: f"https://pump.fun/coin/{m}")
    df["mcap_usd"] = df.get("market_cap_sol", z).apply(
        lambda v: fmt_mcap_usd(v, SOL_USD)
    )
    df["vol"] = df["buy_volume_sol"].apply(lambda v: f"{v:.1f}")
    df["curve"] = df["curve_progress_pct"].apply(lambda p: f"{p:.0f}%")
    df["buys"] = df.get("buy_count", zi).apply(lambda x: str(int(x)))
    df["sells"] = df.get("sell_count", zi).apply(lambda x: str(int(x)))
    df["sp"] = df.get("sell_pressure", z).apply(lambda p: f"{p:.1f}" if p else "—")

    # Fast phase columns
    df["fast"] = df.get("fast_decision", zs).apply(lambda d: d if d else "—")
    df["f_sc"] = df.get("fast_score", zi).apply(lambda s: f"{int(s):+d}" if s else "—")
    df["f_buys"] = df.get("fast_buy_count", zi).apply(
        lambda x: str(int(x)) if x else "—"
    )
    df["f_sells"] = df.apply(
        lambda r: (
            str(
                int(
                    (r.get("fast_sell_ratio", 0) or 0)
                    * max(r.get("fast_buy_count", 0) or 0, 1)
                )
            )
            if r.get("fast_buy_count", 0)
            else "—"
        ),
        axis=1,
    )
    df["f_rate"] = df.get("fast_buy_rate", z).apply(lambda r: f"{r:.1f}" if r else "—")

    # Live P&L
    df["live_pnl"] = df.get("live_pnl_pct", z).apply(fmt_pnl)

    # P&L at entry points
    df["pnl5"] = df.get("pnl_5th_pct", z).apply(fmt_pnl)
    df["pnl10"] = df.get("pnl_10th_pct", z).apply(fmt_pnl)
    df["pnl20"] = df.get("pnl_20th_pct", z).apply(fmt_pnl)
    df["score_f"] = df["total_score"].apply(lambda s: f"{s:+d}")

    # Paper trade status per mint
    traded_mints: dict[str, str] = {}
    if db is not None:
        for t in db.get_paper_trades():
            traded_mints[t["mint"]] = t.get("status", "?")
    df["traded"] = df["mint"].apply(lambda m: traded_mints.get(m, "—"))

    # Select and rename
    display_df = df[
        [
            "age",
            "symbol",
            "link",
            "fast",
            "f_sc",
            "f_buys",
            "f_sells",
            "f_rate",
            "decision",
            "score_f",
            "live_pnl",
            "mcap_usd",
            "unique_buyers",
            "buys",
            "sells",
            "sp",
            "vol",
            "curve",
            "pnl5",
            "pnl10",
            "pnl20",
            "traded",
        ]
    ].copy()

    display_df.columns = pd.Index(
        [
            "Age",
            "Sym",
            "Link",
            "Fast",
            "F.Sc",
            "F.Buys",
            "F.Sells",
            "Rate/s",
            "Full",
            "Score",
            "Live",
            "MCap$",
            "Uniq",
            "Buys",
            "Sells",
            "SP",
            "Vol",
            "Curve",
            "~5",
            "~10",
            "~20",
            "Trade",
        ]
    )

    # Color by P&L: green if profitable, red if losing
    def _parse_pnl(val: str) -> float:
        if not val or val == "—":
            return 0.0
        try:
            return float(val.replace("%", "").replace("+", ""))
        except (ValueError, AttributeError):
            return 0.0

    def color_row(row: pd.Series) -> list[str]:
        # Only color BUY tokens
        full = row.get("Full", "")
        if full not in ("BUY", "BORDERLINE"):
            return [""] * len(row)

        # Check ~5, ~10, ~20 — use first non-zero
        pnl = 0.0
        for col in ["~5", "~10", "~20"]:
            v = _parse_pnl(row.get(col, "—"))
            if v != 0:
                pnl = v
                break

        if full == "BORDERLINE":
            return ["background-color: rgba(250,204,21,0.15); color: #a16207"] * len(
                row
            )

        if pnl > 0:
            intensity = min(pnl / 100, 1.0)
            alpha = 0.08 + 0.22 * intensity
            return [
                f"background-color: rgba(34,197,94,{alpha:.2f}); color: #166534"
            ] * len(row)
        if pnl < 0:
            intensity = min(abs(pnl) / 100, 1.0)
            alpha = 0.08 + 0.22 * intensity
            return [
                f"background-color: rgba(239,68,68,{alpha:.2f}); color: #991b1b"
            ] * len(row)
        return [""] * len(row)

    cell_count = len(display_df) * len(display_df.columns)
    data_for_render = (
        display_df.style.apply(color_row, axis=1)
        if cell_count <= 200_000
        else display_df
    )

    st.dataframe(
        data_for_render,
        use_container_width=True,
        height=min(len(display_df) * 35 + 38, 700),
        hide_index=True,
        column_config={
            **{
                c: st.column_config.TextColumn(width="small")
                for c in display_df.columns
            },
            "Link": st.column_config.LinkColumn(width="small", display_text="pump.fun"),
        },
    )


# ── Helpers ────────────────────────────────────────────────────


def _load_scores_for_mints(db: Database, mints: list[str]) -> dict[str, dict]:
    """Load token_scores for a list of mints. Returns {mint: score_dict}."""
    if not mints:
        return {}
    placeholders = ",".join(["?"] * len(mints))
    rows = db._sync_query(
        f"SELECT mint, fast_decision, fast_score, decision, total_score, "
        f"unique_buyers, buy_volume_sol, curve_progress_pct, sell_pressure "
        f"FROM token_scores WHERE mint IN ({placeholders}) AND source='live'",
        tuple(mints),
    )
    return {r["mint"]: r for r in rows}


def format_ts(ts: float) -> str:
    """Unix timestamp → HH:MM:SS."""
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_age(seconds: float) -> str:
    """Seconds ago → compact string."""
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def fmt_mcap_usd(mcap_sol: float, sol_usd: float) -> str:
    """Market cap in USD."""
    if not mcap_sol or mcap_sol <= 0:
        return "—"
    usd = mcap_sol * sol_usd
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.1f}M"
    if usd >= 1_000:
        return f"${usd / 1_000:.1f}K"
    return f"${usd:.0f}"


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
