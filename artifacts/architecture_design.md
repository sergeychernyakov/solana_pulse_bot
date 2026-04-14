# REST API Task Management - Architecture Design

**Project**: TODO List REST API with Database Persistence
**Date**: 2025-10-23
**Architect**: @agent-code-architect
**Location**: /Users/sergeychernyakov/www/blank_python_project

---

## 1. ARCHITECTURE SUMMARY

- **Goal**: Build a production-ready REST API for task management with full CRUD operations, filtering, pagination, and database persistence
- **Framework**: FastAPI with async/await for high-performance async request handling
- **Database**: SQLAlchemy 2.0 async ORM with SQLite (dev) and PostgreSQL (prod) support
- **Architecture Pattern**: Layered architecture (API → Service → Repository → Database) for clear separation of concerns
- **Key Design Decisions**:
  - Repository pattern abstracts data access for easy database swapping
  - Service layer encapsulates all business logic and validation
  - Pydantic schemas provide automatic request/response validation
  - Dependency injection manages database sessions per request
  - Alembic handles versioned database migrations
  - Comprehensive error handling with proper HTTP status codes
  - Async-first design for scalability
- **Quality Standards**: 90%+ test coverage, type hints everywhere, Google-style docstrings, pylint score >= 9.5
- **Security**: No hardcoded secrets, input validation via Pydantic, SQL injection prevention via ORM

---

## 2. SYSTEM ARCHITECTURE

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENT                               │
│                    (HTTP Requests)                          │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    API LAYER                                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  FastAPI Application                                 │   │
│  │  - Routing                                          │   │
│  │  - Request Validation (Pydantic)                    │   │
│  │  - Response Serialization                           │   │
│  │  - Error Handling                                   │   │
│  │  - Dependency Injection                             │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                  SERVICE LAYER                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  TaskService                                         │   │
│  │  - Business Logic                                    │   │
│  │  - Data Transformation                               │   │
│  │  - Validation Rules                                  │   │
│  │  - Transaction Orchestration                         │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                REPOSITORY LAYER                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  TaskRepository                                      │   │
│  │  - CRUD Operations                                   │   │
│  │  - Query Building                                    │   │
│  │  - Data Access Abstraction                          │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                 DATABASE LAYER                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  SQLAlchemy AsyncSession                             │   │
│  │  - Connection Pool                                   │   │
│  │  - Transaction Management                            │   │
│  │  - SQLite / PostgreSQL                              │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Request Flow Example (Create Task)

```
1. Client → POST /api/v1/tasks + JSON body
                ↓
2. FastAPI validates request body against TaskCreate schema (Pydantic)
                ↓
3. Dependency injection provides async DB session
                ↓
4. Route handler → TaskService.create_task(data, session)
                ↓
5. Service layer validates business rules, transforms data
                ↓
6. Service → TaskRepository.create(task_data, session)
                ↓
7. Repository creates SQLAlchemy model, adds to session
                ↓
8. Database commits transaction, returns Task with ID
                ↓
9. Repository → Service → API converts to TaskResponse schema
                ↓
10. FastAPI serializes to JSON, returns HTTP 201
```

---

## 3. COMPONENTS & INTERFACES

### 3.1 Database Layer

#### 3.1.1 Task Model (SQLAlchemy)

