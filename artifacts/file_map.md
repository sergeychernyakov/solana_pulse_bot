# File Map - TODO List REST API

**Project**: /Users/sergeychernyakov/www/blank_python_project
**Date**: 2025-10-23
**Architect**: @agent-code-architect

---

## Overview

This document provides a complete file-by-file implementation guide. Each file includes:
- Full path
- Purpose
- Key classes/functions with signatures
- Dependencies
- Implementation notes

Files are organized by implementation priority (dependencies first).

---

## 1. CONFIGURATION & DEPENDENCIES

### 1.1 requirements.txt

**Path**: `/Users/sergeychernyakov/www/blank_python_project/requirements.txt`

**Purpose**: Add new dependencies for REST API functionality

**Action**: ADD to existing requirements

**New Dependencies**:
```
# Web Framework
fastapi==0.104.1
uvicorn[standard]==0.24.0

# Database
sqlalchemy[asyncio]==2.0.23
aiosqlite==0.19.0
asyncpg==0.29.0
alembic==1.12.1

# HTTP Client for Testing
httpx==0.25.1
pytest-asyncio==0.21.1
```

**Implementation Notes**:
- Keep existing dependencies
- Add these at the end
- Run `pip install -r requirements.txt` after adding

---

### 1.2 src/config/settings.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/config/settings.py`

**Purpose**: Extend configuration with database and API settings

**Action**: UPDATE existing file

**Additions**:
```python
from dataclasses import dataclass, field

@dataclass
class Config:
    """Base configuration class."""
    DEBUG: bool = False
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # Database Configuration
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "sqlite+aiosqlite:///./tmp/tasks.db"
    )
    DATABASE_ECHO: bool = False  # Log SQL queries

    # API Configuration
    API_V1_PREFIX: str = os.getenv("API_V1_PREFIX", "/api/v1")
    CORS_ORIGINS: list[str] = field(default_factory=lambda: ["*"])

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

@dataclass
class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG: bool = True
    DATABASE_ECHO: bool = True  # Show SQL in development
    RELOAD: bool = True

@dataclass
class ProductionConfig(Config):
    """Production configuration."""
    DEBUG: bool = False
    DATABASE_ECHO: bool = False
    RELOAD: bool = False
    CORS_ORIGINS: list[str] = field(
        default_factory=lambda: os.getenv("CORS_ORIGINS", "").split(",")
    )
```

**Dependencies**: os, dataclasses, dotenv (existing)

**Implementation Notes**:
- Preserve existing DEBUG and APP_ENV fields
- Add database, API, and server configuration
- Use environment variables with sensible defaults
- Development config shows SQL queries for debugging

---

### 1.3 .env

**Path**: `/Users/sergeychernyakov/www/blank_python_project/.env`

**Purpose**: Add environment variables for database and API

**Action**: UPDATE existing file

**Additions**:
```env
# Existing
APP_ENV=development

# Database
DATABASE_URL=sqlite+aiosqlite:///./tmp/tasks.db
# For PostgreSQL: postgresql+asyncpg://user:password@localhost:5432/tasks_db

# API
API_V1_PREFIX=/api/v1
CORS_ORIGINS=http://localhost:3000,http://localhost:8000

# Server
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=DEBUG
```

**Implementation Notes**:
- Keep existing APP_ENV setting
- Add new settings below
- Not committed to git (already in .gitignore)

---

## 2. MODELS & ENUMS

### 2.1 src/models/enums.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/models/enums.py`

**Purpose**: Add Priority enum for tasks

**Action**: UPDATE existing file

**Additions**:
```python
class PriorityEnum(str, Enum):
    """Task priority levels."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
```

**Dependencies**: enum (existing)

**Implementation Notes**:
- Add after existing YesNoEnum
- Inherits from str for JSON serialization
- Three priority levels: LOW, MEDIUM, HIGH

---

### 2.2 src/models/database_base.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/models/database_base.py`

**Purpose**: Create SQLAlchemy declarative base

**Action**: CREATE new file

**Contents**:
```python
# src/models/database_base.py

"""SQLAlchemy declarative base for database models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy models.

    All database models should inherit from this class.
    """
    pass
```

