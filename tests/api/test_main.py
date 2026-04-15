# tests/api/test_main.py

"""Tests for FastAPI app lifecycle and exception handlers."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.api.exceptions import DatabaseError, TaskValidationError
from src.api.main import app, lifespan


def make_request() -> Request:
    """Build a minimal ASGI request for direct handler calls."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.mark.asyncio
async def test_lifespan_closes_database() -> None:
    """Test lifespan startup/shutdown path awaits database cleanup."""
    with patch("src.api.main.close_db", new_callable=AsyncMock) as close_db:
        async with lifespan(FastAPI()):
            close_db.assert_not_awaited()

        close_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_validation_exception_handler_response() -> None:
    """Test custom task validation exception handler response shape."""
    handler = app.exception_handlers[TaskValidationError]
    response = await handler(make_request(), TaskValidationError("Bad task", field="title"))

    assert response.status_code == 422
    assert json.loads(response.body) == {"detail": "Bad task", "field": "title"}


@pytest.mark.asyncio
async def test_database_exception_handler_response() -> None:
    """Test custom database exception handler hides internal details."""
    handler = app.exception_handlers[DatabaseError]
    response = await handler(make_request(), DatabaseError("connection failed"))

    assert response.status_code == 500
    assert json.loads(response.body) == {"detail": "Internal server error"}
