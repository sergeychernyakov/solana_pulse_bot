# src/api/v1/schemas/task.py

"""
Pydantic schemas for task endpoints.

This module defines request and response schemas for task operations,
providing automatic validation and serialization.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import PriorityEnum


class TaskBase(BaseModel):
    """
    Base task schema with common fields.

    Contains fields that are shared between create and response schemas.
    """

    title: str = Field(..., min_length=1, max_length=200, description="Task title")
    description: Optional[str] = Field(None, max_length=2000, description="Task description")
    priority: PriorityEnum = Field(default=PriorityEnum.MEDIUM, description="Task priority level")
    due_date: Optional[datetime] = Field(None, description="Optional due date")


class TaskCreate(TaskBase):
    """
    Schema for creating a task.

    Inherits all fields from TaskBase. Used for POST requests.
    """

    pass


class TaskUpdate(BaseModel):
    """
    Schema for updating a task.

    All fields optional - only provided fields are updated.
    Used for PUT requests with partial updates.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=200, description="Task title")
    description: Optional[str] = Field(None, max_length=2000, description="Task description")
    priority: Optional[PriorityEnum] = Field(None, description="Task priority level")
    completed: Optional[bool] = Field(None, description="Completion status")
    due_date: Optional[datetime] = Field(None, description="Optional due date")


class TaskResponse(TaskBase):
    """
    Schema for task response.

    Includes all base fields plus read-only fields (id, timestamps, completed).
    Used for all response bodies containing task data.
    """

    id: int = Field(..., description="Task ID")
    completed: bool = Field(..., description="Completion status")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)