**Dependencies**: sqlalchemy

**Implementation Notes**:
- Separate from existing Pydantic Base class
- All SQLAlchemy models inherit from this
- Used by Alembic for autogeneration
- Name it database_base.py to avoid confusion with existing base.py

---

### 2.3 src/models/task.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/models/task.py`

**Purpose**: SQLAlchemy model for tasks table

**Action**: CREATE new file

**Contents**:
```python
# src/models/task.py

"""SQLAlchemy model for tasks."""

from datetime import datetime
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
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=False)
    description: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    priority: Mapped[PriorityEnum] = mapped_column(
        Enum(PriorityEnum),
        default=PriorityEnum.MEDIUM,
        nullable=False,
        index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    due_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    def __repr__(self) -> str:
        """String representation of Task."""
        return f"<Task(id={self.id}, title='{self.title}', completed={self.completed})>"
```

**Dependencies**: sqlalchemy, datetime, src.models.database_base, src.models.enums

**Implementation Notes**:
- Use SQLAlchemy 2.0 Mapped syntax
- Indexes on completed, priority, created_at for query performance
- Auto-update updated_at on changes
- Use UTC for all timestamps
- Enum stored as string in database

---

## 3. DATABASE LAYER

### 3.1 src/database/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/database/__init__.py`

**Purpose**: Make database a package

**Action**: CREATE new file

**Contents**:
```python
# src/database/__init__.py

"""Database package for session management and migrations."""
```

**Dependencies**: None

---

### 3.2 src/database/session.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/database/session.py`

**Purpose**: Database session management and engine configuration

**Action**: CREATE new file

**Key Functions**:
```python
async def get_async_engine() -> AsyncEngine:
    """
    Get or create async database engine.

    Returns:
        AsyncEngine: SQLAlchemy async engine
    """
    pass

async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide async database session for dependency injection.

    Yields:
        AsyncSession: Database session with automatic commit/rollback

    Example:
        @app.get("/tasks")
        async def get_tasks(session: AsyncSession = Depends(get_async_session)):
            ...
    """
    pass

async def init_db() -> None:
    """
    Initialize database (create tables).

    Used for testing and initial setup.
    Production should use Alembic migrations.
    """
    pass
```

**Dependencies**: sqlalchemy.ext.asyncio, src.config, src.models.database_base

**Implementation Notes**:
- Create engine with connection pooling
- Session factory with async context manager
- Proper error handling and rollback
- Used as FastAPI dependency

---

### 3.3 alembic.ini

**Path**: `/Users/sergeychernyakov/www/blank_python_project/alembic.ini`

**Purpose**: Alembic configuration file

**Action**: CREATE new file

**Key Settings**:
```ini
[alembic]
script_location = src/database/migrations
prepend_sys_path = .
version_path_separator = os

sqlalchemy.url = sqlite+aiosqlite:///./tmp/tasks.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

**Implementation Notes**:
- Generated by `alembic init src/database/migrations`
- Update script_location to our structure
- Database URL can be overridden via env.py

---

### 3.4 src/database/migrations/env.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/database/migrations/env.py`

**Purpose**: Alembic environment configuration

**Action**: CREATE new file (generated by alembic init, then customize)

**Key Functions**:
```python
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    pass

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    pass
```

**Dependencies**: alembic, src.config, src.models.database_base, src.models.task

**Implementation Notes**:
- Import all models for autogenerate
- Use config.DATABASE_URL
- Support async migrations
- Import Task model so Alembic can detect it

---

## 4. REPOSITORY LAYER

### 4.1 src/repositories/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/repositories/__init__.py`

**Purpose**: Make repositories a package

**Action**: CREATE new file

**Contents**:
```python
# src/repositories/__init__.py

"""Repository layer for data access."""
```

---

### 4.2 src/repositories/base.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/repositories/base.py`

**Purpose**: Base repository with common CRUD operations

**Action**: CREATE new file

