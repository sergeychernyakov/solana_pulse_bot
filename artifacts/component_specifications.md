# Component Specifications

**Project**: REST API Task Management
**Date**: 2025-10-23
**Architect**: @agent-code-architect

---

## Purpose

This document provides detailed specifications for each major component. These are design specifications (interfaces, contracts, behaviors) - NOT implementations.

---

## 1. DATABASE SESSION COMPONENT

### File: src/database/session.py

**Purpose**: Manage async database connections and sessions

**Dependencies**:
- sqlalchemy.ext.asyncio.AsyncEngine
- sqlalchemy.ext.asyncio.AsyncSession
- sqlalchemy.ext.asyncio.create_async_engine
- src.config.settings

**Module-Level Variables**:
```python
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
```

**Function: get_async_engine()**
- Returns global AsyncEngine instance (singleton pattern)
- Creates engine on first call using config.DATABASE_URL
- Configuration: echo=config.DATABASE_ECHO, pool_pre_ping=True
- Thread-safe singleton

**Function: get_async_session()**
- Async generator function (yields AsyncSession)
- Creates session from factory
- Yields session to caller
- Commits on successful completion
- Rolls back on exception
- Always closes session in finally block
- Used as FastAPI dependency

**Function: init_db()**
- Creates all tables using Base.metadata.create_all()
- Only for testing/initial setup
- Production uses Alembic migrations
- Should be async function

**Behaviors**:
- Connection pooling enabled by default
- Sessions are request-scoped (one per API request)
- Automatic transaction management
- Proper cleanup even on errors

---

## 2. TASK REPOSITORY COMPONENT

### File: src/repositories/task_repository.py

**Purpose**: Data access layer for task operations

**Dependencies**:
- src.repositories.base.BaseRepository
- src.models.task.Task
- src.models.enums.PriorityEnum
- sqlalchemy.ext.asyncio.AsyncSession
- sqlalchemy.future.select

**Class: TaskRepository(BaseRepository[Task])**

Inherits from BaseRepository, gets these methods automatically:
- create()
- get_by_id()
- update()
- delete()

**Method: __init__()**
- Calls super().__init__(Task)
- No additional initialization needed

**Method: get_all()**
- Parameters:
  - session: AsyncSession
  - completed: bool | None = None
  - priority: PriorityEnum | None = None
  - search: str | None = None
  - limit: int = 100
  - offset: int = 0
- Returns: list[Task]
- Behavior:
  - Builds SELECT query with filters
  - If completed is not None: WHERE completed = completed
  - If priority is not None: WHERE priority = priority
  - If search is not None: WHERE title LIKE %search% OR description LIKE %search%
  - LIMIT and OFFSET for pagination
  - ORDER BY created_at DESC
  - Executes with session.execute()
  - Returns list of Task instances

**Method: mark_complete()**
- Parameters: session, id
- Returns: Task | None
- Behavior:
  - Gets task by ID
  - If not found, returns None
  - Sets completed = True
  - Sets updated_at = current time
  - Commits transaction
  - Returns updated task

**Method: get_count()**
- Parameters: session, completed, priority, search (same as get_all)
- Returns: int
- Behavior:
  - Same filters as get_all()
  - Uses COUNT(*) instead of SELECT *
  - Returns total count of matching tasks

**Query Building Pattern**:
```python
# Example pattern (not implementation)
stmt = select(Task)
if completed is not None:
    stmt = stmt.where(Task.completed == completed)
if priority is not None:
    stmt = stmt.where(Task.priority == priority)
if search is not None:
    stmt = stmt.where(
        or_(
            Task.title.ilike(f"%{search}%"),
            Task.description.ilike(f"%{search}%")
        )
    )
stmt = stmt.order_by(Task.created_at.desc())
stmt = stmt.limit(limit).offset(offset)
result = await session.execute(stmt)
return result.scalars().all()
```

**Performance Considerations**:
- Use indexes on completed, priority, created_at
- ILIKE for case-insensitive search
- Parameterized queries (no SQL injection)

---

## 3. TASK SERVICE COMPONENT

### File: src/services/task_service.py

**Purpose**: Business logic layer for task operations

