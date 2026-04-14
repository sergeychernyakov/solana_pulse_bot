# src/repositories/task_repository.py

"""
Task repository for data access operations.

This module provides data access methods specific to Task model,
including filtering and search capabilities.
"""

from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.helpers.logger import get_logger
from src.models.enums import PriorityEnum
from src.models.task import Task
from src.repositories.base import BaseRepository

logger = get_logger(__name__)


class TaskRepository(BaseRepository[Task]):
    """
    Repository for task data access.

    Provides CRUD operations and query methods for tasks including
    filtering, searching, and pagination.
    """

    def __init__(self) -> None:
        """Initialize task repository with Task model."""
        super().__init__(Task)

    async def get_all(
        self,
        session: AsyncSession,
        completed: Optional[bool] = None,
        priority: Optional[PriorityEnum] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        """
        Retrieve tasks with optional filters and pagination.

        Args:
            session: Database session
            completed: Filter by completion status (None = no filter)
            priority: Filter by priority level (None = no filter)
            search: Search text in title/description (None = no search)
            limit: Maximum results (max 1000)
            offset: Skip first N results

        Returns:
            List of tasks matching criteria, ordered by created_at desc
        """
        stmt = select(Task)

        # Apply filters
        if completed is not None:
            stmt = stmt.where(Task.completed == completed)

        if priority is not None:
            stmt = stmt.where(Task.priority == priority)

        if search is not None:
            search_pattern = f"%{search}%"
            stmt = stmt.where(or_(Task.title.ilike(search_pattern), Task.description.ilike(search_pattern)))

        # Order and pagination
        stmt = stmt.order_by(Task.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)

        result = await session.execute(stmt)
        tasks = list(result.scalars().all())

        logger.debug(
            "Retrieved %d tasks (completed=%s, priority=%s, search=%s, limit=%d, offset=%d)",
            len(tasks),
            completed,
            priority,
            search,
            limit,
            offset,
        )

        return tasks

    async def mark_complete(self, session: AsyncSession, id: int) -> Task | None:  # pylint: disable=redefined-builtin
        """
        Mark task as completed.

        Args:
            session: Database session
            id: Task ID

        Returns:
            Updated task or None if not found
        """
        task = await self.get_by_id(session, id)
        if task is None:
            return None

        task.completed = True
        await session.flush()
        await session.refresh(task)

        logger.debug("Marked task %d as complete", id)
        return task

    async def get_count(
        self,
        session: AsyncSession,
        completed: Optional[bool] = None,
        priority: Optional[PriorityEnum] = None,
        search: Optional[str] = None,
    ) -> int:
        """
        Count tasks matching filters.

        Args:
            session: Database session
            completed: Filter by completion status
            priority: Filter by priority level
            search: Search text in title/description

        Returns:
            Count of matching tasks
        """
        stmt = select(func.count(Task.id))

        # Apply same filters as get_all
        if completed is not None:
            stmt = stmt.where(Task.completed == completed)

        if priority is not None:
            stmt = stmt.where(Task.priority == priority)

        if search is not None:
            search_pattern = f"%{search}%"
            stmt = stmt.where(or_(Task.title.ilike(search_pattern), Task.description.ilike(search_pattern)))

        result = await session.execute(stmt)
        count = result.scalar_one()

        logger.debug("Counted %d tasks (completed=%s, priority=%s, search=%s)", count, completed, priority, search)

        return count