**Key Class**:
```python
class BaseRepository(Generic[T]):
    """
    Base repository with common CRUD operations.

    Type Parameters:
        T: SQLAlchemy model type

    Attributes:
        model_class: The SQLAlchemy model class
    """

    def __init__(self, model_class: type[T]) -> None:
        """Initialize repository with model class."""
        pass

    async def create(
        self,
        session: AsyncSession,
        **kwargs: Any
    ) -> T:
        """
        Create a new record.

        Args:
            session: Database session
            **kwargs: Field values

        Returns:
            Created model instance
        """
        pass

    async def get_by_id(
        self,
        session: AsyncSession,
        id: int
    ) -> T | None:
        """
        Retrieve record by ID.

        Args:
            session: Database session
            id: Record ID

        Returns:
            Model instance or None if not found
        """
        pass

    async def update(
        self,
        session: AsyncSession,
        id: int,
        **kwargs: Any
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
        pass

    async def delete(
        self,
        session: AsyncSession,
        id: int
    ) -> bool:
        """
        Delete record by ID.

        Args:
            session: Database session
            id: Record ID

        Returns:
            True if deleted, False if not found
        """
        pass
```

**Dependencies**: typing, sqlalchemy.ext.asyncio, sqlalchemy.future

**Implementation Notes**:
- Generic base class for reusability
- All methods async
- Session passed as parameter (not stored)
- Returns None instead of raising on not found

---

### 4.3 src/repositories/task_repository.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/repositories/task_repository.py`

**Purpose**: Task-specific data access operations

**Action**: CREATE new file

**Key Class**:
```python
class TaskRepository(BaseRepository[Task]):
    """
    Repository for task data access.

    Provides CRUD operations and query methods for tasks.
    """

    def __init__(self) -> None:
        """Initialize task repository."""
        pass

    async def get_all(
        self,
        session: AsyncSession,
        completed: bool | None = None,
        priority: PriorityEnum | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Task]:
        """
        Retrieve tasks with optional filters and pagination.

        Args:
            session: Database session
            completed: Filter by completion status
            priority: Filter by priority level
            search: Search text in title/description
            limit: Maximum results (max 1000)
            offset: Skip first N results

        Returns:
            List of tasks matching criteria
        """
        pass

    async def mark_complete(
        self,
        session: AsyncSession,
        id: int
    ) -> Task | None:
        """
        Mark task as completed.

        Args:
            session: Database session
            id: Task ID

        Returns:
            Updated task or None if not found
        """
        pass

    async def get_count(
        self,
        session: AsyncSession,
        completed: bool | None = None,
        priority: PriorityEnum | None = None,
        search: str | None = None
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
        pass
```

**Dependencies**: sqlalchemy, src.models.task, src.models.enums, src.repositories.base

**Implementation Notes**:
- Inherits common CRUD from BaseRepository
- Adds task-specific query methods
- Use SQLAlchemy select() for queries
- Filter with .where() clauses
- Search uses .ilike() for case-insensitive search
- Indexes on completed/priority optimize filters

---

## 5. SERVICE LAYER

### 5.1 src/services/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/services/__init__.py`

**Purpose**: Make services a package

**Action**: CREATE new file

**Contents**:
```python
# src/services/__init__.py

"""Service layer for business logic."""
```

---

### 5.2 src/services/task_service.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/services/task_service.py`

**Purpose**: Business logic for task operations

**Action**: CREATE new file

