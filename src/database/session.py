# src/database/session.py

"""
Database session management and engine configuration.

This module provides async database session management for FastAPI
dependency injection and database initialization utilities.
"""

from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config.settings import config
from src.helpers.logger import get_logger
from src.models.database_base import Base

logger = get_logger(__name__)

_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_async_engine() -> AsyncEngine:
    """
    Get or create async database engine (singleton pattern).

    Returns:
        AsyncEngine: SQLAlchemy async engine instance

    Note:
        Engine is created once and reused for connection pooling.
    """
    global _engine  # pylint: disable=global-statement

    if _engine is None:
        logger.info("Creating async database engine: %s", config.DATABASE_URL)
        _engine = create_async_engine(
            config.DATABASE_URL,
            echo=config.DATABASE_ECHO,
            pool_pre_ping=True,
            future=True,
        )
        logger.info("Database engine created successfully")

    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    Get or create async session factory.

    Returns:
        async_sessionmaker: Session factory for creating database sessions
    """
    global _async_session_factory  # pylint: disable=global-statement

    if _async_session_factory is None:
        engine = get_async_engine()
        _async_session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    return _async_session_factory


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide async database session for dependency injection.

    Yields:
        AsyncSession: Database session with automatic commit/rollback

    Example:
        @app.get("/tasks")
        async def get_tasks(session: AsyncSession = Depends(get_async_session)):
            # Use session here
            pass

    Note:
        Session automatically commits on successful completion
        and rolls back on exception. Always closed after use.
    """
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            logger.debug("Database session created")
            yield session
            await session.commit()
            logger.debug("Database session committed")
        except Exception as exc:
            await session.rollback()
            logger.error("Database session rolled back due to error: %s", exc)
            raise
        finally:
            await session.close()
            logger.debug("Database session closed")


async def init_db() -> None:
    """
    Initialize database (create tables).

    Used for testing and initial setup.
    Production should use Alembic migrations instead.

    Note:
        Creates all tables defined in Base.metadata.
    """
    engine = get_async_engine()
    logger.info("Initializing database tables")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database tables created successfully")


async def close_db() -> None:
    """
    Close database engine and cleanup resources.

    Used during application shutdown.
    """
    global _engine  # pylint: disable=global-statement

    if _engine is not None:
        logger.info("Closing database engine")
        await _engine.dispose()
        _engine = None
        logger.info("Database engine closed")