```python
# src/models/task.py
class Task(Base):
    """SQLAlchemy model for tasks table."""

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[PriorityEnum] = mapped_column(Enum(PriorityEnum))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

#### 3.1.2 Database Session Management

```python
# src/database/session.py
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provides async database session per request.

    Yields:
        AsyncSession: Database session with automatic commit/rollback
    """
    pass  # @agent-senior-coder implements
```

### 3.2 Repository Layer

#### 3.2.1 Base Repository

```python
# src/repositories/base.py
class BaseRepository(Generic[T]):
    """Base repository with common CRUD operations."""

    async def create(self, data: dict[str, Any], session: AsyncSession) -> T:
        """Create a new record."""
        pass

    async def get_by_id(self, id: int, session: AsyncSession) -> T | None:
        """Retrieve record by ID."""
        pass

    async def update(self, id: int, data: dict[str, Any], session: AsyncSession) -> T | None:
        """Update record by ID."""
        pass

    async def delete(self, id: int, session: AsyncSession) -> bool:
        """Delete record by ID."""
        pass
```

#### 3.2.2 Task Repository

```python
# src/repositories/task_repository.py
class TaskRepository(BaseRepository[Task]):
    """Repository for task data access."""

    async def get_all(
        self,
        session: AsyncSession,
        completed: bool | None = None,
        priority: PriorityEnum | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0
    ) -> list[Task]:
        """Retrieve tasks with filters and pagination."""
        pass

    async def mark_complete(self, id: int, session: AsyncSession) -> Task | None:
        """Mark task as completed."""
        pass
```

### 3.3 Service Layer

#### 3.3.1 Task Service

```python
# src/services/task_service.py
class TaskService:
    """Service layer for task business logic."""

    async def create_task(
        self,
        data: TaskCreate,
        session: AsyncSession
    ) -> Task:
        """
        Create a new task with validation.

        Args:
            data: Task creation data
            session: Database session

        Returns:
            Created task

        Raises:
            ValueError: If validation fails
        """
        pass

    async def get_tasks(
        self,
        session: AsyncSession,
        filters: TaskFilters
    ) -> list[Task]:
        """Retrieve tasks with filters."""
        pass

    async def get_task_by_id(self, task_id: int, session: AsyncSession) -> Task:
        """
        Get single task by ID.

        Raises:
            TaskNotFoundError: If task doesn't exist
        """
        pass

    async def update_task(
        self,
        task_id: int,
        data: TaskUpdate,
        session: AsyncSession
    ) -> Task:
        """Update task with validation."""
        pass

    async def delete_task(self, task_id: int, session: AsyncSession) -> None:
        """Delete task."""
        pass

    async def mark_task_complete(
        self,
        task_id: int,
        session: AsyncSession
    ) -> Task:
        """Mark task as completed."""
        pass
```

### 3.4 API Layer (Schemas)

#### 3.4.1 Request Schemas

```python
# src/api/v1/schemas/task.py
class TaskBase(BaseModel):
    """Base task fields."""
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    priority: PriorityEnum = Field(default=PriorityEnum.MEDIUM)
    due_date: datetime | None = None

class TaskCreate(TaskBase):
    """Schema for creating a task."""
    pass

class TaskUpdate(BaseModel):
    """Schema for updating a task (all fields optional)."""
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    priority: PriorityEnum | None = None
    due_date: datetime | None = None
    completed: bool | None = None

class TaskResponse(TaskBase):
    """Schema for task response."""
    id: int
    completed: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

#### 3.4.2 Route Handlers

```python
# src/api/v1/routes/tasks.py
router = APIRouter()

@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    task: TaskCreate,
    session: AsyncSession = Depends(get_async_session)
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
    session: AsyncSession = Depends(get_async_session)
) -> list[TaskResponse]:
    """List tasks with filters."""
    pass

@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_async_session)
) -> TaskResponse:
    """Get single task by ID."""
    pass

@router.put("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    task: TaskUpdate,
    session: AsyncSession = Depends(get_async_session)
) -> TaskResponse:
    """Update a task."""
    pass

@router.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
async def mark_task_complete(
    task_id: int,
    session: AsyncSession = Depends(get_async_session)
) -> TaskResponse:
    """Mark task as complete."""
    pass

@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_async_session)
) -> None:
    """Delete a task."""
    pass
```

---

## 4. DATA FLOWS

### 4.1 Create Task Flow

```
Client sends POST /api/v1/tasks
    {"title": "Buy milk", "priority": "HIGH"}
          ↓
FastAPI validates with TaskCreate schema
          ↓
get_async_session() provides DB session
          ↓
create_task() handler calls TaskService.create_task()
          ↓
Service validates business rules (title not empty, etc.)
          ↓
Service calls TaskRepository.create()
          ↓
Repository creates Task model instance
Repository adds to session and commits
          ↓
Task returned with auto-generated ID and timestamps
          ↓
Service returns Task to handler
          ↓
FastAPI converts Task to TaskResponse schema
          ↓
JSON response returned with HTTP 201
```

### 4.2 Query with Filters Flow

```
Client sends GET /api/v1/tasks?completed=false&priority=HIGH&limit=50
          ↓
FastAPI validates query parameters
          ↓
list_tasks() handler extracts filters
          ↓
Service.get_tasks() receives filters
          ↓
Repository.get_all() builds SQL query:
  - WHERE completed = false
  - AND priority = 'HIGH'
  - LIMIT 50
          ↓
Database executes query, returns rows
          ↓
Repository converts rows to Task models
          ↓
Service returns list to handler
          ↓
FastAPI converts to list[TaskResponse]
          ↓
JSON array returned with HTTP 200
```

### 4.3 Error Handling Flow

```
Client sends GET /api/v1/tasks/999 (non-existent)
          ↓
Handler calls Service.get_task_by_id(999)
          ↓
Repository.get_by_id(999) returns None
          ↓
Service raises TaskNotFoundError(task_id=999)
          ↓
Custom exception handler catches TaskNotFoundError
          ↓
Returns {"detail": "Task with ID 999 not found"} with HTTP 404
```

---

## 5. DATABASE SCHEMA

### 5.1 Tasks Table

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Auto-increment ID
    title VARCHAR(200) NOT NULL,           -- Required task title
    description VARCHAR(2000),             -- Optional description
    completed BOOLEAN NOT NULL DEFAULT 0,  -- Completion status
    priority VARCHAR(10) NOT NULL,         -- LOW, MEDIUM, HIGH
    created_at TIMESTAMP NOT NULL,         -- Creation timestamp
    updated_at TIMESTAMP NOT NULL,         -- Last update timestamp
    due_date TIMESTAMP,                    -- Optional due date

    -- Indexes for query performance
    INDEX idx_completed (completed),
    INDEX idx_priority (priority),
    INDEX idx_created_at (created_at)
);
```

### 5.2 Indexes

- **idx_completed**: Speeds up filtering by completion status
- **idx_priority**: Speeds up filtering by priority level
- **idx_created_at**: Speeds up sorting by creation date

### 5.3 Constraints

- **title**: NOT NULL, max 200 chars
- **completed**: NOT NULL, default False
- **priority**: NOT NULL (enum constraint in application)
- **created_at**: NOT NULL (auto-generated)
- **updated_at**: NOT NULL (auto-updated)

---

## 6. API CONTRACT SPECIFICATION

### 6.1 Create Task

**Endpoint**: `POST /api/v1/tasks`

**Request Body**:
```json
{
  "title": "Buy groceries",
  "description": "Milk, bread, eggs",
  "priority": "MEDIUM",
  "due_date": "2025-10-25T18:00:00Z"
}
```

**Response (201 Created)**:
```json
{
  "id": 1,
  "title": "Buy groceries",
  "description": "Milk, bread, eggs",
  "completed": false,
  "priority": "MEDIUM",
  "created_at": "2025-10-23T12:00:00Z",
  "updated_at": "2025-10-23T12:00:00Z",
  "due_date": "2025-10-25T18:00:00Z"
}
```

**Errors**:
- `422 Unprocessable Entity`: Validation error (title too long, invalid priority)

### 6.2 List Tasks

**Endpoint**: `GET /api/v1/tasks`

**Query Parameters**:
- `completed` (bool, optional): Filter by completion status
- `priority` (LOW|MEDIUM|HIGH, optional): Filter by priority
- `search` (string, optional): Search in title/description
- `limit` (int, default: 100, max: 1000): Max results
- `offset` (int, default: 0): Pagination offset

**Example**: `GET /api/v1/tasks?completed=false&priority=HIGH&limit=10`

**Response (200 OK)**:
```json
[
  {
    "id": 1,
    "title": "Urgent task",
    "description": "Complete ASAP",
    "completed": false,
    "priority": "HIGH",
    "created_at": "2025-10-23T10:00:00Z",
    "updated_at": "2025-10-23T10:00:00Z",
    "due_date": null
  }
]
```

### 6.3 Get Single Task

**Endpoint**: `GET /api/v1/tasks/{task_id}`

**Response (200 OK)**: Same as create response

**Errors**:
- `404 Not Found`: Task doesn't exist

### 6.4 Update Task

**Endpoint**: `PUT /api/v1/tasks/{task_id}`

**Request Body** (all fields optional):
```json
{
  "title": "Updated title",
  "completed": true
}
```

**Response (200 OK)**: Updated task

**Errors**:
- `404 Not Found`: Task doesn't exist
- `422 Unprocessable Entity`: Validation error

### 6.5 Mark Complete

**Endpoint**: `PATCH /api/v1/tasks/{task_id}/complete`

**Request Body**: None

**Response (200 OK)**: Task with `completed: true`

**Errors**:
- `404 Not Found`: Task doesn't exist

### 6.6 Delete Task

**Endpoint**: `DELETE /api/v1/tasks/{task_id}`

**Request Body**: None

**Response (204 No Content)**: Empty body

**Errors**:
- `404 Not Found`: Task doesn't exist

### 6.7 Health Check

**Endpoint**: `GET /api/v1/health`

**Response (200 OK)**:
```json
{
  "status": "ok",
  "timestamp": "2025-10-23T12:00:00Z",
  "database": "connected"
}
```

---

## 7. ERROR HANDLING STRATEGY

### 7.1 Exception Hierarchy

```python
# src/api/exceptions.py
class TaskAPIException(Exception):
    """Base exception for task API."""
    pass

class TaskNotFoundError(TaskAPIException):
    """Raised when task doesn't exist."""
    def __init__(self, task_id: int):
        self.task_id = task_id
        pass