**Key Class**:
```python
class TaskService:
    """
    Service layer for task business logic.

    Encapsulates business rules, validation, and orchestration.
    """

    def __init__(self) -> None:
        """Initialize task service with repository."""
        pass

    async def create_task(
        self,
        session: AsyncSession,
        title: str,
        description: str | None = None,
        priority: PriorityEnum = PriorityEnum.MEDIUM,
        due_date: datetime | None = None
    ) -> Task:
        """
        Create a new task with validation.

        Args:
            session: Database session
            title: Task title (1-200 chars)
            description: Task description (max 2000 chars)
            priority: Priority level
            due_date: Optional due date

        Returns:
            Created task

        Raises:
            ValueError: If validation fails
        """
        pass

    async def get_task_by_id(
        self,
        session: AsyncSession,
        task_id: int
    ) -> Task:
        """
        Get task by ID.

        Args:
            session: Database session
            task_id: Task ID

        Returns:
            Task instance

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        pass

    async def get_tasks(
        self,
        session: AsyncSession,
        completed: bool | None = None,
        priority: PriorityEnum | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Task]:
        """
        Get tasks with filters.

        Args:
            session: Database session
            completed: Filter by completion
            priority: Filter by priority
            search: Search in title/description
            limit: Max results (capped at 1000)
            offset: Pagination offset

        Returns:
            List of tasks
        """
        pass

    async def update_task(
        self,
        session: AsyncSession,
        task_id: int,
        title: str | None = None,
        description: str | None = None,
        priority: PriorityEnum | None = None,
        completed: bool | None = None,
        due_date: datetime | None = None
    ) -> Task:
        """
        Update task with validation.

        Args:
            session: Database session
            task_id: Task ID
            title: New title (if provided)
            description: New description (if provided)
            priority: New priority (if provided)
            completed: New completion status (if provided)
            due_date: New due date (if provided)

        Returns:
            Updated task

        Raises:
            TaskNotFoundError: If task doesn't exist
            ValueError: If validation fails
        """
        pass

    async def delete_task(
        self,
        session: AsyncSession,
        task_id: int
    ) -> None:
        """
        Delete task.

        Args:
            session: Database session
            task_id: Task ID

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        pass

    async def mark_task_complete(
        self,
        session: AsyncSession,
        task_id: int
    ) -> Task:
        """
        Mark task as completed.

        Args:
            session: Database session
            task_id: Task ID

        Returns:
            Updated task

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        pass
```

**Dependencies**: datetime, src.models.task, src.models.enums, src.repositories.task_repository

**Implementation Notes**:
- Validates business rules (title length, etc.)
- Raises domain exceptions (TaskNotFoundError)
- Transforms data between API and repository
- Orchestrates repository calls
- Caps limit at 1000 for safety

---

## 6. API LAYER - EXCEPTIONS

### 6.1 src/api/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/__init__.py`

**Purpose**: Make API a package

**Action**: CREATE new file

**Contents**:
```python
# src/api/__init__.py

"""API layer for REST endpoints."""
```

---

### 6.2 src/api/exceptions.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/exceptions.py`

**Purpose**: Custom exceptions for API

**Action**: CREATE new file

**Key Classes**:
```python
class TaskAPIException(Exception):
    """Base exception for task API."""
    pass

class TaskNotFoundError(TaskAPIException):
    """Raised when task doesn't exist."""

    def __init__(self, task_id: int) -> None:
        """
        Initialize exception.

        Args:
            task_id: ID of task that wasn't found
        """
        pass

class TaskValidationError(TaskAPIException):
    """Raised when task validation fails."""

    def __init__(self, message: str, field: str | None = None) -> None:
        """
        Initialize exception.

        Args:
            message: Validation error message
            field: Field that failed validation
        """
        pass

class DatabaseError(TaskAPIException):
    """Raised when database operation fails."""
    pass
```

**Dependencies**: None (pure Python)

**Implementation Notes**:
- Clear exception hierarchy
- Store context data (task_id, field)
- Used by service layer and exception handlers

---

## 7. API LAYER - SCHEMAS

### 7.1 src/api/v1/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/__init__.py`

**Purpose**: Make v1 API a package

**Action**: CREATE new file

**Contents**:
```python
# src/api/v1/__init__.py

"""API v1 package."""
```

---

### 7.2 src/api/v1/schemas/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/schemas/__init__.py`

**Purpose**: Make schemas a package

**Action**: CREATE new file

**Contents**:
```python
# src/api/v1/schemas/__init__.py

"""Pydantic schemas for request/response validation."""
```

---

### 7.3 src/api/v1/schemas/task.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/schemas/task.py`

**Purpose**: Pydantic schemas for task endpoints

**Action**: CREATE new file

