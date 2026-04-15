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
    db.init_schema()

    # Use ReplayLaunchpad — same Pipeline code as live, just different data source
    from pulse_bot.sources.replay import ReplayLaunchpad

    db.clear_backtest_scores()
    db.clear_creators()

    launchpad = ReplayLaunchpad(config.backtest_db_path, speed=0.0)  # instant replay
    scorer = Scorer(config, db)
    fast_filter = FastFilter(config)

    # Override source to 'backtest' for all scores
    original_score = scorer.score

    def score_as_backtest(*args, **kwargs):
        result = original_score(*args, **kwargs)
        result.source = "backtest"
        return result

    scorer.score = score_as_backtest

    pipeline = Pipeline(config, db, launchpad, scorer, fast_filter)

    # Pipeline writes to token_scores with source='backtest'
    # Monkeypatch pipeline to set source='backtest'
    original_handle = pipeline._handle_token

    async def handle_backtest(token):
        await original_handle(token)

    pipeline._handle_token = handle_backtest

    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        pass

    # Print summary from backtest scores
    bt_stats = db.get_stats(source="backtest")
    logging.getLogger(__name__).info(
        "Backtest done: %d tokens scored, BUY=%d, SKIP=%d, FAST_BUY=%d",
        bt_stats.get("total_seen") or 0, bt_stats.get("total_buy") or 0,
        bt_stats.get("total_skip") or 0, bt_stats.get("total_fast_buy") or 0,
    )


def _run_verify() -> None:
    """Run backtest, then compare live vs backtest from same token_scores table."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger(__name__)

    config = get_config()
    db = Database(config.db_path)
    db.init_schema()

    # Step 1: Check live data exists
    live_rows = db.get_recent_scores(limit=10000, source="live")
    if not live_rows:
        log.info("No live data. Run 'python main.py monitor' first, then 'python main.py verify'.")
        sys.exit(1)
    log.info("Live scores: %d tokens", len(live_rows))

    # Step 2: Clear backtest state and creator cache, then replay
    log.info("Clearing backtest scores and creator cache...")
    db.clear_backtest_scores()
    db.clear_creators()

    log.info("Running backtest (replay) on same data...")
    from pulse_bot.sources.replay import ReplayLaunchpad

    launchpad = ReplayLaunchpad(config.db_path, speed=0.0)
    scorer = Scorer(config, db)
    fast_filter = FastFilter(config)
    pipeline = Pipeline(config, db, launchpad, scorer, fast_filter)
    asyncio.run(pipeline.run())

    # Step 3: Compare live vs backtest from same table
    bt_rows = db.get_recent_scores(limit=10000, source="backtest")
    log.info("Backtest scores: %d tokens", len(bt_rows))

    # Index by mint
    live_by_mint = {r["mint"]: r for r in live_rows}
    bt_by_mint = {r["mint"]: r for r in bt_rows}

    common = set(live_by_mint.keys()) & set(bt_by_mint.keys())
    log.info("Common tokens: %d", len(common))

    match_fast = 0
    match_full = 0
    match_score = 0
    total = 0

    log.info("")
    log.info("%-14s %-10s %-10s %-5s %-10s %-10s %-5s %-12s", "Symbol", "Live Fast", "BT Fast", "F.OK", "Live Full", "BT Full", "OK", "Score L/BT")
    log.info("-" * 90)

    for mint in sorted(common, key=lambda m: live_by_mint[m].get("created_at", 0)):
        lv = live_by_mint[mint]
        bt = bt_by_mint[mint]
        total += 1

        lf = lv.get("fast_decision") or ""
        bf = bt.get("fast_decision") or ""
        ld = lv.get("decision") or ""
        bd = bt.get("decision") or ""
        ls = lv.get("total_score", 0)
        bs = bt.get("total_score", 0)

        fok = "=" if lf == bf else "X"
        dok = "=" if ld == bd else "X"
        sok = f"{ls}/{bs}" if ls == bs else f"{ls}!={bs}"

        if lf == bf:
            match_fast += 1
        if ld == bd:
            match_full += 1
        if ls == bs:
            match_score += 1

        sym = (lv.get("symbol") or "")[:14]
        log.info("%-14s %-10s %-10s %-5s %-10s %-10s %-5s %-12s", sym, lf, bf, fok, ld, bd, dok, sok)

    log.info("")
    log.info("=" * 90)
    log.info("Compared: %d tokens", total)
    log.info("FAST decisions:  %d/%d match (%.1f%%)", match_fast, total, match_fast / max(total, 1) * 100)
    log.info("FULL decisions:  %d/%d match (%.1f%%)", match_full, total, match_full / max(total, 1) * 100)
    log.info("FULL scores:     %d/%d exact (%.1f%%)", match_score, total, match_score / max(total, 1) * 100)

    if match_fast == total and match_full == total and match_score == total:
        log.info("RESULT: 100%% PERFECT MATCH")
    else:
        log.info("RESULT: %.1f%% match", (match_fast + match_full + match_score) / max(total * 3, 1) * 100)
    log.info("=" * 90)


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
