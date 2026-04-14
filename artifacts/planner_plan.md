# TODO List REST API - Implementation Plan

**Project**: REST API for Task Management with Database Persistence
**Date**: 2025-10-23
**Location**: /Users/sergeychernyakov/www/blank_python_project

---

## Executive Summary

This plan outlines the complete implementation of a REST API for a to-do list application with database persistence. The implementation will follow the project's coding standards, use FastAPI for the REST API framework, SQLAlchemy for ORM, and SQLite for development (with PostgreSQL support for production).

---

## Technical Stack

### Core Technologies
- **Web Framework**: FastAPI (async support, automatic OpenAPI docs, Pydantic validation)
- **ORM**: SQLAlchemy 2.0 (modern async support)
- **Database**: SQLite (development), PostgreSQL support (production)
- **Validation**: Pydantic models (built into FastAPI)
- **Testing**: pytest, pytest-asyncio, httpx (for async client testing)
- **Migration**: Alembic (database migrations)

### Additional Dependencies
- `fastapi` - Web framework
- `uvicorn[standard]` - ASGI server
- `sqlalchemy[asyncio]` - ORM with async support
- `aiosqlite` - Async SQLite driver
- `asyncpg` - Async PostgreSQL driver (optional)
- `alembic` - Database migrations
- `python-dotenv` - Environment variable management
- `httpx` - HTTP client for testing
- `pytest-asyncio` - Async testing support
- `pytest-cov` - Coverage reporting

---

## API Specification

### Data Model: Task

```python
{
    "id": int,                    # Auto-generated primary key
    "title": str,                 # Required, max 200 chars
    "description": str | None,    # Optional, max 2000 chars
    "completed": bool,            # Default: False
    "priority": str,              # Enum: LOW, MEDIUM, HIGH
    "created_at": datetime,       # Auto-generated
    "updated_at": datetime,       # Auto-updated
    "due_date": datetime | None   # Optional
}
```

### REST Endpoints

| Method | Endpoint | Description | Request Body | Response |
|--------|----------|-------------|--------------|----------|
| GET | `/api/v1/tasks` | List all tasks (with filters) | - | List[Task] |
| GET | `/api/v1/tasks/{id}` | Get single task | - | Task |
| POST | `/api/v1/tasks` | Create new task | TaskCreate | Task |
| PUT | `/api/v1/tasks/{id}` | Update task | TaskUpdate | Task |
| PATCH | `/api/v1/tasks/{id}/complete` | Mark as complete | - | Task |
| DELETE | `/api/v1/tasks/{id}` | Delete task | - | 204 No Content |
| GET | `/api/v1/health` | Health check | - | {"status": "ok"} |

### Query Parameters for GET /tasks
- `completed`: bool - Filter by completion status
- `priority`: str - Filter by priority level
- `search`: str - Search in title/description
- `limit`: int - Pagination limit (default: 100, max: 1000)
- `offset`: int - Pagination offset (default: 0)

---

## Project Structure

```
src/
├── api/
│   ├── __init__.py
│   ├── dependencies.py         # Dependency injection (DB sessions)
│   ├── v1/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── tasks.py       # Task CRUD endpoints
│   │   │   └── health.py      # Health check endpoint
│   │   └── schemas/
│   │       ├── __init__.py
│   │       ├── task.py        # Pydantic schemas
│   │       └── common.py      # Common schemas (pagination, etc.)
│   └── main.py                # FastAPI app initialization
├── models/
│   ├── __init__.py
│   ├── base.py                # Updated base with SQLAlchemy Base
│   ├── enums.py               # Priority enum
│   └── task.py                # Task SQLAlchemy model
├── repositories/
│   ├── __init__.py
│   ├── base.py                # Base repository pattern
│   └── task_repository.py    # Task data access layer
├── services/
│   ├── __init__.py
│   └── task_service.py        # Business logic layer
├── database/
│   ├── __init__.py
│   ├── session.py             # Database session management
│   └── migrations/            # Alembic migrations
│       ├── env.py
│       └── versions/
└── config/
    ├── __init__.py
    └── settings.py            # Updated with DB settings

tests/
├── api/
│   ├── __init__.py
│   ├── conftest.py            # API test fixtures
│   └── v1/
│       ├── __init__.py
│       └── test_tasks.py      # Task endpoint tests
├── repositories/
│   ├── __init__.py
│   └── test_task_repository.py
├── services/
│   ├── __init__.py
│   └── test_task_service.py
└── conftest.py                # Global test fixtures (DB, client)
```

