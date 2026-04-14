# Handoff Document: REST API Implementation

**To**: @agent-senior-coder
**From**: @agent-code-architect
**Task**: Implement TODO List REST API
**Date**: 2025-10-23
**Project**: /Users/sergeychernyakov/www/blank_python_project

---

## Task Summary

Implement a production-ready REST API for task management (TODO list) with database persistence. All architectural decisions have been made. Your job is to implement the design exactly as specified.

---

## Architecture Documents

**MUST READ** before starting implementation:

1. **Architecture Design**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/architecture_design.md`
   - Complete system architecture
   - Component specifications with interfaces
   - Data flows and error handling
   - API contract specification
   - All design decisions explained

2. **File Map**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/file_map.md`
   - Every file to create/modify
   - Function signatures and classes
   - Dependencies and implementation notes
   - Implementation order

3. **Project Plan**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/planner_plan.md`
   - Overall project context
   - Technical stack
   - Quality standards

---

## Implementation Scope

**What to Build**:
- REST API with 7 endpoints (CRUD + health check)
- FastAPI framework with async/await
- SQLAlchemy 2.0 ORM with async support
- SQLite database (PostgreSQL compatible)
- Repository pattern for data access
- Service layer for business logic
- Pydantic schemas for validation
- Comprehensive test suite (90%+ coverage)
- Alembic database migrations

**Files to Create**: 30 new files
**Files to Update**: 4 existing files
**Estimated LOC**: ~2000 lines (within FEATURE scope)

---

## Implementation Order

Follow this exact order to satisfy dependencies:

### Phase 1: Foundation (Configuration & Database)

1. Update `requirements.txt` - Add FastAPI, SQLAlchemy, etc.
2. Update `src/config/settings.py` - Add DB and API config
3. Update `.env` - Add environment variables
4. Update `src/models/enums.py` - Add PriorityEnum
5. Create `src/models/database_base.py` - SQLAlchemy Base
6. Create `src/models/task.py` - Task model
7. Create `src/database/__init__.py`
8. Create `src/database/session.py` - Session management
9. Setup Alembic - Create alembic.ini and migrations/env.py
10. Run initial migration - `alembic revision --autogenerate -m "Initial schema"`
11. Apply migration - `alembic upgrade head`

### Phase 2: Repository Layer

12. Create `src/repositories/__init__.py`
13. Create `src/repositories/base.py` - Base repository
14. Create `src/repositories/task_repository.py` - Task repository
15. Create `tests/repositories/__init__.py`
16. Create `tests/repositories/test_task_repository.py` - Repository tests
17. Run repository tests - `pytest tests/repositories/ -v`

### Phase 3: Service Layer

18. Create `src/api/exceptions.py` - Custom exceptions
19. Create `src/services/__init__.py`
20. Create `src/services/task_service.py` - Task service
21. Create `tests/services/__init__.py`
22. Create `tests/services/test_task_service.py` - Service tests
23. Run service tests - `pytest tests/services/ -v`

### Phase 4: API Layer

24. Create `src/api/__init__.py`
25. Create `src/api/v1/__init__.py`
26. Create `src/api/v1/schemas/__init__.py`
27. Create `src/api/v1/schemas/task.py` - Pydantic schemas
28. Create `src/api/v1/schemas/common.py` - Common schemas
29. Create `src/api/dependencies.py` - Dependency injection
30. Create `src/api/v1/routes/__init__.py`
31. Create `src/api/v1/routes/health.py` - Health check
32. Create `src/api/v1/routes/tasks.py` - Task endpoints
33. Create `src/api/main.py` - FastAPI app

### Phase 5: Testing & Entry Point

34. Update `tests/conftest.py` - Add async fixtures
35. Create `tests/api/__init__.py`
36. Create `tests/api/v1/__init__.py`
37. Create `tests/api/v1/test_tasks.py` - API tests
38. Create `run_api.py` - Entry point
39. Run all tests - `pytest -v --cov=src --cov-report=term-missing`
40. Run linters - `black src tests && isort src tests && ruff check src tests && pylint src tests`

---

## Critical Requirements

### Coding Standards (from PYTHON_STYLE_GUIDE.md)

- **First line of EVERY file**: `# path/to/file.py`
- **File names**: snake_case (e.g., `task_service.py`)
- **Class names**: CamelCase (e.g., `TaskService`)
- **Type hints**: On ALL functions, methods, parameters, return values
- **Docstrings**: Google-style on ALL public APIs
- **Logging**: Use `logging` module, NEVER `print()`
- **No hardcoded secrets**: Use environment variables
- **Line length**: Max 100 characters
- **Test structure**: Mirror source structure in tests/

