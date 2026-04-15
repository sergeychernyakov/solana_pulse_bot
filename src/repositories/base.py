# src/repositories/base.py

"""
Base repository with common CRUD operations.

This module provides a generic base repository class with common
database operations that can be inherited by specific repositories.
"""

from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.helpers.logger import get_logger

T = TypeVar("T")

logger = get_logger(__name__)


class BaseRepository(Generic[T]):
    """
    Base repository with common CRUD operations.

    Type Parameters:
        T: SQLAlchemy model type

    Attributes:
        model_class: The SQLAlchemy model class for this repository
    """

    def __init__(self, model_class: type[T]) -> None:
        """
        Initialize repository with model class.

        Args:
            model_class: SQLAlchemy model class to use for operations
        """
        self.model_class = model_class

    async def create(self, session: AsyncSession, **kwargs: Any) -> T:
        """
        Create a new record.

        Args:
            session: Database session
            **kwargs: Field values for the new record

        Returns:
            Created model instance with ID and timestamps
        """
        instance = self.model_class(**kwargs)
        session.add(instance)
        await session.flush()
        await session.refresh(instance)
        logger.debug("Created %s with ID: %s", self.model_class.__name__, getattr(instance, "id", None))
        return instance

    async def get_by_id(self, session: AsyncSession, id: int) -> T | None:  # pylint: disable=redefined-builtin
        """
        Retrieve record by ID.

        Args:
            session: Database session
            id: Record ID

        Returns:
            Model instance or None if not found
        """
        stmt = select(self.model_class).where(getattr(self.model_class, "id") == id)
        result = await session.execute(stmt)
        instance = result.scalar_one_or_none()
        logger.debug(
            "Retrieved %s with ID %s: %s",
            self.model_class.__name__,
            id,
            "found" if instance else "not found",
        )
        return instance

    async def update(
        self, session: AsyncSession, id: int, **kwargs: Any  # pylint: disable=redefined-builtin
    ) -> T | None:
        """
        Update record by ID.

        Args:
            session: Database session
            id: Record ID
            **kwargs: Fields to update

        Returns:
            Updated model instance or None if not found
        """
        instance = await self.get_by_id(session, id)
        if instance is None:
            return None

        for key, value in kwargs.items():
            if hasattr(instance, key):
                setattr(instance, key, value)

        await session.flush()
        await session.refresh(instance)
        logger.debug("Updated %s with ID: %s", self.model_class.__name__, id)
        return instance

    async def delete(self, session: AsyncSession, id: int) -> bool:  # pylint: disable=redefined-builtin
        """
        Delete record by ID.

        Args:
            session: Database session
            id: Record ID

        Returns:
            True if deleted, False if not found
        """
        instance = await self.get_by_id(session, id)
        if instance is None:
            return False

        await session.delete(instance)
        await session.flush()
        logger.debug("Deleted %s with ID: %s", self.model_class.__name__, id)
        return True