**Key Classes**:
```python
class TaskBase(BaseModel):
    """Base task schema with common fields."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    priority: PriorityEnum = Field(default=PriorityEnum.MEDIUM)
    due_date: datetime | None = None


class TaskCreate(TaskBase):
    """Schema for creating a task."""
    pass


class TaskUpdate(BaseModel):
    """
    Schema for updating a task.

    All fields optional - only provided fields are updated.
    """

    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    priority: PriorityEnum | None = None
    completed: bool | None = None
    due_date: datetime | None = None


class TaskResponse(TaskBase):
    """Schema for task response."""

    id: int
    completed: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

**Dependencies**: pydantic, datetime, src.models.enums

**Implementation Notes**:
- TaskBase has common fields
- TaskCreate inherits all required fields
- TaskUpdate makes everything optional
- TaskResponse adds read-only fields (id, timestamps)
- from_attributes=True enables SQLAlchemy model conversion
- Field validation automatic via Pydantic

---

### 7.4 src/api/v1/schemas/common.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/schemas/common.py`

**Purpose**: Common schemas for pagination, errors, etc.

**Action**: CREATE new file

**Key Classes**:
```python
class ErrorResponse(BaseModel):
    """Schema for error responses."""

    detail: str
    field: str | None = None


class HealthResponse(BaseModel):
    """Schema for health check response."""

    status: str
    timestamp: datetime
    database: str
```

**Dependencies**: pydantic, datetime

**Implementation Notes**:
- Standardized error format
- Health check includes DB status
- Can add pagination metadata later if needed

---

## 8. API LAYER - DEPENDENCIES

### 8.1 src/api/dependencies.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/dependencies.py`

**Purpose**: Dependency injection for FastAPI

**Action**: CREATE new file

**Key Functions**:
```python
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide database session for request.

    Yields:
        AsyncSession: Database session

    Example:
        @app.get("/tasks")
        async def get_tasks(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    pass


def get_task_service() -> TaskService:
    """
    Provide task service instance.

    Returns:
        TaskService: Service instance
    """
    pass
```

**Dependencies**: src.database.session, src.services.task_service

**Implementation Notes**:
- get_db_session wraps database session generator
- Automatically commits/rolls back
- Services are stateless, can be singletons
- Used in route handlers via Depends()

---

## 9. API LAYER - ROUTES

### 9.1 src/api/v1/routes/__init__.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/routes/__init__.py`

**Purpose**: Make routes a package

**Action**: CREATE new file

**Contents**:
```python
# src/api/v1/routes/__init__.py

"""API route handlers."""
```

---

### 9.2 src/api/v1/routes/health.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/routes/health.py`

**Purpose**: Health check endpoint

**Action**: CREATE new file

**Key Route**:
```python
router = APIRouter()

@router.get("/health", response_model=HealthResponse)
async def health_check(
    session: AsyncSession = Depends(get_db_session)
) -> HealthResponse:
    """
    Health check endpoint.

    Args:
        session: Database session

    Returns:
        Health status including database connectivity
    """
    pass
```

**Dependencies**: fastapi, datetime, src.api.dependencies, src.api.v1.schemas.common

**Implementation Notes**:
- Tests database connectivity
- Returns current timestamp
- No business logic needed

---

### 9.3 src/api/v1/routes/tasks.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/v1/routes/tasks.py`

**Purpose**: Task CRUD endpoints

**Action**: CREATE new file

**Key Routes**:
```python
router = APIRouter()

@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    task: TaskCreate,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """Create a new task."""
    pass

@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(
    completed: bool | None = None,
    priority: PriorityEnum | None = None,
    search: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> list[TaskResponse]:
    """List tasks with optional filters."""
    pass

@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """Get a single task by ID."""
    pass

@router.put("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    task: TaskUpdate,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """Update a task."""
    pass

@router.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
async def mark_task_complete(
    task_id: int,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """Mark a task as complete."""
    pass

@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> None:
    """Delete a task."""
    pass
```

**Dependencies**: fastapi, src.api.dependencies, src.api.v1.schemas.task, src.services.task_service

**Implementation Notes**:
- All routes async
- Dependency injection for session and service
- Pydantic auto-validates request/response
- Query parameters for filtering
- Proper HTTP status codes (201, 204, etc.)

---

## 10. API LAYER - MAIN APP

### 10.1 src/api/main.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/src/api/main.py`

**Purpose**: FastAPI application initialization

**Action**: CREATE new file

