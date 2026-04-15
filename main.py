# main.py
"""Entry point for the Pulse Bot application."""

import asyncio
import logging
import sys

from pulse_bot.backtest import BacktestEngine
from pulse_bot.config import get_config
from pulse_bot.db import Database
from pulse_bot.filters.fast import FastFilter
from pulse_bot.filters.scorer import Scorer
from pulse_bot.launchpads.pumpfun import PumpFunLaunchpad
from pulse_bot.pipeline import Pipeline


def main() -> None:
    """Run the application based on the command argument."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "monitor":
        _run_monitor()
    elif command == "backtest":
        _run_backtest()
    elif command == "optimize":
        _run_optimize()
    elif command == "qa":
        _run_qa()
    elif command == "verify":
        _run_verify()
    elif command == "dashboard":
        _log_cli_message("Run the dashboard with: streamlit run pulse_bot/dashboard.py")
        sys.exit(0)
    else:
        _log_cli_message(
            "\n".join(
                [
                    "Usage:",
                    "  python main.py monitor     — start the token monitoring pipeline",
                    "  python main.py backtest    — run backtest on collected data",
                    "  python main.py optimize    — run grid search optimizer",
                    "  python main.py qa          — run all tests and quality checks",
                    "  python main.py verify      — compare live decisions vs backtest",
                    "  streamlit run pulse_bot/dashboard.py          — live dashboard",
                    "  streamlit run pulse_bot/backtest_dashboard.py — backtest results",
                ]
            )
        )
        sys.exit(0)


def _log_cli_message(message: str) -> None:
    """Log a CLI message when no command-specific logging is configured."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info(message)


def _run_monitor() -> None:
    """Start the async monitoring pipeline."""
    config = get_config()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(config.db_path)
    db.init_schema()

    launchpad = PumpFunLaunchpad(config)
    scorer = Scorer(config, db)
    fast_filter = FastFilter(config)
    pipeline = Pipeline(config, db, launchpad, scorer, fast_filter)

    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopped by user")


def _run_backtest() -> None:
    """Run backtest on collected historical data."""
    config = get_config()

    # Parse optional args
    args = sys.argv[2:]
    for arg in args:
        if arg.startswith("--entry="):
            config.entry_mode = arg.split("=")[1]
        elif arg.startswith("--buy="):
            config.buy_amount_sol = float(arg.split("=")[1])
        elif arg.startswith("--balance="):
            config.portfolio_initial_sol = float(arg.split("=")[1])
        elif arg.startswith("--fast-sec="):
            config.fast_observe_seconds = int(arg.split("=")[1])
        elif arg.startswith("--full-sec="):
            config.observe_seconds = int(arg.split("=")[1])
        elif arg.startswith("--db="):
            config.backtest_db_path = arg.split("=")[1]
        elif arg.startswith("--stop-loss="):
            config.exit_hard_stop_loss_pct = float(arg.split("=")[1])
        elif arg.startswith("--max-hold="):
            config.exit_max_hold_seconds = float(arg.split("=")[1])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(config.backtest_db_path)
    engine = BacktestEngine(config, db)
    result = engine.run()
    result.print_report()


