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
    elif command == "dashboard":
        print("Run the dashboard with: streamlit run pulse_bot/dashboard.py")
        sys.exit(0)
    else:
        print("Usage:")
        print("  python main.py monitor     — start the token monitoring pipeline")
        print("  streamlit run pulse_bot/dashboard.py  — start the dashboard")
        sys.exit(0)


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


if __name__ == "__main__":
    main()