**Dependencies**:
- src.repositories.task_repository.TaskRepository
- src.models.task.Task
- src.models.enums.PriorityEnum
- src.api.exceptions (TaskNotFoundError, TaskValidationError)
- src.helpers.logger.get_logger
- datetime.datetime
- sqlalchemy.ext.asyncio.AsyncSession

**Class: TaskService**

**Attributes**:
- repository: TaskRepository (instance variable)
- logger: Logger (instance variable)

**Method: __init__()**
- Creates TaskRepository instance
- Gets logger for this module
- No parameters needed

**Method: create_task()**
- Parameters:
  - session: AsyncSession
  - title: str
  - description: str | None = None
  - priority: PriorityEnum = PriorityEnum.MEDIUM
  - due_date: datetime | None = None
- Returns: Task
- Raises: ValueError if validation fails
- Validation Rules:
  - title must not be empty (after strip)
  - title length must be 1-200 characters
  - description length must be <= 2000 characters (if provided)
  - due_date must be in future (if provided)
- Behavior:
  - Log task creation attempt
  - Validate all inputs
  - Call repository.create() with validated data
  - Log successful creation with task ID
  - Return created task

**Method: get_task_by_id()**
- Parameters: session, task_id
- Returns: Task
- Raises: TaskNotFoundError if task doesn't exist
- Behavior:
  - Call repository.get_by_id()
  - If None returned, raise TaskNotFoundError(task_id)
  - Return task

**Method: get_tasks()**
- Parameters: session, completed, priority, search, limit, offset
- Returns: list[Task]
- Behavior:
  - Cap limit at 1000 for safety
  - Call repository.get_all() with filters
  - Return list of tasks
  - No exception on empty list

**Method: update_task()**
- Parameters:
  - session: AsyncSession
  - task_id: int
  - title: str | None = None
  - description: str | None = None
  - priority: PriorityEnum | None = None
  - completed: bool | None = None
  - due_date: datetime | None = None
- Returns: Task
- Raises: TaskNotFoundError, ValueError
- Behavior:
  - Validate provided fields (same rules as create)
  - Build dict of fields to update (only non-None values)
  - Call repository.update()
  - If None returned, raise TaskNotFoundError
  - Return updated task

**Method: delete_task()**
- Parameters: session, task_id
- Returns: None
- Raises: TaskNotFoundError
- Behavior:
  - Call repository.delete()
  - If False returned, raise TaskNotFoundError
  - Return None on success

**Method: mark_task_complete()**
- Parameters: session, task_id
- Returns: Task
- Raises: TaskNotFoundError
- Behavior:
  - Call repository.mark_complete()
  - If None returned, raise TaskNotFoundError
  - Log completion
  - Return updated task

**Logging Strategy**:
- INFO: Task created/updated/deleted with ID
- WARNING: Validation failures
- ERROR: Unexpected errors
- Use lazy formatting: logger.info("Task %d created", task_id)

**Transaction Boundaries**:
- Service methods do NOT manage transactions
- Repository operations use session provided by caller
- API layer (dependency injection) manages commit/rollback

---

## 4. TASK SCHEMAS COMPONENT

### File: src/api/v1/schemas/task.py

**Purpose**: Pydantic models for request/response validation

**Dependencies**:
- pydantic.BaseModel
- pydantic.Field
- pydantic.ConfigDict
- src.models.enums.PriorityEnum
- datetime.datetime

**Class: TaskBase**
- Inherits: BaseModel
- Fields:
  - title: str (Field with min_length=1, max_length=200, required)
  - description: str | None (Field with max_length=2000, optional)
  - priority: PriorityEnum (Field with default=PriorityEnum.MEDIUM)
  - due_date: datetime | None (optional)
- Purpose: Common fields for create/response schemas
- Validation: Automatic via Pydantic Field constraints

**Class: TaskCreate**
- Inherits: TaskBase
- Additional Fields: None
- Purpose: Request schema for creating tasks
- All fields from TaskBase (title required, others optional)

**Class: TaskUpdate**
- Inherits: BaseModel
- Fields (all optional):
  - title: str | None (Field with min_length=1, max_length=200)
  - description: str | None (Field with max_length=2000)
  - priority: PriorityEnum | None
  - completed: bool | None
  - due_date: datetime | None
- Purpose: Request schema for updating tasks
- Partial update: only provided fields are updated

