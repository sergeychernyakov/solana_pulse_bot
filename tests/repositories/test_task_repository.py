# tests/repositories/test_task_repository.py

"""
Test task repository operations.

This module tests TaskRepository-specific functionality including
filtering, searching, and pagination.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.enums import PriorityEnum
from src.models.task import Task
from src.repositories.task_repository import TaskRepository


@pytest.mark.asyncio
async def test_get_all_no_filters(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all with no filters returns all tasks."""
    repository = TaskRepository()

    tasks = await repository.get_all(async_session)

    assert len(tasks) == 5
    # Verify ordering by created_at desc (newest first)
    assert tasks[0].title == "Buy groceries"  # Last created


@pytest.mark.asyncio
async def test_get_all_with_completed_filter(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all filtered by completed status."""
    repository = TaskRepository()

    completed_tasks = await repository.get_all(async_session, completed=True)
    incomplete_tasks = await repository.get_all(async_session, completed=False)

    assert len(completed_tasks) == 2
    assert all(task.completed for task in completed_tasks)
    assert len(incomplete_tasks) == 3
    assert all(not task.completed for task in incomplete_tasks)


@pytest.mark.asyncio
async def test_get_all_with_priority_filter(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all filtered by priority."""
    repository = TaskRepository()

    high_priority = await repository.get_all(async_session, priority=PriorityEnum.HIGH)
    medium_priority = await repository.get_all(async_session, priority=PriorityEnum.MEDIUM)
    low_priority = await repository.get_all(async_session, priority=PriorityEnum.LOW)

    assert len(high_priority) == 2
    assert all(task.priority == PriorityEnum.HIGH for task in high_priority)
    assert len(medium_priority) == 2
    assert len(low_priority) == 1


@pytest.mark.asyncio
async def test_get_all_with_search(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all with search term."""
    repository = TaskRepository()

    # Search in title
    results = await repository.get_all(async_session, search="groceries")

    assert len(results) == 1
    assert "groceries" in results[0].title.lower()


@pytest.mark.asyncio
async def test_get_all_pagination(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all with limit and offset."""
    repository = TaskRepository()

    # Get first 2 tasks
    page1 = await repository.get_all(async_session, limit=2, offset=0)
    assert len(page1) == 2

    # Get next 2 tasks
    page2 = await repository.get_all(async_session, limit=2, offset=2)
    assert len(page2) == 2

    # Verify different tasks
    page1_ids = {task.id for task in page1}
    page2_ids = {task.id for task in page2}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_mark_complete_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test marking task as complete."""
    repository = TaskRepository()

    # Ensure task starts as incomplete
    assert sample_task.completed is False

    result = await repository.mark_complete(async_session, sample_task.id)

    assert result is not None
    assert result.completed is True
    assert result.id == sample_task.id


@pytest.mark.asyncio
async def test_mark_complete_not_found(async_session: AsyncSession) -> None:
    """Test marking non-existent task as complete returns None."""
    repository = TaskRepository()

    result = await repository.mark_complete(async_session, 9999)

    assert result is None


@pytest.mark.asyncio
async def test_get_count_no_filters(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_count with no filters."""
    repository = TaskRepository()

    count = await repository.get_count(async_session)

    assert count == 5


@pytest.mark.asyncio
async def test_get_count_with_completed_filter(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_count filtered by completed status."""
    repository = TaskRepository()

    completed_count = await repository.get_count(async_session, completed=True)
    incomplete_count = await repository.get_count(async_session, completed=False)

    assert completed_count == 2
    assert incomplete_count == 3


@pytest.mark.asyncio
async def test_get_count_with_priority_filter(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_count filtered by priority."""
    repository = TaskRepository()

    high_count = await repository.get_count(async_session, priority=PriorityEnum.HIGH)
    medium_count = await repository.get_count(async_session, priority=PriorityEnum.MEDIUM)

    assert high_count == 2
    assert medium_count == 2


@pytest.mark.asyncio
async def test_get_count_with_search(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_count with search term."""
    repository = TaskRepository()

    count = await repository.get_count(async_session, search="groceries")

    assert count == 1


@pytest.mark.asyncio
async def test_get_all_combined_filters(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test get_all with multiple filters combined."""
    repository = TaskRepository()

    # Filter by completed and priority
    results = await repository.get_all(async_session, completed=True, priority=PriorityEnum.HIGH)

    assert len(results) == 1
    assert all(task.completed and task.priority == PriorityEnum.HIGH for task in results)
