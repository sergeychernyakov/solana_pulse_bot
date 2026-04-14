# src/api/v1/routes/health.py

"""
Health check endpoint.

This module provides a simple health check endpoint to verify
API and database connectivity.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.api.v1.schemas.common import HealthResponse
from src.helpers.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health", response_model=HealthResponse)
async def health_check(session: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    """
    Health check endpoint.

    Tests API responsiveness and database connectivity.

    Args:
        session: Database session (injected)

    Returns:
        Health status including database connectivity

    Example:
        GET /api/v1/health
        Response: {"status": "ok", "timestamp": "...", "database": "connected"}
    """
    logger.debug("Health check requested")

    # Test database connectivity
    try:
        await session.execute(text("SELECT 1"))
        database_status = "connected"
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Database health check failed: %s", exc)
        database_status = "disconnected"

    response = HealthResponse(status="ok", timestamp=datetime.now(UTC), database=database_status)

    logger.debug("Health check completed: database=%s", database_status)
    return response