class TaskValidationError(TaskAPIException):
    """Raised when task validation fails."""
    pass

class DatabaseError(TaskAPIException):
    """Raised when database operation fails."""
    pass
```

### 7.2 HTTP Status Code Mapping

| Exception | Status Code | Response Format |
|-----------|-------------|-----------------|
| TaskNotFoundError | 404 | `{"detail": "Task with ID {id} not found"}` |
| TaskValidationError | 422 | `{"detail": "Validation error", "errors": [...]}` |
| DatabaseError | 500 | `{"detail": "Internal server error"}` |
| Pydantic ValidationError | 422 | Auto-generated by FastAPI |

### 7.3 Exception Handlers

```python
# src/api/main.py
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
```

### 7.4 Logging Strategy

- **DEBUG**: SQL queries, parameter values
- **INFO**: Request received, response sent
- **WARNING**: Validation errors, not found errors
- **ERROR**: Database errors, unexpected exceptions
- **CRITICAL**: Application startup failures

---

## 8. CONFIGURATION MANAGEMENT

### 8.1 Environment Variables

```env
# .env file (not committed to git)

# Application
APP_ENV=development                    # development | production
LOG_LEVEL=DEBUG                        # DEBUG | INFO | WARNING | ERROR

# Database
DATABASE_URL=sqlite+aiosqlite:///./tmp/tasks.db
# For PostgreSQL: postgresql+asyncpg://user:password@localhost:5432/tasks

