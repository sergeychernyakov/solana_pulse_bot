# src/api/exceptions.py

"""
Custom exceptions for API.

This module defines domain-specific exceptions used throughout
the API layer and service layer.
"""


class TaskAPIException(Exception):
    """
    Base exception for task API.

    All custom exceptions for the task API should inherit from this.
    """

    pass


class TaskNotFoundError(TaskAPIException):
    """
    Raised when task doesn't exist.

    Attributes:
        task_id: ID of task that wasn't found
        message: Error message
    """

    def __init__(self, task_id: int) -> None:
        """
        Initialize exception.

        Args:
            task_id: ID of task that wasn't found
        """
        self.task_id = task_id
        self.message = f"Task with ID {task_id} not found"
        super().__init__(self.message)


class TaskValidationError(TaskAPIException):
    """
    Raised when task validation fails.

    Attributes:
        message: Validation error message
        field: Field that failed validation (optional)
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        """
        Initialize exception.

        Args:
            message: Validation error message
            field: Field that failed validation
        """
        self.message = message
        self.field = field
        super().__init__(message)


class DatabaseError(TaskAPIException):
    """
    Raised when database operation fails.

    Used for unexpected database errors that should result in 500 responses.
    """

    pass
