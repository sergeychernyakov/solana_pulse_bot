# main.py
"""Entry point for the Pulse Bot application."""

import asyncio
import logging
import sys


def main() -> None:
    """Run the application based on the command argument."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "monitor":
        _run_monitor()
    elif command == "backtest":
        _run_backtest()
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
                    "  streamlit run pulse_bot/dashboard.py  — start the dashboard",
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
    from pulse_bot.config import get_config
    from pulse_bot.db import Database
    from pulse_bot.filters.fast import FastFilter
    from pulse_bot.filters.scorer import Scorer
    from pulse_bot.launchpads.pumpfun import PumpFunLaunchpad
    from pulse_bot.pipeline import Pipeline

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
    from pulse_bot.backtest import BacktestEngine
    from pulse_bot.config import get_config
    from pulse_bot.db import Database

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


if __name__ == "__main__":
    main()
