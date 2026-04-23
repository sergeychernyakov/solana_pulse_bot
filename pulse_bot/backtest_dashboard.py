# pulse_bot/backtest_dashboard.py
"""Backtest Dashboard — shows optimizer results and trade analysis."""

from __future__ import annotations

import datetime
import json
import time

import pandas as pd
import streamlit as st

from pulse_bot.config import get_config
from pulse_bot.db import Database

st.set_page_config(
    page_title="Backtest Results", layout="wide", initial_sidebar_state="collapsed"
)

st.markdown(
    """
<style>
    .block-container { padding-top: 0.5rem; padding-left: 0.5rem; padding-right: 0.5rem; }
    .stats-bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 8px 0; font-size: 13px; }
    .stats-bar .stat { background: #e8e8e8; color: #1a1a1a; padding: 4px 10px; border-radius: 6px; white-space: nowrap; }
    .stats-bar .stat b { color: #1a1a1a; }
    .pos b { color: #16a34a !important; }
    .neg b { color: #dc2626 !important; }
    [data-theme="dark"] .stats-bar .stat { background: #1e1e1e; color: #e0e0e0; }
    [data-theme="dark"] .stats-bar .stat b { color: #e0e0e0; }
    [data-theme="dark"] .pos b { color: #4ade80 !important; }
    [data-theme="dark"] .neg b { color: #f87171 !important; }
</style>
""",
    unsafe_allow_html=True,
)


def main() -> None:
    """Backtest dashboard entry point."""
    config = get_config()
    db = Database(config.optimizer_db_path)
    db.init_schema()

    st.markdown("### Backtest Results")

    # ── Session selector ───────────────────────────────────
    sessions = db.get_optimization_sessions()

    if not sessions:
        st.info("No optimization runs yet. Run: `python main.py optimize`")
        time.sleep(5)
        st.rerun()
        return

    session_options = [
        f"{s['optimizer_session']} ({s['run_count']} runs, best PF={s['best_pf']:.2f})"
        for s in sessions
    ]
    selected_idx = st.selectbox(
        "Session", range(len(session_options)), format_func=lambda i: session_options[i]
    )
    selected_session = sessions[selected_idx]["optimizer_session"]

    # ── Load runs ──────────────────────────────────────────
    runs = db.get_optimization_runs(session=selected_session)

    if not runs:
        st.warning("No runs in this session.")
        return

    # ── Summary stats ──────────────────────────────────────
    render_session_summary(runs)

    # ── Runs table ─────────────────────────────────────────
    st.markdown("#### Runs ranked by Profit Factor")
    render_runs_table(runs)

    # ── Selected run detail ────────────────────────────────
    st.markdown("#### Trade Detail")
    run_options = [
        f"#{i+1} PF={r['profit_factor']:.2f} WR={r['win_rate']:.0f}% ROI={r['roi_pct']:+.1f}% | {json.loads(r['params'])}"
        for i, r in enumerate(runs)
    ]
    selected_run_idx = st.selectbox(
        "Select run", range(len(run_options)), format_func=lambda i: run_options[i]
    )
    selected_run = runs[selected_run_idx]

    render_run_detail(selected_run, db)

    # ── Auto-refresh ───────────────────────────────────────
    if st.checkbox("Auto-refresh (while optimizer runs)", value=False):
        time.sleep(5)
        st.rerun()


def render_session_summary(runs: list[dict]) -> None:
    """Summary stats bar for the session."""
    total = len(runs)
    profitable = sum(1 for r in runs if r["total_pnl_sol"] > 0)
    best_pf = max(r["profit_factor"] for r in runs)
    best_roi = max(r["roi_pct"] for r in runs)
    best_wr = max(r["win_rate"] for r in runs)
    avg_pnl = sum(r["total_pnl_sol"] for r in runs) / total

    pnl_cls = "pos" if avg_pnl > 0 else "neg"
    roi_cls = "pos" if best_roi > 0 else "neg"

    st.markdown(
        f"""<div class="stats-bar">
        <div class="stat">Runs <b>{total}</b></div>
        <div class="stat pos">Profitable <b>{profitable}/{total}</b></div>
        <div class="stat pos">Best PF <b>{best_pf:.2f}</b></div>
        <div class="stat {roi_cls}">Best ROI <b>{best_roi:+.1f}%</b></div>
        <div class="stat pos">Best WR <b>{best_wr:.0f}%</b></div>
        <div class="stat {pnl_cls}">Avg PnL <b>{avg_pnl:+.4f} SOL</b></div>
    </div>""",
        unsafe_allow_html=True,
    )