---

## Implementation Phases

### Phase 1: Architecture Design
**Agent**: @agent-code-architect
**Duration**: Design phase
**Deliverables**:
- Complete system architecture document
- Component interaction diagrams
- Database schema design
- API contract specifications
- Error handling strategy
- File-by-file implementation guide

**Key Decisions**:
- FastAPI for REST API (async, auto-docs, Pydantic validation)
- SQLAlchemy 2.0 with async support
- Repository pattern for data access
- Service layer for business logic
- Alembic for database migrations
- SQLite for development (easy setup, no external dependencies)

---

### Phase 2: Core Implementation - Foundation
**Agent**: @agent-senior-coder
**Duration**: Implementation phase
**Deliverables**:
1. Update dependencies in requirements.txt
2. Database configuration and session management
3. SQLAlchemy models (Task model with relationships)
4. Alembic migration setup and initial migration
5. Repository layer implementation
6. Service layer implementation
7. Pydantic schemas for request/response validation

**Files to Create/Modify**:
- `requirements.txt` - Add FastAPI, SQLAlchemy, etc.
- `src/database/session.py` - DB session management
- `src/models/task.py` - Task SQLAlchemy model
- `src/models/enums.py` - Update with Priority enum
- `src/repositories/base.py` - Base repository
- `src/repositories/task_repository.py` - Task repository
- `src/services/task_service.py` - Task business logic
- `src/api/v1/schemas/task.py` - Pydantic schemas
- `src/config/settings.py` - Update with DB config
- `alembic.ini` - Alembic configuration
- `src/database/migrations/env.py` - Alembic environment

---

### Phase 3: API Endpoints Implementation
**Agent**: @agent-senior-coder
**Duration**: Implementation phase
**Deliverables**:
1. FastAPI application setup
2. Dependency injection for DB sessions
3. Task CRUD endpoints implementation
4. Health check endpoint
5. Error handlers and middleware
6. CORS configuration
7. API versioning structure

**Files to Create**:
- `src/api/main.py` - FastAPI app initialization
- `src/api/dependencies.py` - Dependency injection
- `src/api/v1/routes/tasks.py` - Task endpoints
- `src/api/v1/routes/health.py` - Health endpoint
- `src/api/v1/schemas/common.py` - Common schemas

**Features**:
- GET /api/v1/tasks - List with filters and pagination
- GET /api/v1/tasks/{id} - Get single task
- POST /api/v1/tasks - Create task
- PUT /api/v1/tasks/{id} - Update task
- PATCH /api/v1/tasks/{id}/complete - Mark complete
- DELETE /api/v1/tasks/{id} - Delete task
- GET /api/v1/health - Health check

---

### Phase 4: Testing Implementation
**Agent**: @agent-senior-coder
**Duration**: Testing phase
**Deliverables**:
1. Test fixtures and configuration
2. Repository layer tests
3. Service layer tests
4. API endpoint tests (all CRUD operations)
5. Integration tests with database
6. Edge case and error handling tests
7. Test coverage report (target: 90%+)

**Test Files to Create**:
- `tests/conftest.py` - Global fixtures (test DB, async client)
- `tests/api/conftest.py` - API test fixtures
- `tests/api/v1/test_tasks.py` - Task endpoint tests
- `tests/repositories/test_task_repository.py` - Repository tests
- `tests/services/test_task_service.py` - Service tests

**Test Coverage Areas**:
- Happy path CRUD operations
- Validation errors (invalid data)
- 404 errors (non-existent tasks)
- Query parameter filtering
- Pagination
- Concurrent operations
- Database transaction rollback

---

### Phase 5: Code Review
**Agent**: @agent-code-reviewer
**Duration**: Review phase
**Deliverables**:
- Comprehensive code review report
- Security vulnerability assessment
- Code quality evaluation
- Best practices compliance check
- Actionable feedback with severity levels

