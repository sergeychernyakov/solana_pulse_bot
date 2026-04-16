# tests/pulse_bot/conftest.py
"""Pytest configuration for pulse_bot tests.

Loads .env file so HELIUS_API_KEY and other secrets are available during tests.
"""

from pathlib import Path


def pytest_configure() -> None:
    """Load .env from project root before tests run."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        import os

        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