def render_runs_table(runs: list[dict]) -> None:
    """Render optimization runs as a sortable table."""
    rows = []
    for i, r in enumerate(runs):
        params = json.loads(r["params"])
        rows.append(
            {
                "#": i + 1,
                "Entry": r["entry_mode"],
                "Trades": r["total_trades"],
                "Wins": r["wins"],
                "Losses": r["losses"],
                "WR%": f"{r['win_rate']:.0f}",
                "PnL SOL": f"{r['total_pnl_sol']:+.4f}",
                "PF": f"{r['profit_factor']:.2f}",
                "ROI%": f"{r['roi_pct']:+.1f}",
                "DD%": f"{r['max_drawdown_pct']:.0f}",
                "Avg Win": f"+{r['avg_win_pct']:.0f}%",
                "Avg Loss": f"-{r['avg_loss_pct']:.0f}%",
                "Hold": f"{r['avg_hold_seconds']:.0f}s",
                **{k: str(v) for k, v in params.items()},
            }
        )

    df = pd.DataFrame(rows)

    def color_row(row: pd.Series) -> list[str]:
        pnl = float(row.get("PnL SOL", "0").replace("+", ""))
        if pnl > 0:
            return ["background-color: rgba(34,197,94,0.15); color: #166534"] * len(row)
        if pnl < -0.01:
            return ["background-color: rgba(239,68,68,0.15); color: #991b1b"] * len(row)
        return [""] * len(row)

    styled = df.style.apply(color_row, axis=1)
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        height=min(len(df) * 35 + 38, 500),
    )


def render_run_detail(run: dict, db: Database) -> None:
    """Show detailed view of a single optimization run."""
    params = json.loads(run["params"])
    exit_reasons = json.loads(run.get("exit_reasons", "{}"))

    # Params + metrics side by side
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Parameters:**")
        for k, v in params.items():
            st.text(f"  {k}: {v}")

    with col2:
        st.markdown("**Exit Reasons:**")
        for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
            pct = count / max(run["total_trades"], 1) * 100
            st.text(f"  {reason}: {count} ({pct:.0f}%)")

    # Trades table
    trades = db.get_optimization_trades(run["run_id"])
    if not trades:
        # Fallback to trades_json
        trades_json = run.get("trades_json", "[]")
        trades = json.loads(trades_json) if trades_json else []

    if not trades:
        st.info("No trade details available.")
        return

    df = pd.DataFrame(trades)

    if "entry_time" in df.columns:
        df["entry"] = df["entry_time"].apply(
            lambda t: (
                datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else ""
            )
        )
    if "pnl_pct" in df.columns:
        df["pnl"] = df["pnl_pct"].apply(lambda p: f"{p:+.1f}%")
    if "pnl_sol" in df.columns:
        df["pnl_s"] = df["pnl_sol"].apply(lambda s: f"{s:+.4f}")
    if "hold_seconds" in df.columns:
        df["hold"] = df["hold_seconds"].apply(lambda s: f"{s:.0f}s")

    display_cols = []
    col_map = {
        "entry": "Time",
        "symbol": "Sym",
        "entry_type": "Entry",
        "pnl": "P&L%",
        "pnl_s": "P&L SOL",
        "hold": "Hold",
        "exit_reason": "Exit",
        "sol_invested": "Invested",
        "sol_received": "Received",
    }
    for col, name in col_map.items():
        if col in df.columns:
            display_cols.append(col)

    if display_cols:
        show_df = df[display_cols].copy()
        show_df.columns = [col_map[c] for c in display_cols]

        def color_trade(row: pd.Series) -> list[str]:
            pnl_str = row.get("P&L%", "+0")
            try:
                val = float(pnl_str.replace("%", "").replace("+", ""))
            except (ValueError, AttributeError):
                val = 0
            if val > 0:
                return ["background-color: rgba(34,197,94,0.15); color: #166534"] * len(
                    row
                )
            if val < -10:
                return ["background-color: rgba(239,68,68,0.15); color: #991b1b"] * len(
                    row
                )
            return [""] * len(row)

        styled = show_df.style.apply(color_trade, axis=1)
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            height=min(len(show_df) * 35 + 38, 400),
        )


if __name__ == "__main__":
    main()