**Review Focus Areas**:
- Code correctness and logic
- Security (SQL injection, input validation)
- Error handling completeness
- Async/await usage patterns
- Database transaction management
- Type hints and documentation
- Adherence to PYTHON_STYLE_GUIDE.md
- Test coverage and quality

---

### Phase 6: Quality Assurance
**Agent**: @agent-qa-engineer
**Duration**: QA phase
**Deliverables**:
- Test execution report
- Coverage analysis (must be >= 90%)
- Static analysis results (ruff, mypy, pylint)
- Performance baseline tests
- Quality gate status report

**Quality Gates**:
1. All tests pass
2. Code coverage >= 90%
3. Pylint score >= 9.5
4. No critical issues from ruff
5. Type checking passes (mypy)
6. All linters pass (black, isort)
7. Pre-commit hooks pass

---

### Phase 7: Documentation & Final Report
**Agent**: @agent-planner
**Duration**: Documentation phase
**Deliverables**:
1. Update README.md with API usage instructions
2. API documentation (auto-generated by FastAPI)
3. Database setup instructions
4. Environment variable documentation
5. Running instructions (uvicorn server)
6. Final implementation report

**Documentation Includes**:
- Quick start guide
- API endpoint documentation
- Example requests/responses (curl/httpx)
- Database migration commands
- Configuration options
- Troubleshooting guide

---

## Quality Standards

### Code Quality Requirements
- Type hints on all functions/methods
- Google-style docstrings for all public APIs
- First line of each file: file path comment
- snake_case for files/functions, CamelCase for classes
- Logging instead of print()
- No hardcoded secrets (use environment variables)

### Testing Requirements
- pytest with AAA pattern
- 90%+ code coverage
- Tests mirror source structure
- Mock external dependencies
- Test fixtures for reusable setups

### Linting Requirements
- black (formatting)
- isort (import sorting)
- ruff (fast linting)
- pylint (score >= 9.5)
- Max line length: 100 chars

---

## Environment Configuration

### Required Environment Variables
```env
# Application
APP_ENV=development
LOG_LEVEL=DEBUG

# Database
DATABASE_URL=sqlite+aiosqlite:///./tmp/tasks.db
# For PostgreSQL: postgresql+asyncpg://user:password@localhost:5432/tasks_db

# API
API_V1_PREFIX=/api/v1
CORS_ORIGINS=["http://localhost:3000"]
```

---

## Risk Mitigation

### Potential Risks
1. **Async complexity**: FastAPI async patterns may be unfamiliar
   - Mitigation: Use clear examples, test async code thoroughly

2. **Database migrations**: Schema changes could break existing data
   - Mitigation: Use Alembic for versioned migrations

3. **Test coverage**: Achieving 90%+ coverage may be challenging
   - Mitigation: Write tests during implementation, not after

4. **Performance**: SQLite may have limitations
   - Mitigation: Design with PostgreSQL compatibility from start

---

## Success Criteria

1. All API endpoints functional and tested
2. Database persistence working correctly
3. 90%+ test coverage achieved
4. All quality gates passing
5. Documentation complete and accurate
6. Pre-commit hooks passing
7. Application runs without errors
8. FastAPI auto-docs accessible at /docs

---

## Timeline Estimate

- Phase 1 (Architecture): ~1 agent session
- Phase 2 (Core Implementation): ~1 agent session
- Phase 3 (API Endpoints): ~1 agent session
- Phase 4 (Testing): ~1 agent session
- Phase 5 (Code Review): ~1 agent session
- Phase 6 (QA): ~1 agent session
- Phase 7 (Documentation): ~1 agent session

**Total**: 7 sequential agent sessions

---

## Next Steps

1. Launch @agent-code-architect for detailed system design
2. Create handoff document with requirements
3. Wait for architecture completion
4. Launch @agent-senior-coder with architecture reference
5. Monitor progress through todo list
6. Coordinate subsequent phases based on results

---

## Notes

- This is a FEATURE implementation (not a bugfix)
- Expected scope: ~200 lines of production code (within FEATURE limit)
- Database file will be stored in tmp/ directory (gitignored)
- FastAPI provides automatic OpenAPI docs at /docs endpoint
- All async operations for scalability
- Repository pattern allows easy database swapping