**Class: TaskResponse**
- Inherits: TaskBase
- Additional Fields:
  - id: int
  - completed: bool
  - created_at: datetime
  - updated_at: datetime
- Config: model_config = ConfigDict(from_attributes=True)
- Purpose: Response schema for API
- from_attributes=True enables SQLAlchemy model conversion

**Validation Behavior**:
- Pydantic automatically validates on instantiation
- Raises ValidationError with detailed field errors
- FastAPI automatically converts ValidationError to 422 response
- JSON serialization/deserialization automatic

**Example Usage Pattern**:
```python
# Request validation (automatic in FastAPI)
task_create = TaskCreate(**request_json)

# SQLAlchemy to Pydantic (response)
task_response = TaskResponse.model_validate(task_model)

# Partial update
task_update = TaskUpdate(title="New title")  # Only title provided
```

---

## 5. TASK ROUTES COMPONENT

### File: src/api/v1/routes/tasks.py

**Purpose**: FastAPI route handlers for task endpoints

**Dependencies**:
- fastapi.APIRouter
- fastapi.Depends
- fastapi.Query
- src.api.dependencies (get_db_session, get_task_service)
- src.api.v1.schemas.task (TaskCreate, TaskUpdate, TaskResponse)
- src.services.task_service.TaskService
- src.models.enums.PriorityEnum
- src.helpers.logger.get_logger
- sqlalchemy.ext.asyncio.AsyncSession

**Module-Level**:
```python
router = APIRouter()
logger = get_logger(__name__)
```

**Route: POST /tasks**
- Decorator: @router.post("/tasks", response_model=TaskResponse, status_code=201)
- Parameters:
  - task: TaskCreate (request body, automatic validation)
  - service: TaskService (dependency injection)
  - session: AsyncSession (dependency injection)
- Returns: TaskResponse
- Behavior:
  - Log incoming request
  - Extract fields from task schema
  - Call service.create_task()
  - Convert Task model to TaskResponse
  - FastAPI serializes to JSON
  - Returns 201 with task
- Error Handling:
  - ValueError from service → let exception handler convert to 422
  - Pydantic ValidationError → automatic 422

**Route: GET /tasks**
- Decorator: @router.get("/tasks", response_model=list[TaskResponse])
- Parameters:
  - completed: bool | None = None (query param)
  - priority: PriorityEnum | None = None (query param)
  - search: str | None = None (query param)
  - limit: int = Query(default=100, le=1000) (query param with validation)
  - offset: int = Query(default=0, ge=0) (query param with validation)
  - service: TaskService (dependency)
  - session: AsyncSession (dependency)
- Returns: list[TaskResponse]
- Behavior:
  - Log request with filters
  - Call service.get_tasks() with all filters
  - Convert list of Task models to list of TaskResponse
  - Return 200 with array

**Route: GET /tasks/{task_id}**
- Decorator: @router.get("/tasks/{task_id}", response_model=TaskResponse)
- Parameters:
  - task_id: int (path parameter)
  - service: TaskService (dependency)
  - session: AsyncSession (dependency)
- Returns: TaskResponse
- Behavior:
  - Call service.get_task_by_id()
  - If raises TaskNotFoundError, exception handler returns 404
  - Convert to TaskResponse
  - Return 200

**Route: PUT /tasks/{task_id}**
- Decorator: @router.put("/tasks/{task_id}", response_model=TaskResponse)
- Parameters:
  - task_id: int (path parameter)
  - task: TaskUpdate (request body)
  - service: TaskService (dependency)
  - session: AsyncSession (dependency)
- Returns: TaskResponse
- Behavior:
  - Extract fields from task schema (only non-None)
  - Call service.update_task() with extracted fields
  - Convert to TaskResponse
  - Return 200

**Route: PATCH /tasks/{task_id}/complete**
- Decorator: @router.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
- Parameters:
  - task_id: int (path parameter)
  - service: TaskService (dependency)
  - session: AsyncSession (dependency)
- Returns: TaskResponse
- Behavior:
  - Call service.mark_task_complete()
  - Convert to TaskResponse
  - Return 200

**Route: DELETE /tasks/{task_id}**
- Decorator: @router.delete("/tasks/{task_id}", status_code=204)
- Parameters:
  - task_id: int (path parameter)
  - service: TaskService (dependency)
  - session: AsyncSession (dependency)