# API
API_V1_PREFIX=/api/v1
CORS_ORIGINS=["http://localhost:3000","http://localhost:8000"]

# Server
HOST=0.0.0.0
PORT=8000
RELOAD=True                            # Auto-reload in development
```

### 8.2 Settings Class

```python
# src/config/settings.py (updated)
@dataclass
class Config:
    """Base configuration."""
    DEBUG: bool = False
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./tmp/tasks.db")
    DATABASE_ECHO: bool = False  # Log SQL queries

    # API
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = field(default_factory=lambda: ["*"])

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

@dataclass
class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG: bool = True
    DATABASE_ECHO: bool = True  # Show SQL queries
    RELOAD: bool = True

@dataclass
class ProductionConfig(Config):
    """Production configuration."""
    DEBUG: bool = False
    DATABASE_ECHO: bool = False
    RELOAD: bool = False
```

---

## 9. TESTING STRATEGY

### 9.1 Test Fixtures

```python
# tests/conftest.py
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

### 9.2 Test Coverage Plan

**Repository Layer Tests** (tests/repositories/test_task_repository.py):
- Create task
- Get task by ID (exists and not exists)
- Update task
- Delete task
- List all tasks
- Filter by completed status
- Filter by priority
- Search by text
- Pagination (limit/offset)