### Quality Gates (from CLAUDE.md)

- All tests pass (`pytest -v`)
- Code coverage >= 90% (`pytest --cov`)
- black passes (`black --check src tests`)
- isort passes (`isort --check src tests`)
- ruff passes (`ruff check src tests`)
- pylint score >= 9.5 (`pylint src tests`)
- Pre-commit hooks pass

### Async Best Practices

- Use `async def` for all I/O operations
- Always `await` async calls
- Use `AsyncSession` for database
- Use `httpx.AsyncClient` for HTTP testing
- No blocking operations in async functions

---

## API Endpoints Specification

### 1. Create Task
- **Endpoint**: `POST /api/v1/tasks`
- **Request**: `{"title": "...", "description": "...", "priority": "MEDIUM", "due_date": "..."}`
- **Response**: `201 Created` with task object
- **Errors**: `422 Unprocessable Entity` for validation errors

### 2. List Tasks
- **Endpoint**: `GET /api/v1/tasks?completed=false&priority=HIGH&limit=100&offset=0`
- **Response**: `200 OK` with array of tasks
- **Query Params**: completed, priority, search, limit (max 1000), offset

### 3. Get Single Task
- **Endpoint**: `GET /api/v1/tasks/{id}`
- **Response**: `200 OK` with task object
- **Errors**: `404 Not Found` if task doesn't exist

### 4. Update Task
- **Endpoint**: `PUT /api/v1/tasks/{id}`
- **Request**: `{"title": "...", "completed": true}` (partial update)
- **Response**: `200 OK` with updated task
- **Errors**: `404 Not Found`, `422 Unprocessable Entity`

### 5. Mark Complete
- **Endpoint**: `PATCH /api/v1/tasks/{id}/complete`
- **Response**: `200 OK` with updated task
- **Errors**: `404 Not Found`

### 6. Delete Task
- **Endpoint**: `DELETE /api/v1/tasks/{id}`
- **Response**: `204 No Content`
- **Errors**: `404 Not Found`

### 7. Health Check
- **Endpoint**: `GET /api/v1/health`
- **Response**: `200 OK` with `{"status": "ok", "timestamp": "...", "database": "connected"}`

---

## Database Schema

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title VARCHAR(200) NOT NULL,
    description VARCHAR(2000),
    completed BOOLEAN NOT NULL DEFAULT 0,
    priority VARCHAR(10) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    due_date TIMESTAMP,

    INDEX idx_completed (completed),
    INDEX idx_priority (priority),
    INDEX idx_created_at (created_at)
);
```

---

## Example Code Patterns

### Example 1: File Header
```python
# src/services/task_service.py

