# Handoff Document: REST API Architecture Design

**Agent**: @agent-code-architect
**Task**: Design architecture for TODO List REST API
**Date**: 2025-10-23
**Project**: /Users/sergeychernyakov/www/blank_python_project

---

## Task Overview

Design a complete system architecture for a REST API that manages a to-do list (task list) with database persistence. The design must be detailed enough for the senior-coder agent to implement without ambiguity.

---

## Requirements

### Functional Requirements

1. **Task Management CRUD Operations**:
   - Create a new task
   - Retrieve all tasks (with filtering and pagination)
   - Retrieve a single task by ID
   - Update an existing task
   - Delete a task
   - Mark a task as complete (PATCH operation)

2. **Task Data Model**:
   - ID (auto-generated)
   - Title (required, max 200 chars)
   - Description (optional, max 2000 chars)
   - Completed status (boolean, default: false)
   - Priority level (enum: LOW, MEDIUM, HIGH)
   - Created timestamp (auto-generated)
   - Updated timestamp (auto-updated)
   - Due date (optional)

3. **API Features**:
   - RESTful design with proper HTTP methods
   - Request/response validation
   - Error handling with appropriate status codes
   - Query parameters for filtering (by completion, priority, search)
   - Pagination support (limit/offset)
   - Health check endpoint
   - API versioning (/api/v1)

4. **Database Requirements**:
   - Persistent storage using SQLAlchemy ORM
   - Async database operations
   - SQLite for development
   - PostgreSQL support for production
   - Database migrations with Alembic
   - Transaction management

### Non-Functional Requirements

1. **Performance**:
   - Async/await pattern throughout
   - Efficient database queries
   - Connection pooling

2. **Code Quality**:
   - Follow PYTHON_STYLE_GUIDE.md
   - Type hints everywhere
   - Google-style docstrings
   - Proper separation of concerns

3. **Testing**:
   - 90%+ code coverage
   - Unit tests for all layers
   - Integration tests for API endpoints

4. **Security**:
   - Input validation using Pydantic
   - SQL injection prevention (using ORM)
   - No hardcoded secrets
   - CORS configuration

---

## Technical Stack (Pre-approved)

- **Web Framework**: FastAPI (async support, auto OpenAPI docs)
- **ORM**: SQLAlchemy 2.0 with async support
- **Database**: SQLite (dev) / PostgreSQL (prod)
- **Validation**: Pydantic (built into FastAPI)
- **Migrations**: Alembic
- **Server**: Uvicorn (ASGI)
- **Testing**: pytest, pytest-asyncio, httpx

---

## Project Constraints

1. **File Naming**: snake_case for files, CamelCase for classes
2. **First Line Rule**: Every file must start with `# path/to/file.py`
3. **No print()**: Use logging module only
4. **No Hardcoded Secrets**: Environment variables only
5. **Linting**: Must pass black, isort, ruff, pylint (>=9.5)
6. **Test Structure**: Mirror source structure in tests/

---

## Current Project Structure

```
src/
├── __init__.py
├── config/
│   ├── __init__.py
│   └── settings.py          # Needs DB config additions
├── helpers/
│   ├── __init__.py
│   └── logger.py            # Already exists
└── models/
    ├── __init__.py
    ├── base.py              # Needs SQLAlchemy Base
    └── enums.py             # Needs Priority enum

tests/
├── __init__.py
└── security/                # Existing tests

main.py                      # Current entry point
requirements.txt             # Needs FastAPI, SQLAlchemy, etc.
```

---

## Architecture Deliverables Required

Your architecture design document must include:

### 1. System Architecture Diagram
- Component interaction flow
- Request/response lifecycle
- Layer separation (API -> Service -> Repository -> Database)

### 2. Directory Structure
- Complete file tree with all new files
- Purpose of each directory
- Module organization

### 3. Component Specifications

#### A. Database Layer
- SQLAlchemy model design
- Database session management
- Migration strategy
- Connection configuration

#### B. Repository Layer
- Base repository pattern
- Task repository interface
- CRUD operations specification
- Query builder patterns

#### C. Service Layer
- Business logic separation
- Task service interface
- Validation rules
- Transaction boundaries

#### D. API Layer
- FastAPI application structure
- Endpoint specifications (path, method, params, body, response)
- Dependency injection design
- Error handling strategy
- Middleware requirements

#### E. Schema Layer (Pydantic)
- Request schemas (TaskCreate, TaskUpdate)
- Response schemas (TaskResponse, TaskList)
- Common schemas (PaginationParams, ErrorResponse)

