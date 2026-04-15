# tests/api/v1/test_health.py

"""Unit tests for health-check route behavior."""

from typing import Any

import pytest

from src.api.v1.routes.health import health_check


class FakeHealthSession:
    """Minimal async session test double for health checks."""

    def __init__(self, should_fail: bool = False) -> None:
        """Initialize fake session behavior."""
        self.should_fail = should_fail

    async def execute(self, statement: Any) -> None:
        """Simulate database execution."""
        if self.should_fail:
            raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_health_check_reports_connected_database() -> None:
    """Test health response when database query succeeds."""
    response = await health_check(FakeHealthSession())

    assert response.status == "ok"
    assert response.database == "connected"


@pytest.mark.asyncio
async def test_health_check_reports_disconnected_database() -> None:
    """Test health response when database query fails."""
    response = await health_check(FakeHealthSession(should_fail=True))

    assert response.status == "ok"
    assert response.database == "disconnected"