- Returns: None
- Behavior:
  - Call service.delete_task()
  - Return 204 No Content (empty body)

**Logging Pattern**:
- Log method and path at start of each handler
- Log task ID for operations on specific tasks
- Use INFO level for normal operations
- Errors logged by exception handlers

---

## 6. FASTAPI APPLICATION COMPONENT

### File: src/api/main.py

**Purpose**: FastAPI application initialization and configuration

**Dependencies**:
- fastapi.FastAPI
- fastapi.Request
- fastapi.responses.JSONResponse
- src.config.settings.config
- src.api.v1.routes.tasks
- src.api.v1.routes.health
- src.api.exceptions (all custom exceptions)
- src.helpers.logger.get_logger

**Function: create_app()**
- Returns: FastAPI
- Behavior:
  - Create FastAPI instance with metadata:
    - title="Task Management API"
    - version="1.0.0"
    - description="REST API for managing tasks"
  - Add CORS middleware with config.CORS_ORIGINS
  - Include routers:
    - tasks.router with prefix config.API_V1_PREFIX
    - health.router with prefix config.API_V1_PREFIX
  - Register exception handlers
  - Add startup/shutdown event handlers
  - Return configured app

**Global: app = create_app()**
- Module-level app instance
- Used by uvicorn

**Exception Handler: task_not_found_handler()**
- Decorator: @app.exception_handler(TaskNotFoundError)
- Parameters: request, exc
- Returns: JSONResponse with status 404
- Body: {"detail": "Task with ID {task_id} not found"}

**Exception Handler: task_validation_handler()**
- Decorator: @app.exception_handler(TaskValidationError)
- Parameters: request, exc
- Returns: JSONResponse with status 422
- Body: {"detail": error_message, "field": field_name}

**Exception Handler: database_error_handler()**
- Decorator: @app.exception_handler(DatabaseError)
- Parameters: request, exc
- Returns: JSONResponse with status 500
- Body: {"detail": "Internal server error"}
- Logs full exception with traceback

**Startup Event: startup()**
- Decorator: @app.on_event("startup")
- Behavior:
  - Log application startup
  - Initialize database engine
  - Log configuration (env, db url, etc.)

**Shutdown Event: shutdown()**
- Decorator: @app.on_event("shutdown")
- Behavior:
  - Log application shutdown
  - Close database connections
  - Cleanup resources

**CORS Configuration**:
- Allow origins from config.CORS_ORIGINS
- Allow credentials: True
- Allow methods: ["*"]
- Allow headers: ["*"]

---

## 7. TEST FIXTURES COMPONENT

### File: tests/conftest.py

**Purpose**: Shared test fixtures for all tests

**Dependencies**:
- pytest
- pytest_asyncio
- httpx.AsyncClient
- sqlalchemy.ext.asyncio
- src.database.session
- src.api.main
- src.models.task.Task
- src.models.enums.PriorityEnum

**Fixture: event_loop**
- Scope: session
- Purpose: Provide event loop for async tests
- Returns: asyncio event loop
- Cleanup: Close loop after session

