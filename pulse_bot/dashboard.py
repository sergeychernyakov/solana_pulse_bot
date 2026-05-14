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
    closed_trades_all = db.get_paper_trades(status="closed")

    # 2026-04-27: filter out tracking-only rows. The bot logs a paper_trade
    # for EVERY scored mint as a "what-if" simulation, regardless of whether
    # it actually bought. Real entries have entry_type ∈ {'fast', 'full',
    # ...} and entry_buyer_number > 0. Shadow rows have entry_type=NULL/''
    # and entry_buyer_number=0. Of 9242 historical closed trades, only ~30
    # are real bot entries (29 'full' + 1 'fast', avg PnL +32%).
    show_tracking = st.checkbox(
        "Show tracking-only rows (shadow simulations)",
        value=False,
        help=(
            "Bot logs a what-if simulation for every scored mint. Only "
            "rows where entry_buyer_number>0 are actual bot entries — "
            "rest are shadow-tracking. Hidden by default."
        ),
    )
    if show_tracking:
        closed_trades = closed_trades_all
    else:
        # Real bot entries are identified by entry_type set to a known
        # value: 'fast', 'full', 'ml_override', 't30', 'timing', etc.
        # Shadow-tracking rows have entry_type='' / NULL. Also exclude
        # synthetic seed-data with timestamp 2026-01-01 00:16:40.
        REAL_TRADES_FROM = 1743465600  # 2026-04-01 00:00 UTC
        REAL_ENTRY_TYPES = {
            "fast",
            "full",
            "ml_override",
            "t30",
            "t30_skip",
            "timing",
            "BUY_EARLY",
        }

        def _is_real(t: dict) -> bool:
            etype = (t.get("entry_type") or "").lower()
            if etype in REAL_ENTRY_TYPES:
                return True
            # Legacy fallback for pre-2026-04-28 entries when entry_type
            # wasn't always set: trust entry_buyer_number > 0.
            return (t.get("entry_buyer_number", 0) or 0) > 0

        closed_trades = [
            t
            for t in closed_trades_all
            if _is_real(t) and (t.get("entry_time", 0) or 0) >= REAL_TRADES_FROM
        ]

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

    # ── Per-config A/B breakdown (2026-05-14 multi-config) ─────────
    # Each entry config opens its own tagged paper trades from one
    # shared WS stream. Grouping by config_id reads the A/B at a glance.
    # Shown only when more than one config has trades — single-config
    # bots see no extra noise.
    by_cfg: dict[str, dict] = {}
    for t in closed_trades:
        cid = t.get("config_id") or "LIVE"
        b = by_cfg.setdefault(
            cid, {"n": 0, "wins": 0, "pnl_sol": 0.0, "pnl_pct_sum": 0.0}
        )
        b["n"] += 1
        if (t.get("pnl_pct", 0) or 0) > 0:
            b["wins"] += 1
        b["pnl_sol"] += t.get("pnl_sol", 0) or 0
        b["pnl_pct_sum"] += t.get("pnl_pct", 0) or 0
    open_by_cfg: dict[str, int] = {}
    for t in open_trades:
        cid = t.get("config_id") or "LIVE"
        open_by_cfg[cid] = open_by_cfg.get(cid, 0) + 1
    all_cfg_ids = set(by_cfg) | set(open_by_cfg)
    if len(all_cfg_ids) > 1:
        cfg_rows = []
        for cid in sorted(all_cfg_ids):
            b = by_cfg.get(cid)
            n = b["n"] if b else 0
            cfg_rows.append(
                {
                    "Config": cid,
                    "Closed": n,
                    "Open": open_by_cfg.get(cid, 0),
                    "WR %": round(b["wins"] / n * 100, 1) if n else 0.0,
                    "P&L SOL": round(b["pnl_sol"], 4) if b else 0.0,
                    "Avg P&L %": round(b["pnl_pct_sum"] / n, 1) if n else 0.0,
                }
            )
        st.caption("Per-config A/B breakdown")
        st.dataframe(
            pd.DataFrame(cfg_rows),
            use_container_width=True,
            height=38 + 35 * len(cfg_rows),
            hide_index=True,
        )

    # Load scoring data for all paper trade mints
    all_mints = [t["mint"] for t in open_trades + closed_trades if t.get("mint")]
    scores_by_mint = _load_scores_for_mints(db, all_mints)

    # ── Open positions table ──────────────────────────────
    if not open_trades:
        # Show diagnostic instead of hiding the section. Helps detect
        # "bot scoring fine but never buying" vs "bot stuck/dead".
        try:
            recent = db._sync_query(
                "SELECT COUNT(*) AS n, MAX(ml_entry_proba) AS mx, "
                "AVG(ml_entry_proba) AS av "
                "FROM token_scores WHERE source = 'live' "
                "AND scored_at > ? AND ml_entry_proba IS NOT NULL",
                (time.time() - 1800,),
            )
            r = recent[0] if recent else {}
            n = r.get("n") or 0
            mx = r.get("mx") or 0.0
            av = r.get("av") or 0.0
            st.caption(
                f"Open Positions (0) — bot scored {n} tokens last 30 min, "
                f"max ML proba {mx:.3f} / avg {av:.3f} "
                f"(buy ceiling = {config.entry_ml_proba_ceiling:.2f})"
            )
        except Exception:
            st.caption("Open Positions (0)")
    if open_trades:
        st.caption(f"Open Positions ({len(open_trades)})")
        now = time.time()
        open_rows = []
        for t in open_trades:
            pnl = t.get("current_pnl_pct", 0) or 0
            hold = now - (t.get("entry_time", 0) or 0)
            sc = scores_by_mint.get(t.get("mint", ""), {})
            ml_proba = sc.get("ml_entry_proba")
            ml_proba_str = f"{float(ml_proba):.2f}" if ml_proba is not None else "—"
            entry_mc = t.get("entry_mcap_sol", 0) or 0
            cur_mc = t.get("current_mcap_sol", 0) or 0
            mc_str = f"{entry_mc:.1f}→{cur_mc:.1f}" if entry_mc else "—"
            open_rows.append(
                {
                    "Sym": t.get("symbol", "?"),
                    "Path": _classify_entry_path(t, sc),
                    "Score": t.get("entry_score", 0),
                    "Pred PnL": _decode_reg_pnl(t),
                    "ML p": ml_proba_str,
                    "Fast": sc.get("fast_decision", "—"),
                    "Full": sc.get("decision", "—"),
                    "Why?": _short_reason(sc.get("reasons")),
                    "Uniq": sc.get("unique_buyers", 0),
                    "Vol": f"{sc.get('buy_volume_sol', 0) or 0:.1f}",
                    "MC SOL": mc_str,
                    "Buyer#": t.get("entry_buyer_number", 0),
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
            ml_proba = sc.get("ml_entry_proba")
            ml_proba_str = f"{float(ml_proba):.2f}" if ml_proba is not None else "—"
            entry_mc = t.get("entry_mcap_sol", 0) or 0
            exit_mc = t.get("exit_mcap_sol", 0) or 0
            mc_str = f"{entry_mc:.1f}→{exit_mc:.1f}" if entry_mc else "—"
            closed_rows.append(
                {
                    "Sym": t.get("symbol", "?"),
                    "Cfg": t.get("config_id") or "LIVE",
                    "Path": _classify_entry_path(t, sc),
                    "Score": t.get("entry_score", 0),
                    "Pred PnL": _decode_reg_pnl(t),
                    "ML p": ml_proba_str,
                    "Fast": sc.get("fast_decision", "—"),
                    "Full": sc.get("decision", "—"),
                    "Why?": _short_reason(sc.get("reasons")),
                    "Uniq": sc.get("unique_buyers", 0),
                    "Vol": f"{sc.get('buy_volume_sol', 0) or 0:.1f}",
                    "MC SOL": mc_str,
                    "In#": t.get("entry_buyer_number", 0),
                    "Out#": t.get("exit_buyer_number", 0),
                    "Exit Reason": t.get("exit_reason", "?"),
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
    """Load token_scores for a list of mints. Returns {mint: score_dict}.

    2026-04-27: extended to include reasoning columns — ml_entry_proba
    (what ML model said at scoring), reasons + fast_reasons (which hard
    filters fired), creator_score + creator_reason. Used by Closed Trades
    panel to expose entry-decision context.
    """
    if not mints:
        return {}
    placeholders = ",".join(["?"] * len(mints))
    rows = db._sync_query(
        f"SELECT mint, fast_decision, fast_score, decision, total_score, "
        f"unique_buyers, buy_volume_sol, curve_progress_pct, sell_pressure, "
        f"ml_entry_proba, ml_model_hash, reasons, fast_reasons, "
        f"creator_score, creator_reason "
        f"FROM token_scores WHERE mint IN ({placeholders}) AND source='live'",
        tuple(mints),
    )
    return {r["mint"]: r for r in rows}


def _decode_reg_pnl(trade: dict) -> str:
    """For ml_override entries, decode entry_score back to predicted_pnl_pct.

    Encoding (decision_service.apply_ml_override 2026-04-29):
        entry_score = round(reg_pnl_pct × 10) + 500  → range [1, 999]
        decode:        (entry_score - 500) / 10.0

    Returns the decoded forecast as ``"+N.N%"`` string when applicable.
    For non-ml_override or pre-encoding rows returns ``"—"`` (legacy
    int(ml_cal × 100) values are in [1, 100] range, not centered on 500,
    so they decode to nonsensical large negatives — this filter avoids
    showing misleading data on old rows).
    """
    et = (trade.get("entry_type") or "").lower()
    if et != "ml_override":
        return "—"
    score = trade.get("entry_score")
    if score is None or not isinstance(score, (int, float)):
        return "—"
    score = int(score)
    # New encoding range — sits roughly in [400, 700] for typical
    # predictions. Old encoding (int(ml_cal*100)) is in [1, 100].
    # Treat anything below 200 as legacy and skip decoding.
    if score < 200:
        return "—"
    pnl = (score - 500) / 10.0
    return f"{pnl:+.1f}%"


def _classify_entry_path(trade: dict, score: dict) -> str:
    """Determine how the bot entered this position. Returns short label
    suitable for a narrow column.

    Possible labels:
    - "FAST"          — entered via fast-phase BUY at T+5s
    - "FULL"          — entered via full-phase BUY at T+90s (rules agreed)
    - "ML_OVERRIDE"   — full said SKIP/BORDERLINE, but ML proba >= ceiling
                        forced BUY (rules ⇏ buy, ML says yes)
    - "T30_BUY"       — Phase-3 T+30 model fired early BUY
    - "?"             — no scoring row found (legacy / lost write)
    """
    et = (trade.get("entry_type") or "").lower()
    full_dec = (score.get("decision") or "").upper() if score else ""
    if et == "fast":
        return "FAST"
    if et == "full" and full_dec == "BUY":
        return "FULL"
    if et == "full" and full_dec in ("SKIP", "BORDERLINE", ""):
        # Bot entered via full path but rules engine said no — must be
        # ML override (PULSE_POLICY=hybrid + ML BUY) or T+30 early buy.
        return "ML_OVERRIDE"
    if not et and not full_dec:
        return "?"
    return et.upper() or "?"


def _short_reason(reasons_str: str | None, max_chars: int = 30) -> str:
    """Compact a reasons list (comma-or-semicolon separated) to its
    first-listed reason — usually the dominant fail. Empty → '—'."""
    if not reasons_str:
        return "—"
    s = str(reasons_str).strip()
    if not s:
        return "—"
    parts = s.replace(";", ",").split(",")
    first = parts[0].strip()
    if len(first) > max_chars:
        return first[: max_chars - 1] + "…"
    return first


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
