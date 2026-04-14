# tests/repositories/test_base_repository.py

"""
Test base repository CRUD operations.

This module tests the generic BaseRepository class to ensure
all common CRUD operations work correctly.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.task import Task
from src.repositories.base import BaseRepository


@pytest.mark.asyncio
async def test_create_success(async_session: AsyncSession) -> None:
    """Test successful record creation."""
    repository = BaseRepository(Task)

    task = await repository.create(async_session, title="Test Task", description="Test Description", completed=False)

    assert task.id is not None
    assert task.title == "Test Task"
    assert task.description == "Test Description"
    assert task.completed is False


@pytest.mark.asyncio
async def test_get_by_id_found(async_session: AsyncSession, sample_task: Task) -> None:
    """Test retrieving existing record by ID."""
    repository = BaseRepository(Task)

    task = await repository.get_by_id(async_session, sample_task.id)

    assert task is not None
    assert task.id == sample_task.id
    assert task.title == sample_task.title


@pytest.mark.asyncio
async def test_get_by_id_not_found(async_session: AsyncSession) -> None:
    """Test retrieving non-existent record returns None."""
    repository = BaseRepository(Task)

    task = await repository.get_by_id(async_session, 9999)

    assert task is None


@pytest.mark.asyncio
async def test_update_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test successful record update."""
    repository = BaseRepository(Task)

    updated_task = await repository.update(async_session, sample_task.id, title="Updated Title", completed=True)

    assert updated_task is not None
    assert updated_task.id == sample_task.id
    assert updated_task.title == "Updated Title"
    assert updated_task.completed is True


@pytest.mark.asyncio
async def test_update_not_found(async_session: AsyncSession) -> None:
    """Test updating non-existent record returns None."""
    repository = BaseRepository(Task)

    result = await repository.update(async_session, 9999, title="Updated")

    assert result is None


@pytest.mark.asyncio
async def test_delete_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test successful record deletion."""
    repository = BaseRepository(Task)

    result = await repository.delete(async_session, sample_task.id)

    assert result is True

    # Verify task is deleted
    task = await repository.get_by_id(async_session, sample_task.id)
    assert task is None


@pytest.mark.asyncio
async def test_delete_not_found(async_session: AsyncSession) -> None:
    """Test deleting non-existent record returns False."""
    repository = BaseRepository(Task)

    result = await repository.delete(async_session, 9999)

    assert result is False