**Fixture: test_engine**
- Scope: session
- Purpose: Create test database engine
- Returns: AsyncEngine
- Configuration: Use in-memory SQLite (sqlite+aiosqlite:///:memory:)
- Cleanup: Dispose engine after session

**Fixture: async_session**
- Scope: function (new session per test)
- Purpose: Provide database session for tests
- Returns: AsyncSession
- Setup:
  - Create all tables
  - Begin transaction
- Yield: Session to test
- Cleanup:
  - Rollback transaction
  - Drop all tables
- Ensures test isolation

**Fixture: async_client**
- Scope: function
- Purpose: Provide HTTP client for API tests
- Returns: httpx.AsyncClient
- Setup:
  - Override get_db_session dependency to use test session
  - Create client with app and base_url
- Yield: Client to test
- Cleanup: Close client

**Fixture: sample_task**
- Scope: function
- Purpose: Create a task for testing
- Parameters: async_session (dependency)
- Returns: Task
- Behavior:
  - Create task with known values:
    - title="Sample Task"
    - description="Sample Description"
    - priority=PriorityEnum.MEDIUM
    - completed=False
  - Add to session and commit
  - Return task instance

**Fixture: sample_tasks**
- Scope: function
- Purpose: Create multiple tasks for list/filter tests
- Parameters: async_session
- Returns: list[Task]
- Behavior:
  - Create 5 tasks with different combinations:
    - completed True/False
    - priority LOW/MEDIUM/HIGH
    - different titles
  - Return list of tasks

**Fixture Usage Pattern**:
```python
# In test file
async def test_get_task(async_session, sample_task):
    # sample_task is already in database
    # async_session is connected to test database
    # Test can use both fixtures
    pass
```

---

## 8. DEPENDENCY INJECTION COMPONENT

### File: src/api/dependencies.py

**Purpose**: Provide FastAPI dependencies for route handlers

**Dependencies**:
- typing.AsyncGenerator
- sqlalchemy.ext.asyncio.AsyncSession
- src.database.session.get_async_session
- src.services.task_service.TaskService

**Function: get_db_session()**
- Signature: async def get_db_session() -> AsyncGenerator[AsyncSession, None]
- Purpose: Provide database session per request
- Behavior:
  - Wraps database.session.get_async_session()
  - Yields session
  - FastAPI handles cleanup
- Usage in routes: session: AsyncSession = Depends(get_db_session)

**Function: get_task_service()**
- Signature: def get_task_service() -> TaskService
- Purpose: Provide TaskService instance
- Behavior:
  - Create new TaskService instance
  - Services are stateless, can be created per request
  - Alternative: use singleton pattern with lru_cache
- Usage in routes: service: TaskService = Depends(get_task_service)

**Dependency Injection Flow**:
```
Route handler declares dependencies
    ↓
FastAPI calls dependency functions
    ↓
Dependencies provide resources
    ↓
Route handler executes with resources
    ↓
FastAPI calls cleanup (for generators)
    ↓
Response returned
```

**Benefits**:
- Automatic resource management
- Easy to test (override dependencies)
- Clear separation of concerns
- Type-safe (type hints on dependencies)

---

## 9. ALEMBIC MIGRATION COMPONENT

### File: src/database/migrations/env.py

**Purpose**: Alembic environment configuration for migrations

**Dependencies**:
- alembic
- sqlalchemy
- src.config.settings.config
- src.models.database_base.Base
- src.models.task.Task (MUST import for autogenerate)

**Key Configuration**:
- target_metadata = Base.metadata
- sqlalchemy.url = config.DATABASE_URL
- Support for async migrations

**Function: run_migrations_offline()**
- Purpose: Generate SQL without database connection
- Use case: For manual migration review
- Behavior:
  - Configure alembic with URL
  - Generate migration SQL
  - Output to stdout or file

**Function: run_migrations_online()**
- Purpose: Execute migrations against database
- Use case: Normal migration execution
- Behavior:
  - Create async engine
  - Connect to database
  - Execute migration in transaction
  - Close connection

**Import Requirements**:
- MUST import all SQLAlchemy models
- Alembic uses Base.metadata to detect changes
- Missing imports = missing tables in migration

**Migration Commands**:
```bash
# Create migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Rollback migration
alembic downgrade -1

# Show current version
alembic current

# Show migration history
alembic history
```

---

## Component Integration Summary

```
Client Request
    ↓
FastAPI App (main.py)
    ↓
Route Handler (routes/tasks.py)
    ↓ (dependencies)
├─ DB Session (dependencies.py → session.py)
└─ Task Service (dependencies.py → task_service.py)
    ↓
Task Repository (task_repository.py)
    ↓
Database (via AsyncSession)
    ↑
SQLAlchemy Models (task.py)
    ↓
Pydantic Schemas (schemas/task.py)
    ↓
JSON Response
```

**Data Flow**:
1. Request → Pydantic schema validation
2. Schema → Service layer
3. Service → Repository layer
4. Repository → Database via SQLAlchemy
5. Database → SQLAlchemy model
6. Model → Service → Route
7. Model → Pydantic schema (response)
8. Schema → JSON → Client

**Error Flow**:
1. Error occurs in any layer
2. Exception raised (custom or standard)
3. FastAPI exception handler catches
4. Handler converts to JSONResponse
5. Appropriate HTTP status code
6. Error logged with context

---

**End of Component Specifications**

All components are specified with clear contracts, behaviors, and integration points. No implementation code provided - only specifications for @agent-senior-coder to implement.
