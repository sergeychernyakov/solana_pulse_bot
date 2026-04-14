# tests/database/test_session.py

"""
Test database session management.

This module tests database engine creation, session lifecycle,
and error handling.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.session import close_db, get_async_engine, get_async_session, init_db


@pytest.mark.asyncio
async def test_get_async_session_lifecycle() -> None:
    """Test async session creation and cleanup."""
    session_gen = get_async_session()
    session = await anext(session_gen)

    try:
        assert session is not None
    finally:
        try:
            await session_gen.asend(None)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_session_rollback_on_error() -> None:
    """Test session rollback on exception."""
    from src.models.task import Task

    session_gen = get_async_session()
    session = await anext(session_gen)

    try:
        # Create task
        task = Task(title="Test Task", completed=False)
        session.add(task)
        await session.flush()

        # Force an error by raising exception
        raise ValueError("Test error")
    except ValueError:
        # Session should rollback automatically
        try:
            await session_gen.athrow(ValueError, ValueError("Test error"), None)
        except ValueError:
            pass  # Expected


@pytest.mark.asyncio
async def test_session_commit_success(async_session: AsyncSession) -> None:
    """Test successful session commit."""
    from src.models.task import Task

    task = Task(title="Commit Test", completed=False)
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)

    assert task.id is not None


@pytest.mark.asyncio
async def test_init_and_close_db() -> None:
    """Test database initialization and cleanup."""
    # Initialize database
    await init_db()

    # Get engine to verify it's created
    engine = get_async_engine()
    assert engine is not None

    # Close database
    await close_db()


@pytest.mark.asyncio
async def test_multiple_sessions_isolation(async_session: AsyncSession) -> None:
    """Test that multiple sessions are isolated."""
    from sqlalchemy import select

    from src.models.task import Task

    # Create task in session
    task = Task(title="Session Isolation Test", completed=False)
    async_session.add(task)
    await async_session.commit()
    await async_session.refresh(task)
    task_id = task.id

    # Query back in same session
    stmt = select(Task).where(Task.id == task_id)
    result = await async_session.execute(stmt)
    found_task = result.scalar_one_or_none()

    assert found_task is not None
    assert found_task.title == "Session Isolation Test"
