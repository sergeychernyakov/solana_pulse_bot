# tests/services/test_task_service.py

"""
Test task service business logic.

This module tests TaskService validation and error handling.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.exceptions import TaskNotFoundError, TaskValidationError
from src.models.enums import PriorityEnum
from src.models.task import Task
from src.services.task_service import TaskService


@pytest.mark.asyncio
async def test_create_task_title_too_long(async_session: AsyncSession) -> None:
    """Test creating task with title exceeding 200 characters."""
    service = TaskService()
    long_title = "a" * 201

    with pytest.raises(TaskValidationError) as exc_info:
        await service.create_task(async_session, title=long_title)

    assert "200 characters" in exc_info.value.message
    assert exc_info.value.field == "title"


@pytest.mark.asyncio
async def test_create_task_empty_title(async_session: AsyncSession) -> None:
    """Test creating task with empty title."""
    service = TaskService()

    with pytest.raises(TaskValidationError) as exc_info:
        await service.create_task(async_session, title="")

    assert "empty" in exc_info.value.message.lower()
    assert exc_info.value.field == "title"


@pytest.mark.asyncio
async def test_create_task_whitespace_title(async_session: AsyncSession) -> None:
    """Test creating task with whitespace-only title."""
    service = TaskService()

    with pytest.raises(TaskValidationError) as exc_info:
        await service.create_task(async_session, title="   ")

    assert "empty" in exc_info.value.message.lower()
    assert exc_info.value.field == "title"


@pytest.mark.asyncio
async def test_create_task_description_too_long(async_session: AsyncSession) -> None:
    """Test creating task with description exceeding 2000 characters."""
    service = TaskService()
    long_description = "a" * 2001

    with pytest.raises(TaskValidationError) as exc_info:
        await service.create_task(async_session, title="Valid Title", description=long_description)

    assert "2000 characters" in exc_info.value.message
    assert exc_info.value.field == "description"


@pytest.mark.asyncio
async def test_create_task_due_date_in_past(async_session: AsyncSession) -> None:
    """Test creating task with due date in the past."""
    service = TaskService()
    past_date = datetime.now(UTC) - timedelta(days=1)

    with pytest.raises(TaskValidationError) as exc_info:
        await service.create_task(async_session, title="Valid Title", due_date=past_date)

    assert "future" in exc_info.value.message.lower()
    assert exc_info.value.field == "due_date"


@pytest.mark.asyncio
async def test_create_task_with_due_date(async_session: AsyncSession) -> None:
    """Test creating task with valid future due date."""
    service = TaskService()
    future_date = datetime.now(UTC) + timedelta(days=1)

    task = await service.create_task(async_session, title="Task with deadline", due_date=future_date)

    assert task.id is not None
    assert task.title == "Task with deadline"
    assert task.due_date is not None


@pytest.mark.asyncio
async def test_get_task_not_found(async_session: AsyncSession) -> None:
    """Test getting non-existent task raises TaskNotFoundError."""
    service = TaskService()

    with pytest.raises(TaskNotFoundError) as exc_info:
        await service.get_task_by_id(async_session, 9999)

    assert exc_info.value.task_id == 9999


@pytest.mark.asyncio
async def test_update_task_empty_title(async_session: AsyncSession, sample_task: Task) -> None:
    """Test updating task with empty title."""
    service = TaskService()

    with pytest.raises(TaskValidationError) as exc_info:
        await service.update_task(async_session, sample_task.id, title="")

    assert "empty" in exc_info.value.message.lower()
    assert exc_info.value.field == "title"


@pytest.mark.asyncio
async def test_update_task_title_too_long(async_session: AsyncSession, sample_task: Task) -> None:
    """Test updating task with title exceeding 200 characters."""
    service = TaskService()
    long_title = "a" * 201

    with pytest.raises(TaskValidationError) as exc_info:
        await service.update_task(async_session, sample_task.id, title=long_title)

    assert "200 characters" in exc_info.value.message
    assert exc_info.value.field == "title"


@pytest.mark.asyncio
async def test_update_task_description_too_long(async_session: AsyncSession, sample_task: Task) -> None:
    """Test updating task with description exceeding 2000 characters."""
    service = TaskService()
    long_description = "a" * 2001

    with pytest.raises(TaskValidationError) as exc_info:
        await service.update_task(async_session, sample_task.id, description=long_description)

    assert "2000 characters" in exc_info.value.message
    assert exc_info.value.field == "description"


@pytest.mark.asyncio
async def test_update_task_due_date_in_past(async_session: AsyncSession, sample_task: Task) -> None:
    """Test updating task with due date in the past."""
    service = TaskService()
    past_date = datetime.now(UTC) - timedelta(days=1)

    with pytest.raises(TaskValidationError) as exc_info:
        await service.update_task(async_session, sample_task.id, due_date=past_date)

    assert "future" in exc_info.value.message.lower()
    assert exc_info.value.field == "due_date"


@pytest.mark.asyncio
async def test_update_task_not_found(async_session: AsyncSession) -> None:
    """Test updating non-existent task raises TaskNotFoundError."""
    service = TaskService()

    with pytest.raises(TaskNotFoundError) as exc_info:
        await service.update_task(async_session, 9999, title="Updated")

    assert exc_info.value.task_id == 9999


@pytest.mark.asyncio
async def test_delete_task_not_found(async_session: AsyncSession) -> None:
    """Test deleting non-existent task raises TaskNotFoundError."""
    service = TaskService()

    with pytest.raises(TaskNotFoundError) as exc_info:
        await service.delete_task(async_session, 9999)

    assert exc_info.value.task_id == 9999


@pytest.mark.asyncio
async def test_mark_complete_not_found(async_session: AsyncSession) -> None:
    """Test marking non-existent task as complete raises TaskNotFoundError."""
    service = TaskService()

    with pytest.raises(TaskNotFoundError) as exc_info:
        await service.mark_task_complete(async_session, 9999)

    assert exc_info.value.task_id == 9999


@pytest.mark.asyncio
async def test_list_tasks_pagination(async_session: AsyncSession, sample_tasks: list[Task]) -> None:
    """Test listing tasks with pagination."""
    service = TaskService()

    # Get first page
    page1 = await service.get_tasks(async_session, limit=2, offset=0)
    assert len(page1) == 2

    # Get second page
    page2 = await service.get_tasks(async_session, limit=2, offset=2)
    assert len(page2) == 2

    # Verify different tasks
    page1_ids = {task.id for task in page1}
    page2_ids = {task.id for task in page2}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_create_task_with_all_fields(async_session: AsyncSession) -> None:
    """Test creating task with all optional fields."""
    service = TaskService()
    future_date = datetime.now(UTC) + timedelta(days=7)

    task = await service.create_task(
        async_session,
        title="Complete Task",
        description="With description",
        priority=PriorityEnum.HIGH,
        due_date=future_date,
    )

    assert task.id is not None
    assert task.title == "Complete Task"
    assert task.description == "With description"
    assert task.priority == PriorityEnum.HIGH
    assert task.due_date is not None
    assert task.completed is False


@pytest.mark.asyncio
async def test_update_task_individual_fields(async_session: AsyncSession, sample_task: Task) -> None:
    """Test updating individual task fields."""
    service = TaskService()

    # Update description only
    updated = await service.update_task(async_session, sample_task.id, description="New description")
    assert updated.description == "New description"
    assert updated.title == sample_task.title

    # Update priority only
    updated = await service.update_task(async_session, sample_task.id, priority=PriorityEnum.HIGH)
    assert updated.priority == PriorityEnum.HIGH

    # Update completed only
    updated = await service.update_task(async_session, sample_task.id, completed=True)
    assert updated.completed is True

    # Update due_date only
    future_date = datetime.now(UTC) + timedelta(days=5)
    updated = await service.update_task(async_session, sample_task.id, due_date=future_date)
    assert updated.due_date is not None


@pytest.mark.asyncio
async def test_get_task_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test successfully getting existing task."""
    service = TaskService()

    task = await service.get_task_by_id(async_session, sample_task.id)

    assert task.id == sample_task.id
    assert task.title == sample_task.title


@pytest.mark.asyncio
async def test_delete_task_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test successfully deleting existing task."""
    service = TaskService()

    await service.delete_task(async_session, sample_task.id)

    # Verify task was deleted
    with pytest.raises(TaskNotFoundError):
        await service.get_task_by_id(async_session, sample_task.id)


@pytest.mark.asyncio
async def test_mark_task_complete_success(async_session: AsyncSession, sample_task: Task) -> None:
    """Test successfully marking task as complete."""
    service = TaskService()

    # Ensure task starts as incomplete
    assert sample_task.completed is False

    result = await service.mark_task_complete(async_session, sample_task.id)

    assert result.completed is True
    assert result.id == sample_task.id
