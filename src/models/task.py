# src/models/task.py

"""
SQLAlchemy model for tasks.

This module defines the Task model representing tasks in the database.
"""

from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database_base import Base
from src.models.enums import PriorityEnum


class Task(Base):
    """
    SQLAlchemy model for tasks table.

    Attributes:
        id: Primary key
        title: Task title (required, max 200 chars)
        description: Task description (optional, max 2000 chars)
        completed: Completion status (default: False)
        priority: Priority level (LOW, MEDIUM, HIGH)
        created_at: Creation timestamp
        updated_at: Last update timestamp
        due_date: Optional due date
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    priority: Mapped[PriorityEnum] = mapped_column(
        Enum(PriorityEnum), default=PriorityEnum.MEDIUM, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        """
        String representation of Task.

        Returns:
            str: String representation with id, title, and completed status
        """
        return f"<Task(id={self.id}, title='{self.title}', completed={self.completed})>"