**Key Components**:
```python
def create_app() -> FastAPI:
    """
    Create and configure FastAPI application.

    Returns:
        FastAPI: Configured application instance
    """
    pass

# Exception handlers
@app.exception_handler(TaskNotFoundError)
async def task_not_found_handler(
    request: Request,
    exc: TaskNotFoundError
) -> JSONResponse:
    """Handle task not found errors."""
    pass

@app.exception_handler(DatabaseError)
async def database_error_handler(
    request: Request,
    exc: DatabaseError
) -> JSONResponse:
    """Handle database errors."""
    pass

# Startup/shutdown events
@app.on_event("startup")
async def startup() -> None:
    """Initialize on startup."""
    pass

@app.on_event("shutdown")
async def shutdown() -> None:
    """Cleanup on shutdown."""
    pass
```

**Dependencies**: fastapi, src.config, src.api.v1.routes, src.api.exceptions

**Implementation Notes**:
- Create app with config settings
- Register routers with prefix
- Add CORS middleware
- Register exception handlers
- Initialize database on startup
- Setup logging
- Provide /docs for API documentation

---

## 11. APPLICATION ENTRY POINT

### 11.1 run_api.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/run_api.py`

**Purpose**: Entry point to run API server

**Action**: CREATE new file

**Contents**:
```python
# run_api.py

"""
Entry point for running the FastAPI application.

Run with: python run_api.py
Or with uvicorn: uvicorn src.api.main:app --reload
"""

import uvicorn

from src.config import config


def main() -> None:
    """
    Run the FastAPI application with uvicorn.

    Configuration loaded from environment variables.
    """
    uvicorn.run(
        "src.api.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        log_level="debug" if config.DEBUG else "info"
    )


if __name__ == "__main__":
    main()
```

**Dependencies**: uvicorn, src.config

**Implementation Notes**:
- Simple entry point
- Uses config for host/port
- Auto-reload in development
- Can also run directly with uvicorn command

---

## 12. TESTING FILES

### 12.1 tests/conftest.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/tests/conftest.py`

**Purpose**: Global test fixtures

**Action**: UPDATE existing file

**Key Fixtures**:
```python
@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    pass

@pytest.fixture
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide test database session."""
    pass

@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Provide async HTTP client for API tests."""
    pass

@pytest.fixture
async def sample_task(async_session: AsyncSession) -> Task:
    """Create a sample task for testing."""
    pass
```

**Dependencies**: pytest, pytest-asyncio, httpx, src.database.session, src.models.task

**Implementation Notes**:
- Use in-memory SQLite for tests
- Create tables before tests
- Clean up after each test
- Provide HTTP client for integration tests

---

### 12.2 tests/repositories/test_task_repository.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/tests/repositories/test_task_repository.py`

**Purpose**: Test task repository

**Action**: CREATE new file

**Test Functions**:
```python
async def test_create_task(async_session):
    """Test creating a task."""
    pass

async def test_get_task_by_id(async_session, sample_task):
    """Test retrieving task by ID."""
    pass

async def test_get_task_not_found(async_session):
    """Test getting non-existent task."""
    pass

async def test_update_task(async_session, sample_task):
    """Test updating a task."""
    pass

async def test_delete_task(async_session, sample_task):
    """Test deleting a task."""
    pass

async def test_get_all_tasks(async_session):
    """Test listing all tasks."""
    pass

async def test_filter_by_completed(async_session):
    """Test filtering by completion status."""
    pass

async def test_filter_by_priority(async_session):
    """Test filtering by priority."""
    pass

async def test_search_tasks(async_session):
    """Test searching in title/description."""
    pass

async def test_pagination(async_session):
    """Test limit and offset."""
    pass

async def test_mark_complete(async_session, sample_task):
    """Test marking task as complete."""
    pass
```

**Dependencies**: pytest, src.repositories.task_repository

---

### 12.3 tests/services/test_task_service.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/tests/services/test_task_service.py`

**Purpose**: Test task service

**Action**: CREATE new file

