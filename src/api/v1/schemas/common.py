# src/api/v1/schemas/common.py

"""
Common schemas for pagination, errors, etc.

This module defines shared schemas used across multiple endpoints.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """
    Schema for error responses.

    Used for consistent error formatting across the API.
    """

    detail: str = Field(..., description="Error message")
    field: Optional[str] = Field(None, description="Field that caused the error (if applicable)")


class HealthResponse(BaseModel):
    """
    Schema for health check response.

    Indicates API and database health status.
    """

    status: str = Field(..., description="Health status (ok/error)")
    timestamp: datetime = Field(..., description="Current server time")
    database: str = Field(..., description="Database connection status")