### 4. Data Flow Diagrams
- Create task flow
- Query with filters flow
- Update task flow
- Error handling flow

### 5. API Contract Specification
- Endpoint definitions with examples
- Request/response schemas
- Status codes
- Error response format

### 6. Database Schema
- Table definitions
- Indexes
- Constraints
- Relationships (if any future expansion)

### 7. Error Handling Strategy
- Exception types
- HTTP status code mapping
- Error response format
- Logging strategy

### 8. Configuration Management
- Environment variables
- Settings structure
- Development vs production config

### 9. Testing Strategy
- Test fixtures design
- Mock strategy
- Coverage plan
- Test database setup

### 10. File-by-File Implementation Guide
For each file to be created/modified:
- File path
- Purpose
- Key classes/functions
- Dependencies
- Implementation notes

---

## Design Principles to Follow

1. **Separation of Concerns**: Clear boundaries between layers
2. **Dependency Injection**: Use FastAPI's dependency system
3. **Repository Pattern**: Abstract data access
4. **Service Layer**: Encapsulate business logic
5. **Single Responsibility**: Each module has one purpose
6. **Open/Closed**: Design for extension
7. **DRY**: Avoid code duplication
8. **Async First**: All I/O operations async

---

## Specific Design Decisions Needed

1. **Session Management**: How to handle async DB sessions with FastAPI
2. **Error Handling**: Custom exception classes vs HTTP exceptions
3. **Validation**: Where to validate (Pydantic vs service layer)
4. **Pagination**: Approach for limit/offset with total count
5. **Filtering**: Query parameter design for complex filters
6. **Testing**: Fixture structure for DB setup/teardown
7. **Migrations**: Initial migration vs future changes
8. **Logging**: What to log at each layer
9. **Response Format**: Envelope vs direct object return
10. **API Versioning**: How to structure v1, prepare for v2

---

## Example Endpoint Specification Format

For each endpoint, provide:

```
POST /api/v1/tasks
Purpose: Create a new task
Request Body:
  {
    "title": "string (required, max 200)",
    "description": "string (optional, max 2000)",
    "priority": "LOW|MEDIUM|HIGH (optional, default: MEDIUM)",
    "due_date": "datetime (optional, ISO 8601)"
  }
Response (201 Created):
  {
    "id": 1,
    "title": "Sample task",
    "description": "Task description",
    "completed": false,
    "priority": "MEDIUM",
    "created_at": "2025-10-23T12:00:00Z",
    "updated_at": "2025-10-23T12:00:00Z",
    "due_date": null
  }
Errors:
  - 400 Bad Request: Invalid input data
  - 422 Unprocessable Entity: Validation error
Dependencies: get_db_session, TaskService
```

---

## Success Criteria

Your architecture design is complete when:

1. All components are specified with clear interfaces
2. Data flows are documented and unambiguous
3. Every file to be created has a specification
4. Database schema is fully defined
5. API contract is complete with examples
6. Error handling covers all scenarios
7. Testing strategy is comprehensive
8. Configuration is environment-aware
9. Security considerations are addressed
10. Implementation guide is actionable

The senior-coder agent should be able to implement the entire system from your design without making architectural decisions.

---

## Output Location

Create your architecture document at:
`/Users/sergeychernyakov/www/blank_python_project/artifacts/architecture_design.md`

---

## Reference Documents

- Project overview: `/Users/sergeychernyakov/www/blank_python_project/README.md`
- Coding standards: `/Users/sergeychernyakov/www/blank_python_project/PYTHON_STYLE_GUIDE.md`
- Agent instructions: `/Users/sergeychernyakov/www/blank_python_project/AGENTS.md`
- Execution plan: `/Users/sergeychernyakov/www/blank_python_project/artifacts/planner_plan.md`

---

## Questions to Answer in Your Design

1. How will async database sessions be managed with FastAPI's request lifecycle?
2. What's the transaction boundary for each operation?
3. How will database errors be translated to HTTP responses?
4. What's the caching strategy (if any)?
5. How will concurrent updates be handled?
6. What indexes are needed for query performance?
7. How will the test database be isolated from development?
8. What's the migration rollback strategy?
9. How will environment-specific config be loaded?
10. What logging information is needed for debugging?

---

## Start Your Design

Please read the reference documents and create a comprehensive architecture design document that addresses all requirements above. The design should be detailed, unambiguous, and ready for implementation.
