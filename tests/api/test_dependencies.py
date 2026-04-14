# tests/api/test_dependencies.py

"""
Test API dependencies.

This module tests FastAPI dependency injection functions.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session, get_task_service


@pytest.mark.asyncio
async def test_get_db_session_dependency() -> None:
    """Test database session dependency generator."""
    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        assert session is not None
        assert isinstance(session, AsyncSession)
    finally:
        try:
            await session_gen.asend(None)
        except StopAsyncIteration:
            pass


def test_get_task_service_dependency() -> None:
    """Test task service dependency function."""
    from src.services.task_service import TaskService

    service = get_task_service()

    assert service is not None
    assert isinstance(service, TaskService)
