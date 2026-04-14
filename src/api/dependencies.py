# src/api/dependencies.py

"""
Dependency injection for FastAPI.

This module provides dependency functions for route handlers,
managing database sessions and service instances.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import get_async_session as db_get_async_session
from src.services.task_service import TaskService


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide database session for request.

    Wraps the database session generator for FastAPI dependency injection.
    Session automatically commits on success and rolls back on error.

    Yields:
        AsyncSession: Database session

    Example:
        @app.get("/tasks")
        async def get_tasks(session: AsyncSession = Depends(get_db_session)):
            # Use session here
            pass
    """
    async for session in db_get_async_session():
        yield session


def get_task_service() -> TaskService:
    """
    Provide task service instance.

    Creates a new TaskService instance for each request.
    Services are stateless, so this is safe.

    Returns:
        TaskService: Service instance

    Example:
        @app.get("/tasks")
        async def get_tasks(service: TaskService = Depends(get_task_service)):
            # Use service here
            pass
    """
    return TaskService()