def _run_verify() -> None:
    """Compare live pipeline decisions vs backtest decisions on same data."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)

    config = get_config()
    db = Database(config.db_path)
    db.init_schema()

    live = db.get_live_decisions()
    if not live:
        log.info("No live decisions recorded yet. Run 'python main.py monitor' first.")
        sys.exit(1)

    log.info("Live decisions: %d tokens", len(live))

    # Replay same tokens through backtest scorer
    from pulse_bot.clock import SimulatedClock
    from pulse_bot.sources.backtest import BacktestSource

    clock = SimulatedClock()
    source = BacktestSource(config.db_path, clock)
    fast_filter = FastFilter(config)
    scorer = Scorer(config, db)

    from pulse_bot.models import Token

    match_fast = 0
    mismatch_fast = 0
    match_full = 0
    mismatch_full = 0
    match_score = 0

    import sqlite3

    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row

    log.info("")
    log.info("%-14s %-10s %-10s %-6s %-10s %-10s %-6s %-6s", "Symbol", "Live Fast", "BT Fast", "F.Match", "Live Full", "BT Full", "Match", "Score")
    log.info("-" * 85)

    for ld in live:
        tok_row = conn.execute("SELECT * FROM tokens WHERE mint=?", (ld["mint"],)).fetchone()
        if not tok_row:
            continue

        token = Token(
            mint=tok_row["mint"], name=tok_row["name"] or "", symbol=tok_row["symbol"] or "",
            creator=tok_row["creator"] or "", created_at=tok_row["created_at"],
            uri=tok_row["uri"] or "", launchpad="pumpfun",
        )

        # Backtest fast
        fast_trades = source.get_fast_trades(token.mint, token.created_at, config.fast_observe_seconds)
        bt_fast = ""
        bt_fast_score = 0
        if fast_trades:
            fr = fast_filter.evaluate(token, fast_trades)
            bt_fast = fr.decision
            bt_fast_score = fr.score

        # Backtest full
        full_trades = source.get_full_trades(token.mint, token.created_at, config.observe_seconds)
        bt_full = ""
        bt_full_score = 0
        if full_trades:
            sr = scorer.score(token, full_trades)
            bt_full = sr.decision
            bt_full_score = sr.total_score

        # Compare
        live_fast = ld["fast_decision"] or ""
        live_full = ld["full_decision"] or ""

        fm = "=" if bt_fast == live_fast else "X"
        fm_full = "=" if bt_full == live_full else "X"
        sm = "=" if bt_full_score == ld["full_score"] else f"{ld['full_score']}!={bt_full_score}"

        if bt_fast == live_fast:
            match_fast += 1
        else:
            mismatch_fast += 1

        if bt_full == live_full:
            match_full += 1
        else:
            mismatch_full += 1

        if bt_full_score == ld["full_score"]:
            match_score += 1

        log.info(
            "%-14s %-10s %-10s %-6s %-10s %-10s %-6s %-6s",
            ld["symbol"][:14], live_fast, bt_fast, fm, live_full, bt_full, fm_full, sm,
        )

    conn.close()

    total = match_fast + mismatch_fast
    log.info("")
    log.info("=" * 85)
    log.info("FAST decisions:  %d/%d match (%.0f%%)", match_fast, total, match_fast / max(total, 1) * 100)
    log.info("FULL decisions:  %d/%d match (%.0f%%)", match_full, total, match_full / max(total, 1) * 100)
    log.info("FULL scores:     %d/%d exact match (%.0f%%)", match_score, total, match_score / max(total, 1) * 100)

    if mismatch_fast == 0 and mismatch_full == 0:
        log.info("RESULT: PERFECT MATCH — backtest = live bot")
    elif (match_fast + match_full) / max(total * 2, 1) > 0.95:
        log.info("RESULT: NEAR MATCH (>95%%) — minor differences on edge cases")
    else:
        log.info("RESULT: MISMATCH — investigate differences")
    log.info("=" * 85)


def _run_qa() -> None:
    """Run all tests and quality checks."""
    import subprocess

    checks = [
        ("Compile check", ["python", "-m", "compileall", "-q", "pulse_bot/", "src/"]),
        ("Ruff lint", ["python", "-m", "ruff", "check", "pulse_bot/", "src/"]),
        ("Black format check", ["python", "-m", "black", "--check", "--quiet", "pulse_bot/", "src/"]),
        ("isort check", ["python", "-m", "isort", "--check-only", "--quiet", "pulse_bot/", "src/"]),
        ("Pytest", ["python", "-m", "pytest", "-q", "--tb=short"]),
    ]

    passed = 0
    failed = 0

    for name, cmd in checks:
        print(f"\n{'─' * 40}")
        print(f"  {name}")
        print(f"{'─' * 40}")
        result = subprocess.run(cmd, capture_output=False)  # noqa: S603
        if result.returncode == 0:
            print(f"  ✓ {name} passed")
            passed += 1
        else:
            print(f"  ✗ {name} FAILED (exit {result.returncode})")
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"  QA: {passed} passed, {failed} failed")
    print(f"{'=' * 40}")
    sys.exit(1 if failed > 0 else 0)


def _run_optimize() -> None:
    """Run grid search optimizer."""
    from pulse_bot.optimizer import Optimizer

    config = get_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(config.backtest_db_path)
    db.init_schema()

    optimizer = Optimizer(config, db)

    # Parse custom grid from CLI args
    custom_grid: dict[str, list] = {}
    for arg in sys.argv[2:]:
        if "=" in arg and arg.startswith("--"):
            key = arg.split("=")[0].lstrip("-").replace("-", "_")
            values_str = arg.split("=")[1]
            # Parse comma-separated values
            values = []
            for v in values_str.split(","):
                v = v.strip()
                try:
                    if "." in v:
                        values.append(float(v))
                    else:
                        values.append(int(v))
                except ValueError:
                    values.append(v)
            custom_grid[key] = values

    if custom_grid:
        optimizer.set_grid(custom_grid)
        logging.getLogger(__name__).info("Custom grid: %s", custom_grid)
    else:
        optimizer.use_default_grid()

    logging.getLogger(__name__).info(
        "Starting optimizer: %d combinations, session=%s",
        optimizer.estimate_runs(), optimizer.session_id,
    )
    logging.getLogger(__name__).info(
        "View results: streamlit run pulse_bot/backtest_dashboard.py --server.port 8502",
    )

    optimizer.run()


if __name__ == "__main__":
    main()