**Service Layer Tests** (tests/services/test_task_service.py):
- Create task with valid data
- Create task with invalid data (validation)
- Get task (exists and not exists)
- Update task (partial and full)
- Delete task
- Mark task complete
- Business logic validation

**API Endpoint Tests** (tests/api/v1/test_tasks.py):
- POST /tasks with valid data → 201
- POST /tasks with invalid data → 422
- GET /tasks → 200 with list
- GET /tasks with filters → 200 with filtered list
- GET /tasks/{id} (exists) → 200
- GET /tasks/{id} (not exists) → 404
- PUT /tasks/{id} → 200
- PATCH /tasks/{id}/complete → 200
- DELETE /tasks/{id} → 204
- GET /health → 200

**Integration Tests**:
- Full CRUD cycle (create → read → update → delete)
- Concurrent task creation
- Transaction rollback on error
- Database constraint violations

### 9.3 Mock Strategy

- Mock database in service tests (use in-memory repository)
- Real database in repository tests (use test database)
- Real database in API integration tests
- Mock external dependencies (if any added later)

---

## 10. RISKS & MITIGATIONS

### 10.1 Async Complexity

**Risk**: Async patterns may introduce bugs (race conditions, improper await usage)

**Mitigation**:
- Use type hints to catch async/sync mismatches
- Comprehensive async tests with pytest-asyncio
- Follow FastAPI best practices for async
- Code review focused on async correctness

### 10.2 Database Session Management

**Risk**: Session leaks or improper transaction boundaries could cause issues

**Mitigation**:
- Use FastAPI dependency injection for session lifecycle
- Ensure sessions are closed in finally blocks
- Test transaction rollback on errors
- Monitor connection pool usage

### 10.3 Data Validation

**Risk**: Invalid data could reach database despite validation

**Mitigation**:
- Pydantic validation at API layer
- Additional business logic validation in service layer
- Database constraints as last line of defense
- Comprehensive validation tests

### 10.4 Performance

**Risk**: SQLite may not perform well under load

**Mitigation**:
- Design queries with indexes in mind
- Keep database logic in repository layer for easy swapping
- PostgreSQL support from day one
- Performance testing with realistic data volumes

### 10.5 Test Coverage

**Risk**: Achieving 90%+ coverage may be challenging

**Mitigation**:
- Write tests alongside implementation (not after)
- Use coverage tools to identify gaps
- Focus on critical paths and error cases
- Make testing part of definition of done

---

## 11. ACCEPTANCE CRITERIA

### 11.1 Functional Criteria

- All 7 API endpoints implemented and working
- CRUD operations functional for tasks
- Filtering by completed status works
- Filtering by priority works
- Search in title/description works
- Pagination with limit/offset works
- Health check endpoint responds correctly
- Database persistence across application restarts

### 11.2 Quality Criteria

- 90%+ code coverage achieved
- All tests passing
- Pylint score >= 9.5
- Black, isort, ruff all pass
- Type hints on all functions
- Google-style docstrings on all public APIs
- Pre-commit hooks pass

### 11.3 Performance Criteria

- Response time < 100ms for single task operations
- Response time < 500ms for list operations (100 items)
- Database queries optimized (no N+1 queries)
- Connection pooling configured

### 11.4 Documentation Criteria

- FastAPI auto-docs accessible at /docs
- README updated with API usage instructions
- Environment variable documentation
- Migration instructions documented
- Example requests/responses provided

---

## 12. IMPLEMENTATION NOTES

### 12.1 Key Design Patterns

**Repository Pattern**: Abstracts data access, allows easy database swapping
**Dependency Injection**: FastAPI's Depends() manages request-scoped dependencies
**Service Layer Pattern**: Encapsulates business logic separate from API/data layers
**Active Record**: SQLAlchemy models combine data and behavior

