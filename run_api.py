# run_api.py

"""
Entry point for running the FastAPI application.

Run with: python run_api.py
Or with uvicorn: uvicorn src.api.main:app --reload
"""

import uvicorn

from src.config.settings import config


def main() -> None:
    """
    Run the FastAPI application with uvicorn.

    Configuration loaded from environment variables via settings module.
    In development mode, auto-reload is enabled for code changes.
    """
    uvicorn.run(
        "src.api.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        log_level="debug" if config.DEBUG else "info"
    )


if __name__ == "__main__":
    main()
