# src/services/task_service.py

"""
Task service for business logic.

This module provides the service layer for task operations,
including validation, business rules, and orchestration.
"""

from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.exceptions import TaskNotFoundError, TaskValidationError
from src.helpers.logger import get_logger
from src.models.enums import PriorityEnum
from src.models.task import Task
from src.repositories.task_repository import TaskRepository

logger = get_logger(__name__)


class TaskService:
    """
    Service layer for task business logic.

    Encapsulates business rules, validation, and orchestration
    of repository operations.

    Attributes:
        repository: TaskRepository instance for data access
    """

    def __init__(self) -> None:
        """Initialize task service with repository."""
        self.repository = TaskRepository()

    async def create_task(
        self,
        session: AsyncSession,
        title: str,
        description: Optional[str] = None,
        priority: PriorityEnum = PriorityEnum.MEDIUM,
        due_date: Optional[datetime] = None,
    ) -> Task:
        """
        Create a new task with validation.

        Args:
            session: Database session
            title: Task title (1-200 chars)
            description: Task description (max 2000 chars)
            priority: Priority level
            due_date: Optional due date

        Returns:
            Created task

        Raises:
            TaskValidationError: If validation fails
        """
        logger.info("Creating task with title: %s", title)

        # Validate title
        if not title or not title.strip():
            raise TaskValidationError("Title cannot be empty", field="title")

        if len(title) > 200:
            raise TaskValidationError("Title must be 200 characters or less", field="title")

        # Validate description
        if description is not None and len(description) > 2000:
            raise TaskValidationError("Description must be 2000 characters or less", field="description")

        # Validate due_date
        if due_date is not None and due_date < datetime.now(UTC):
            raise TaskValidationError("Due date must be in the future", field="due_date")

        # Create task
        task = await self.repository.create(
            session=session,
            title=title.strip(),
            description=description,
            priority=priority,
            due_date=due_date,
            completed=False,
        )

        logger.info("Task created with ID: %d", task.id)
        return task

    async def get_task_by_id(self, session: AsyncSession, task_id: int) -> Task:
        """
        Get task by ID.

        Args:
            session: Database session
            task_id: Task ID

        Returns:
            Task instance

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        task = await self.repository.get_by_id(session, task_id)
        if task is None:
            logger.warning("Task not found: %d", task_id)
            raise TaskNotFoundError(task_id)

        return task

    async def get_tasks(
        self,
        session: AsyncSession,
        completed: Optional[bool] = None,
        priority: Optional[PriorityEnum] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        """
        Get tasks with filters.

        Args:
            session: Database session
            completed: Filter by completion
            priority: Filter by priority
            search: Search in title/description
            limit: Max results (capped at 1000)
            offset: Pagination offset

        Returns:
            List of tasks
        """
        # Cap limit at 1000 for safety
        limit = min(limit, 1000)

        logger.debug(
            "Getting tasks (completed=%s, priority=%s, search=%s, limit=%d, offset=%d)",
            completed,
            priority,
            search,
            limit,
            offset,
        )

        tasks = await self.repository.get_all(
            session=session,
            completed=completed,
            priority=priority,
            search=search,
            limit=limit,
            offset=offset,
        )

        return tasks

    async def update_task(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        session: AsyncSession,
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[PriorityEnum] = None,
        completed: Optional[bool] = None,
        due_date: Optional[datetime] = None,
    ) -> Task:
        """
        Update task with validation.

        Args:
            session: Database session
            task_id: Task ID
            title: New title (if provided)
            description: New description (if provided)
            priority: New priority (if provided)
            completed: New completion status (if provided)
            due_date: New due date (if provided)

        Returns:
            Updated task

        Raises:
            TaskNotFoundError: If task doesn't exist
            TaskValidationError: If validation fails
        """
        logger.info("Updating task: %d", task_id)

        # Validate inputs
        if title is not None:
            if not title.strip():
                raise TaskValidationError("Title cannot be empty", field="title")
            if len(title) > 200:
                raise TaskValidationError("Title must be 200 characters or less", field="title")
            title = title.strip()

        if description is not None and len(description) > 2000:
            raise TaskValidationError("Description must be 2000 characters or less", field="description")

        if due_date is not None and due_date < datetime.now(UTC):
            raise TaskValidationError("Due date must be in the future", field="due_date")

        # Build update dict with only non-None values
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if priority is not None:
            updates["priority"] = priority
        if completed is not None:
            updates["completed"] = completed
        if due_date is not None:
            updates["due_date"] = due_date

        # Update updated_at timestamp
        updates["updated_at"] = datetime.now(UTC)

        # Update task
        task = await self.repository.update(session, task_id, **updates)
        if task is None:
            logger.warning("Task not found for update: %d", task_id)
            raise TaskNotFoundError(task_id)

        logger.info("Task updated: %d", task_id)
        return task

    async def delete_task(self, session: AsyncSession, task_id: int) -> None:
        """
        Delete task.

        Args:
            session: Database session
            task_id: Task ID

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        logger.info("Deleting task: %d", task_id)

        deleted = await self.repository.delete(session, task_id)
        if not deleted:
            logger.warning("Task not found for deletion: %d", task_id)
            raise TaskNotFoundError(task_id)

        logger.info("Task deleted: %d", task_id)

    async def mark_task_complete(self, session: AsyncSession, task_id: int) -> Task:
        """
        Mark task as completed.

        Args:
            session: Database session
            task_id: Task ID

        Returns:
            Updated task

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        logger.info("Marking task as complete: %d", task_id)

        task = await self.repository.mark_complete(session, task_id)
        if task is None:
            logger.warning("Task not found: %d", task_id)
            raise TaskNotFoundError(task_id)

        logger.info("Task marked complete: %d", task_id)
        return task