"""
Task service for business logic.

This module provides the service layer for task operations,
including validation and orchestration.
"""
```

### Example 2: Repository Method
```python
async def get_by_id(
    self,
    session: AsyncSession,
    id: int
) -> Task | None:
    """
    Retrieve task by ID.

    Args:
        session: Database session
        id: Task ID

    Returns:
        Task instance or None if not found
    """
    stmt = select(Task).where(Task.id == id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
```

### Example 3: Service Method
```python
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
    logger.info("Creating task with title: %s", title)

    # Validation
    if not title or len(title) > 200:
        raise ValueError("Title must be 1-200 characters")

    # Repository call
    task = await self.repository.create(
        session=session,
        title=title,
        description=description,
        priority=priority,
        due_date=due_date
    )

    logger.info("Task created with ID: %d", task.id)
    return task
```

### Example 4: API Route
```python
@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    task: TaskCreate,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """
    Create a new task.

    Args:
        task: Task creation data
        service: Task service instance
        session: Database session

    Returns:
        Created task

    Raises:
        HTTPException: 422 if validation fails
    """
    logger.info("POST /tasks - Creating task")

    created_task = await service.create_task(
        session=session,
        title=task.title,
        description=task.description,
        priority=task.priority,
        due_date=task.due_date
    )

    return TaskResponse.model_validate(created_task)
```

### Example 5: Test Function
```python
async def test_create_task(async_session: AsyncSession) -> None:
    """Test creating a task."""
    # Arrange
    repository = TaskRepository()

    # Act
    task = await repository.create(
        session=async_session,
        title="Test task",
        priority=PriorityEnum.HIGH
    )

    # Assert
    assert task.id is not None
    assert task.title == "Test task"
    assert task.priority == PriorityEnum.HIGH
    assert task.completed is False
    assert task.created_at is not None
```

---

## Testing Strategy

### Repository Tests
- Test CRUD operations (create, read, update, delete)
- Test filtering (by completed, priority, search)
- Test pagination (limit, offset)
- Test edge cases (not found, validation)

### Service Tests
- Test business logic validation
- Test exception handling (TaskNotFoundError)
- Test data transformation
- Mock repository layer

### API Tests
- Test all endpoints (happy paths)
- Test error cases (404, 422)
- Test filtering and pagination
- Use async client with test database

### Coverage Targets
- Overall: >= 90%
- Critical paths: 100%
- Edge cases: Covered
- Error handlers: Covered

---

## Error Handling

### Exception Types
- `TaskNotFoundError`: Task doesn't exist (404)
- `TaskValidationError`: Validation failed (422)
- `DatabaseError`: Database operation failed (500)

### HTTP Status Codes
- `200 OK`: Successful GET, PUT, PATCH
- `201 Created`: Successful POST
- `204 No Content`: Successful DELETE
- `404 Not Found`: Resource doesn't exist
- `422 Unprocessable Entity`: Validation error
- `500 Internal Server Error`: Unexpected error

---

## Environment Variables

Required in `.env`:
```env
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///./tmp/tasks.db
API_V1_PREFIX=/api/v1
CORS_ORIGINS=http://localhost:3000,http://localhost:8000
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=DEBUG
```

---

## Running the Application

### Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Run application
python run_api.py

# Or with uvicorn directly
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

### Testing
```bash
# Run all tests
pytest -v

# Run with coverage
pytest -v --cov=src --cov-report=term-missing

# Run specific test file
pytest tests/api/v1/test_tasks.py -v
```

### Linting
```bash
# Format code
black src tests
isort src tests

# Check linting
ruff check src tests
pylint src tests
```

---

## Common Pitfalls to Avoid

1. **Forgetting `async`/`await`**: All database operations must be async
2. **Not closing sessions**: Use dependency injection properly
3. **Forgetting type hints**: Every function needs complete type hints
4. **Using `print()`**: Always use logging module
5. **Hardcoding values**: Use config and environment variables
6. **Missing docstrings**: All public APIs need Google-style docstrings
7. **Skipping tests**: Write tests alongside implementation
8. **Ignoring linters**: Run linters frequently during development
9. **Copy-paste errors**: Each file needs correct path comment on line 1
10. **Transaction management**: Let dependency injection handle commits/rollbacks

---

## Definition of Done

Implementation is complete when:

- [ ] All 30 new files created
- [ ] All 4 existing files updated
- [ ] First line of every file has path comment
- [ ] All functions have type hints
- [ ] All public APIs have Google-style docstrings
- [ ] All tests pass (`pytest -v`)
- [ ] Coverage >= 90% (`pytest --cov`)
- [ ] black passes (`black --check src tests`)
- [ ] isort passes (`isort --check src tests`)
- [ ] ruff passes (`ruff check src tests`)
- [ ] pylint >= 9.5 (`pylint src tests`)
- [ ] Application runs without errors (`python run_api.py`)
- [ ] API docs accessible at `http://localhost:8000/docs`
- [ ] All 7 endpoints working (test with curl or httpx)
- [ ] Database persists data across restarts
- [ ] Migrations work (`alembic upgrade head`)

---

## Questions & Clarifications

If you encounter ambiguity during implementation:

1. First, check the architecture design document
2. Second, check the file map for detailed specs
3. Third, refer to PYTHON_STYLE_GUIDE.md for coding conventions
4. If still unclear, make a reasonable decision and document it

Do NOT make architectural decisions - all design is complete.

---

## Next Steps

1. Read architecture design document thoroughly
2. Read file map for detailed specifications
3. Start implementation in the order specified
4. Write tests alongside each component
5. Run linters frequently
6. Commit changes incrementally (small, focused commits)
7. Create handoff document when complete

---

## Success Criteria

Your implementation will be considered successful when:

1. All API endpoints functional and tested
2. 90%+ code coverage achieved
3. All quality gates passing
4. Application runs without errors
5. FastAPI docs work at /docs
6. Database persistence working
7. Code follows all project standards

---

## Reference Files

- **Architecture**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/architecture_design.md`
- **File Map**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/file_map.md`
- **Plan**: `/Users/sergeychernyakov/www/blank_python_project/artifacts/planner_plan.md`
- **Style Guide**: `/Users/sergeychernyakov/www/blank_python_project/PYTHON_STYLE_GUIDE.md`
- **Project Rules**: `/Users/sergeychernyakov/www/blank_python_project/CLAUDE.md`
- **README**: `/Users/sergeychernyakov/www/blank_python_project/README.md`

---

**Ready to implement! All design decisions are made. Follow the architecture exactly. Good luck!**