### 12.2 Async Best Practices

- Use `async def` for all I/O operations
- Always `await` async calls
- Use `AsyncSession` for database operations
- Use `httpx.AsyncClient` for HTTP testing
- Avoid blocking operations in async functions

### 12.3 Database Migration Strategy

**Initial Migration**:
1. Create alembic.ini configuration
2. Run `alembic init src/database/migrations`
3. Update env.py with Base metadata
4. Generate initial migration: `alembic revision --autogenerate -m "Initial schema"`
5. Apply migration: `alembic upgrade head`

**Future Migrations**:
1. Modify SQLAlchemy models
2. Generate migration: `alembic revision --autogenerate -m "Description"`
3. Review generated migration
4. Apply: `alembic upgrade head`
5. Rollback if needed: `alembic downgrade -1`

### 12.4 Transaction Boundaries

- Each API request = one database transaction
- Transaction begins when session is created (dependency injection)
- Commits on successful completion
- Rolls back on exception
- Repository operations do not manage transactions (service layer responsibility)

---

## 13. FILE-BY-FILE IMPLEMENTATION GUIDE

See `artifacts/file_map.md` for complete file listing with responsibilities and dependencies.

---

## 14. NEXT STEPS FOR IMPLEMENTATION

1. @agent-senior-coder should read this architecture document thoroughly
2. Review `artifacts/file_map.md` for detailed file specifications
3. Implement files in dependency order:
   - Configuration and database setup first
   - Models and enums
   - Repository layer
   - Service layer
   - Schemas
   - API routes
   - Tests alongside each layer
4. Follow coding standards in PYTHON_STYLE_GUIDE.md
5. Ensure each file starts with path comment
6. Use type hints and docstrings everywhere
7. Run linters after each file
8. Write tests for each component before moving to next

---

## 15. QUESTIONS ANSWERED

### Q1: How will async database sessions be managed with FastAPI's request lifecycle?

**A**: FastAPI's dependency injection system with `async def get_async_session()` will create a new session per request, yield it to the route handler, and automatically close it when the request completes (even on exception). The session is request-scoped.

### Q2: What's the transaction boundary for each operation?

**A**: Each API request is a single transaction. The session dependency begins the transaction, the service layer performs operations, and the session is committed/rolled back when the dependency exits (on return or exception).

### Q3: How will database errors be translated to HTTP responses?

**A**: Custom exception classes (TaskNotFoundError, DatabaseError) will be raised by the service layer. FastAPI exception handlers will catch these and convert to appropriate HTTP responses (404, 500, etc.).

### Q4: What's the caching strategy?

**A**: No caching in initial implementation (YAGNI). Can be added later at service layer if needed.

### Q5: How will concurrent updates be handled?

**A**: SQLAlchemy's default isolation level (READ COMMITTED) prevents dirty reads. The updated_at timestamp provides optimistic locking indicator. For future: can add version column for true optimistic locking.

### Q6: What indexes are needed for query performance?

**A**: Three indexes: idx_completed, idx_priority, idx_created_at for filtering and sorting operations.

### Q7: How will the test database be isolated from development?

**A**: Test fixtures create an in-memory SQLite database (`:memory:`) or temporary file database that is created before tests and destroyed after.

### Q8: What's the migration rollback strategy?

**A**: Alembic supports `alembic downgrade -1` to rollback one migration, or `alembic downgrade <revision>` to rollback to specific version. All migrations should have both upgrade() and downgrade() functions.

### Q9: How will environment-specific config be loaded?

**A**: `python-dotenv` loads .env file at application startup. Config class reads environment variables with sensible defaults. Different .env files for dev/prod (.env.development, .env.production).

### Q10: What logging information is needed for debugging?

**A**: Each layer logs:
- **API**: Incoming request, response status, errors
- **Service**: Business logic decisions, validation failures
- **Repository**: Query execution (DEBUG only), database errors
- **Database**: SQL queries (DEBUG only via DATABASE_ECHO)

---

**End of Architecture Design Document**

This design provides a complete blueprint for implementation. All architectural decisions have been made. The senior-coder agent can now implement this system without making design choices.