**Test Functions**:
```python
async def test_create_task_success(async_session):
    """Test creating task with valid data."""
    pass

async def test_create_task_validation_error(async_session):
    """Test creating task with invalid data."""
    pass

async def test_get_task_by_id(async_session, sample_task):
    """Test getting task by ID."""
    pass

async def test_get_task_not_found(async_session):
    """Test getting non-existent task raises error."""
    pass

async def test_update_task(async_session, sample_task):
    """Test updating task."""
    pass

async def test_delete_task(async_session, sample_task):
    """Test deleting task."""
    pass

async def test_delete_task_not_found(async_session):
    """Test deleting non-existent task raises error."""
    pass

async def test_mark_complete(async_session, sample_task):
    """Test marking task as complete."""
    pass

async def test_get_tasks_with_filters(async_session):
    """Test filtering tasks."""
    pass

async def test_limit_capped_at_1000(async_session):
    """Test that limit is capped at 1000."""
    pass
```

**Dependencies**: pytest, src.services.task_service, src.api.exceptions

---

### 12.4 tests/api/v1/test_tasks.py

**Path**: `/Users/sergeychernyakov/www/blank_python_project/tests/api/v1/test_tasks.py`

**Purpose**: Test task API endpoints

**Action**: CREATE new file

**Test Functions**:
```python
async def test_create_task(async_client):
    """Test POST /api/v1/tasks."""
    pass

async def test_create_task_validation_error(async_client):
    """Test POST with invalid data returns 422."""
    pass

async def test_list_tasks(async_client):
    """Test GET /api/v1/tasks."""
    pass

async def test_list_tasks_with_filters(async_client):
    """Test GET /api/v1/tasks with query params."""
    pass

async def test_get_task(async_client, sample_task):
    """Test GET /api/v1/tasks/{id}."""
    pass

async def test_get_task_not_found(async_client):
    """Test GET /api/v1/tasks/{id} returns 404."""
    pass

async def test_update_task(async_client, sample_task):
    """Test PUT /api/v1/tasks/{id}."""
    pass

async def test_update_task_not_found(async_client):
    """Test PUT /api/v1/tasks/{id} returns 404."""
    pass

async def test_mark_complete(async_client, sample_task):
    """Test PATCH /api/v1/tasks/{id}/complete."""
    pass

async def test_delete_task(async_client, sample_task):
    """Test DELETE /api/v1/tasks/{id}."""
    pass

async def test_delete_task_not_found(async_client):
    """Test DELETE /api/v1/tasks/{id} returns 404."""
    pass

async def test_health_check(async_client):
    """Test GET /api/v1/health."""
    pass
```

**Dependencies**: pytest, httpx, src.api.main

---

## 13. IMPLEMENTATION ORDER

Implement files in this order to satisfy dependencies:

1. **Configuration**: requirements.txt, settings.py, .env
2. **Enums**: models/enums.py (add PriorityEnum)
3. **Database Base**: models/database_base.py
4. **Models**: models/task.py
5. **Database Session**: database/session.py
6. **Alembic Setup**: alembic.ini, database/migrations/env.py
7. **Initial Migration**: Run `alembic revision --autogenerate -m "Initial schema"`
8. **Apply Migration**: Run `alembic upgrade head`
9. **Repository Base**: repositories/base.py
10. **Task Repository**: repositories/task_repository.py
11. **Repository Tests**: tests/repositories/test_task_repository.py
12. **Exceptions**: api/exceptions.py
13. **Task Service**: services/task_service.py
14. **Service Tests**: tests/services/test_task_service.py
15. **Schemas**: api/v1/schemas/task.py, api/v1/schemas/common.py
16. **Dependencies**: api/dependencies.py
17. **Routes**: api/v1/routes/health.py, api/v1/routes/tasks.py
18. **Main App**: api/main.py
19. **API Tests**: tests/api/v1/test_tasks.py
20. **Test Fixtures**: tests/conftest.py (update)
21. **Entry Point**: run_api.py
22. **Run Tests**: pytest with coverage
23. **Run Linters**: black, isort, ruff, pylint

---

## 14. SUMMARY

**Total New Files**: 30
**Updated Files**: 4
**Total Lines (estimated)**: ~2000 LOC (within FEATURE scope)

All files follow project conventions:
- First line: file path comment
- snake_case for files, CamelCase for classes
- Type hints everywhere
- Google-style docstrings
- Logging, no print()
- No hardcoded secrets

Implementation ready for @agent-senior-coder.
