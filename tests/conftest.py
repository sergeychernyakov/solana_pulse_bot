# tests/conftest.py

"""
Global test fixtures for pytest.

This module provides reusable fixtures for testing including
async database sessions, HTTP clients, and sample data.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Generator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.api.dependencies import get_db_session
from src.api.main import app
from src.models.database_base import Base
from src.models.enums import PriorityEnum
from src.models.task import Task


# Configure pytest-asyncio
@pytest_asyncio.fixture(scope="function")
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """
    Create test database engine.

    Uses in-memory SQLite for fast, isolated tests.

    Yields:
        AsyncEngine: Test database engine
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Cleanup
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def async_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """
    Provide database session for tests.

    Each test gets a fresh session with rolled-back transactions
    for isolation.

    Args:
        test_engine: Test database engine

    Yields:
        AsyncSession: Database session for testing
    """
    async_session_maker = sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False, autocommit=False, autoflush=False
    )

    async with async_session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(scope="function")
async def async_client(async_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    Provide async HTTP client for API tests.

    Overrides the database session dependency to use test database.

    Args:
        async_session: Test database session

    Yields:
        AsyncClient: HTTP client for testing API endpoints
    """

    # Override dependency
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield async_session

    app.dependency_overrides[get_db_session] = override_get_db

    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client

    # Clear overrides
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_task(async_session: AsyncSession) -> Task:
    """
    Create a sample task for testing.

    Args:
        async_session: Database session

    Returns:
        Task: Created task instance
    """
    task = Task(title="Sample Task", description="Sample Description", priority=PriorityEnum.MEDIUM, completed=False)
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    return task


@pytest_asyncio.fixture
async def sample_tasks(async_session: AsyncSession) -> list[Task]:
    """
    Create multiple tasks for testing filters and pagination.

    Args:
        async_session: Database session

    Returns:
        list[Task]: List of created tasks
    """
    tasks = [
        Task(title="Task 1", priority=PriorityEnum.HIGH, completed=False),
        Task(title="Task 2", priority=PriorityEnum.MEDIUM, completed=True),
        Task(title="Task 3", priority=PriorityEnum.LOW, completed=False),
        Task(title="Task 4", priority=PriorityEnum.HIGH, completed=True),
        Task(title="Buy groceries", priority=PriorityEnum.MEDIUM, completed=False),
    ]

    for task in tasks:
        async_session.add(task)

    await async_session.commit()

    for task in tasks:
        await async_session.refresh(task)

    return tasks
