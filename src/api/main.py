# src/api/main.py

"""
FastAPI application initialization.

This module creates and configures the FastAPI application,
including middleware, exception handlers, and route registration.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.exceptions import DatabaseError, TaskNotFoundError, TaskValidationError
from src.api.v1.routes import health, tasks
from src.config.settings import config
from src.database.session import close_db
from src.helpers.logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan handler.

    Manages startup and shutdown operations for the FastAPI application.

    Args:
        app: FastAPI application instance

    Yields:
        None
    """
    # Startup
    logger.info("Starting Task Management API")
    logger.info("Environment: %s", config.APP_ENV)
    logger.info("Database: %s", config.DATABASE_URL)
    logger.info("API prefix: %s", config.API_V1_PREFIX)

    yield

    # Shutdown
    logger.info("Shutting down Task Management API")
    await close_db()


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.

    Returns:
        FastAPI: Configured application instance
    """
    application = FastAPI(
        title="Task Management API",
        version="1.0.0",
        description="REST API for managing tasks with database persistence",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Add CORS middleware
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers with API prefix
    application.include_router(tasks.router, prefix=config.API_V1_PREFIX, tags=["tasks"])
    application.include_router(health.router, prefix=config.API_V1_PREFIX, tags=["health"])

    # Register exception handlers
    @application.exception_handler(TaskNotFoundError)
    async def task_not_found_handler(
        request: Request, exc: TaskNotFoundError  # pylint: disable=unused-argument
    ) -> JSONResponse:
        """
        Handle task not found errors.

        Args:
            request: HTTP request
            exc: TaskNotFoundError exception

        Returns:
            JSON response with 404 status
        """
        logger.warning("Task not found: %d", exc.task_id)
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": exc.message})

    @application.exception_handler(TaskValidationError)
    async def task_validation_handler(
        request: Request, exc: TaskValidationError  # pylint: disable=unused-argument
    ) -> JSONResponse:
        """
        Handle task validation errors.

        Args:
            request: HTTP request
            exc: TaskValidationError exception

        Returns:
            JSON response with 422 status
        """
        logger.warning("Validation error: %s (field: %s)", exc.message, exc.field)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.message, "field": exc.field},
        )

    @application.exception_handler(DatabaseError)
    async def database_error_handler(
        request: Request, exc: DatabaseError  # pylint: disable=unused-argument
    ) -> JSONResponse:
        """
        Handle database errors.

        Args:
            request: HTTP request
            exc: DatabaseError exception

        Returns:
            JSON response with 500 status
        """
        logger.error("Database error: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    return application


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.api.main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
