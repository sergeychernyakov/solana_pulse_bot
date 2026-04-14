# QA Engineer Report

**Project**: REST API Task Management
**Date**: 2025-10-23
**QA Engineer**: qa-engineer agent
**Location**: /Users/sergeychernyakov/www/blank_python_project
**Status**: FAIL - Coverage below 90% target

---

## QA SUMMARY

- All 12 API tests passing successfully
- Test coverage at 66% - BELOW 90% target (CRITICAL)
- Code formatting issues found - 12 files need black formatting (MAJOR)
- Import sorting issues in 2 migration files (MINOR)
- Mypy type checking found 8 errors (MAJOR)
- Pylint passed with score 9.50/10 (meets 9.5 requirement)
- Ruff linter passed with no violations
- Application starts successfully and API endpoints functional
- Deprecation warnings present (datetime.utcnow, FastAPI on_event) (MINOR)
- No hardcoded secrets found
- No print() statements found (using proper logging)
- Code quality standards mostly met (path comments, type hints, docstrings present)

**CRITICAL RISK**: Test coverage at 66% poses significant risk for production deployment. Repository layer (49-54%), service layer (60%), and database session management (30%) have insufficient test coverage.

---

## QUALITY GATES

| Gate                    | Target      | Actual      | Status      |
|-------------------------|-------------|-------------|-------------|
| Tests Passing           | 100%        | 12/12 (100%)| ✓ PASS      |
| Test Coverage           | >= 90%      | 66%         | ✗ FAIL      |
| Mypy Type Errors        | 0           | 8           | ✗ FAIL      |
| Ruff Violations         | 0           | 0           | ✓ PASS      |
| Pylint Score            | >= 9.5      | 9.50/10     | ✓ PASS      |
| Black Formatting        | All pass    | 12 files fail| ✗ FAIL     |
| Isort Import Sorting    | All pass    | 2 files fail| ✗ FAIL      |
| Application Starts      | Success     | Success     | ✓ PASS      |
| API Endpoints           | Functional  | Functional  | ✓ PASS      |
| OpenAPI Docs            | Accessible  | Accessible  | ✓ PASS      |

**Overall Status**: FAIL (3 critical gates failed)

---

## TEST RESULTS

### Test Execution
```bash
============================= test session starts ==============================
platform darwin -- Python 3.12.0, pytest-7.4.3, pluggy-1.5.0
12 tests collected

tests/api/v1/test_tasks.py::test_create_task PASSED                      [  8%]
tests/api/v1/test_tasks.py::test_create_task_validation_error PASSED     [ 16%]
tests/api/v1/test_tasks.py::test_list_tasks PASSED                       [ 25%]
tests/api/v1/test_tasks.py::test_list_tasks_with_filters PASSED          [ 33%]
tests/api/v1/test_tasks.py::test_get_task PASSED                         [ 41%]
tests/api/v1/test_tasks.py::test_get_task_not_found PASSED               [ 50%]
tests/api/v1/test_tasks.py::test_update_task PASSED                      [ 58%]
tests/api/v1/test_tasks.py::test_update_task_not_found PASSED            [ 66%]
tests/api/v1/test_tasks.py::test_mark_complete PASSED                    [ 75%]
tests/api/v1/test_tasks.py::test_delete_task PASSED                      [ 83%]
tests/api/v1/test_tasks.py::test_delete_task_not_found PASSED            [ 91%]
tests/api/v1/test_tasks.py::test_health_check PASSED                     [100%]

======================= 12 passed, 38 warnings in 0.16s ========================
```

**Status**: ✓ All tests passing
**Performance**: All tests complete in < 0.2s (excellent)
**Warnings**: 38 deprecation warnings (see Deprecation Warnings section)

### Coverage Report
```
Name                                  Stmts   Miss  Cover   Missing
-------------------------------------------------------------------
src/api/dependencies.py                   9      2    78%   34-35
src/api/exceptions.py                    14      3    79%   59-61
src/api/main.py                          41     13    68%   95-96, 116-117, 126-129, 135-139, 149-150
src/api/v1/routes/health.py              21      7    67%   48-60
src/api/v1/routes/tasks.py               44     10    77%   60-61, 107-108, 139, 183-184, 215-216, 247
src/api/v1/schemas/common.py             10      0   100%
src/api/v1/schemas/task.py               23      0   100%
src/config/settings.py                   31      1    97%   73
src/database/session.py                  46     32    30%   37-47, 59-69, 89-102, 115-121, 132-136
src/helpers/logger.py                    19      0   100%
src/models/base.py                       42     28    33%   31-38, 42, 59-71, 88-92, 108-112
src/models/database_base.py               3      0   100%
src/models/enums.py                       8      0   100%
src/models/task.py                       18      1    94%   72
src/repositories/base.py                 41     21    49%   60-62, 81-88, 108-118, 136-142
src/repositories/task_repository.py      48     22    54%   81-88, 106-114, 135-161
src/services/task_service.py             81     32    60%   67, 70, 74, 78, 90-91, 112-116, 158, 194, 196, 200, 203, 210, 212, 216, 223-228, 248-252, 275-280
-------------------------------------------------------------------
TOTAL                                   511    173    66%
```

**Status**: ✗ FAIL - 66% coverage (target: 90%)
**Gap**: 24 percentage points below target

---

## COVERAGE ANALYSIS BY LAYER

### Critical Coverage Gaps

#### 1. Database Session Layer (30% coverage) - CRITICAL
**File**: `src/database/session.py`
**Coverage**: 30% (46 statements, 32 missing)
**Missing Lines**: 37-47, 59-69, 89-102, 115-121, 132-136

**Uncovered Functionality**:
- Engine creation and singleton pattern (lines 37-47)
- Session factory creation (lines 59-69)
- Session error handling and rollback logic (lines 89-102)
- Database initialization (lines 115-121)
- Database cleanup and disposal (lines 132-136)

**Risk**: HIGH - Database session management is critical infrastructure. Errors in session handling can cause data corruption, connection leaks, and application crashes.

**Root Cause**: No unit tests for database session module. Only integration tests through API endpoints which don't exercise all code paths.

#### 2. Repository Base Class (49% coverage) - CRITICAL
**File**: `src/repositories/base.py`
**Coverage**: 49% (41 statements, 21 missing)
**Missing Lines**: 60-62, 81-88, 108-118, 136-142

**Uncovered Functionality**:
- ID refresh after creation (line 60-62)
- Debug logging for retrieval (lines 81-88)
- Update logic for setting attributes (lines 108-118)
- Delete operation logging (lines 136-142)

**Risk**: MEDIUM-HIGH - Base repository is used by all data access. Bugs here affect entire application.

**Root Cause**: Tests only cover happy paths through API layer, not direct repository testing.

#### 3. Task Repository (54% coverage) - CRITICAL
**File**: `src/repositories/task_repository.py`
**Coverage**: 54% (48 statements, 22 missing)
**Missing Lines**: 81-88, 106-114, 135-161

**Uncovered Functionality**:
- Debug logging for get_all results (lines 81-88)
- mark_complete logic (lines 106-114)
- get_count method entirely (lines 135-161)

**Risk**: MEDIUM - get_count is not tested at all. Could fail in production silently.

**Root Cause**: No tests for mark_complete repository method or get_count method.

#### 4. Service Layer (60% coverage) - MAJOR
**File**: `src/services/task_service.py`
**Coverage**: 60% (81 statements, 32 missing)
**Missing Lines**: 67, 70, 74, 78, 90-91, 112-116, 158, 194, 196, 200, 203, 210, 212, 216, 223-228, 248-252, 275-280

**Uncovered Functionality**:
- Empty title validation (line 67)
- Title length validation (line 70)
- Description validation (line 74)
- Due date past validation (line 78)
- Error handling for task creation (lines 90-91)
- Database error handling (lines 112-116)
- Various error paths in update and delete operations

**Risk**: HIGH - Validation logic is critical for data integrity. Untested validation means bad data could enter system.

**Root Cause**: Tests only cover happy paths. No edge case or error condition tests.

#### 5. API Layer (67-78% coverage) - MAJOR
**Files**: Multiple API route files
**Coverage**: 67-78% depending on file

**Uncovered Functionality**:
- Error handler exceptions in main.py (lines 95-96, 116-117, 126-129, 135-150)
- Health endpoint error cases (lines 48-60)
- Task endpoint error logging (various lines)

**Risk**: MEDIUM - Error handlers not tested. Production errors may not be handled gracefully.

#### 6. Models Base Class (33% coverage) - MINOR
**File**: `src/models/base.py`
**Coverage**: 33% (42 statements, 28 missing)

**Note**: This is the old base.py from template. Not used by Task API implementation (uses Pydantic schemas instead). Low coverage is acceptable as it's not in critical path.

---

## LINTER AND TYPE CHECKING RESULTS

### Black Formatter - FAIL
```
Oh no! 💥 💔 💥
12 files would be reformatted, 25 files would be left unchanged.
```

**Files needing formatting**:
1. src/api/v1/routes/health.py
2. src/api/main.py
3. src/config/settings.py
4. src/database/migrations/versions/619d7e413fa3_initial_schema.py
5. src/models/task.py
6. src/repositories/base.py
7. src/api/v1/routes/tasks.py
8. src/database/session.py
9. tests/conftest.py
10. src/repositories/task_repository.py
11. tests/api/v1/test_tasks.py
12. src/services/task_service.py

**Impact**: MAJOR - Code formatting inconsistency makes code harder to review and maintain.

**Recommendation**: Run `black src/ tests/ --line-length 120` to auto-format all files.

### Isort Import Sorting - FAIL
```
ERROR: src/database/migrations/env.py - Imports incorrectly sorted/formatted
ERROR: src/database/migrations/versions/619d7e413fa3_initial_schema.py - Imports incorrectly sorted/formatted
```

**Impact**: MINOR - Only affects migration files, not critical application code.

**Recommendation**: Run `isort src/ tests/ --profile black --line-length 120` to fix import sorting.

### Ruff Linter - PASS
```
All checks passed!
```

**Status**: ✓ No violations found

### Mypy Type Checking - FAIL
```
Found 8 errors in 3 files (checked 30 source files)
```

**Errors by File**:

**src/models/base.py** (3 errors):
```
Line 37: Key expression in dictionary comprehension has incompatible type "int | str"; expected type "str"
Line 60: Incompatible return value type (got "None", expected "str")
Line 109: Incompatible return value type (got "None", expected "str")
```
**Note**: This is the old template base.py, not used by Task API. Can be ignored or fixed separately.

**src/repositories/base.py** (2 errors):
```
Line 61: "T" has no attribute "id"
Line 79: "type[T]" has no attribute "id"
```
**Impact**: MAJOR - Type safety issue in base repository. Generic type T needs constraint to ensure it has 'id' attribute.

**Root Cause**: Missing protocol or bound constraint on TypeVar T. Should be:
```python
class HasID(Protocol):
    id: int

T = TypeVar("T", bound=HasID)
```

**src/services/task_service.py** (3 errors):
```
Line 214: Incompatible types in assignment (expression has type "bool", target has type "str")
Line 216: Incompatible types in assignment (expression has type "datetime", target has type "str")
Line 219: Incompatible types in assignment (expression has type "datetime", target has type "str")
```
**Impact**: MAJOR - Type errors in update logic. Dict type annotation is incorrect.

**Root Cause**: Line ~207 likely has:
```python
updates: dict[str, str] = {}  # Wrong - should be dict[str, Any]
```

### Pylint Score - PASS
```
Your code has been rated at 9.50/10
```

**Status**: ✓ PASS (meets 9.5 requirement)

**Issues Found** (all acceptable):
- C0103: 9 instances of SCREAMING_CASE config names (acceptable in Settings class)
- R0902: 1 "too many instance attributes" in Settings (acceptable for config)
- R0913/R0917: 1 "too many arguments" in get_tasks method (acceptable for filter params)
- E1102: 1 "func.count is not callable" false positive (SQLAlchemy)
- W0107: 3 unnecessary pass statements (acceptable in exception classes)
- R0903: 1 "too few public methods" (acceptable for Base class)
- W0621: 1 "redefining name 'app'" (acceptable in factory pattern)
- C0415: 1 import outside toplevel in shutdown (acceptable)
- R0801: Duplicate code between service and routes (minor)

---

## DEPRECATION WARNINGS

### 1. FastAPI on_event Deprecation (3 warnings)
**Location**: src/api/main.py lines 123, 132
**Message**: "on_event is deprecated, use lifespan event handlers instead"

**Impact**: MINOR - Deprecated but still functional in current FastAPI version.

**Recommendation**: Migrate to lifespan context manager:
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    yield
    # Shutdown
    await close_db()

app = FastAPI(lifespan=lifespan)
```

### 2. datetime.utcnow() Deprecation (35 warnings)
**Locations**:
- src/models/task.py (31 warnings from default values)
- src/services/task_service.py line 219
- src/api/v1/routes/health.py line 55

**Message**: "datetime.datetime.utcnow() is deprecated, use datetime.datetime.now(datetime.UTC)"

**Impact**: MINOR - Deprecated in Python 3.12, will be removed in future Python version.

**Recommendation**: Replace all instances:
```python
# Old
datetime.utcnow()

# New
datetime.now(datetime.UTC)
```

---

## FUNCTIONAL TESTING RESULTS

### Application Startup - PASS
```bash
$ python run_api.py
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Started server process
INFO:     Waiting for application startup.
2025-10-23 12:46:19,453 - src.api.main - INFO - Starting Task Management API
2025-10-23 12:46:19,453 - src.api.main - INFO - Environment: development
2025-10-23 12:46:19,453 - src.api.main - INFO - Database: sqlite+aiosqlite:///./tmp/tasks.db
2025-10-23 12:46:19,453 - src.api.main - INFO - API prefix: /api/v1
INFO:     Application startup complete.
```

**Status**: ✓ Application starts successfully
**Startup Time**: < 1 second
**Database**: Successfully connected to SQLite

### Health Endpoint - PASS
```bash
$ curl http://localhost:8000/api/v1/health
{"status":"ok","timestamp":"2025-10-23T10:50:52.635795","database":"connected"}
```

**Status**: ✓ Health check functional
**Response Time**: < 50ms
**Database Check**: Connected successfully

### OpenAPI Documentation - PASS
```bash
$ curl http://localhost:8000/docs
<!DOCTYPE html>
<html>
<head>
<title>Task Management API - Swagger UI</title>
...
```

**Status**: ✓ OpenAPI/Swagger UI accessible
**URL**: http://localhost:8000/docs
**Interactive Docs**: Functional

### Test Performance - PASS
```
============================= slowest 10 durations =============================
0.03s setup    tests/api/v1/test_tasks.py::test_create_task
0.01s call     tests/api/v1/test_tasks.py::test_create_task
0.01s setup    tests/api/v1/test_tasks.py::test_list_tasks_with_filters
0.01s setup    tests/api/v1/test_tasks.py::test_list_tasks
0.01s call     tests/api/v1/test_tasks.py::test_list_tasks_with_filters
0.01s setup    tests/api/v1/test_tasks.py::test_delete_task
```

**Status**: ✓ All tests execute quickly
**Slowest Test**: 0.03s (setup time for first test)
**Total Suite Time**: 0.16s
**Performance**: Excellent - no slow tests detected

---

## CODE QUALITY STANDARDS VERIFICATION

### First Line Path Comments - PASS
✓ All source files have path comment as first line
**Sample**:
- `# src/services/task_service.py`
- `# src/repositories/task_repository.py`
- `# src/api/main.py`

### Type Hints - PASS
✓ All public functions and methods have type hints
**Sample** from task_service.py:
```python
async def create_task(
    self,
    session: AsyncSession,
    title: str,
    description: Optional[str] = None,
    priority: PriorityEnum = PriorityEnum.MEDIUM,
    due_date: Optional[datetime] = None
) -> Task:
```

### Google-Style Docstrings - PASS
✓ All public APIs have comprehensive docstrings
**Sample** from task_service.py:
```python
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
    TaskValidationError: If validation fails
"""
```

### Logging Instead of print() - PASS
✓ No print() statements found in source code
✓ All output uses logging module
**Sample**:
```python
logger.info("Creating task with title: %s", title)
logger.error("Database session rolled back due to error: %s", exc)
logger.debug("Retrieved %d tasks", len(tasks))
```

### No Hardcoded Secrets - PASS
✓ No hardcoded passwords, API keys, or secrets found
✓ All configuration via environment variables in .env file
✓ Database URL in environment: `DATABASE_URL=sqlite+aiosqlite:///./tmp/tasks.db`

### Naming Conventions - PASS
✓ File names: snake_case (task_service.py, task_repository.py)
✓ Class names: CamelCase (TaskService, TaskRepository)
✓ Function names: snake_case (create_task, get_task_by_id)

---

## RECOMMENDATIONS

### CRITICAL (Must Fix Before Merge)

#### 1. Increase Test Coverage to 90%+
**Priority**: CRITICAL
**Current**: 66%
**Target**: 90%+
**Effort**: 4-6 hours

**Required Tests**:

**A. Repository Layer Tests** (tests/repositories/test_task_repository.py):
- test_create_task() - Test repository create method
- test_get_by_id_found() - Test retrieval of existing task
- test_get_by_id_not_found() - Test retrieval of non-existent task
- test_update_task() - Test update method
- test_update_task_not_found() - Test update of non-existent task
- test_delete_task() - Test delete method
- test_delete_task_not_found() - Test delete of non-existent task
- test_get_all_no_filters() - Test listing all tasks
- test_get_all_with_completed_filter() - Test completed filter
- test_get_all_with_priority_filter() - Test priority filter
- test_get_all_with_search() - Test search functionality
- test_get_all_pagination() - Test limit and offset
- test_mark_complete() - Test mark_complete method
- test_mark_complete_not_found() - Test mark_complete on non-existent task
- test_get_count_no_filters() - Test count method
- test_get_count_with_filters() - Test count with filters

**B. Service Layer Tests** (tests/services/test_task_service.py):
- test_create_task_empty_title() - Validate empty title rejection
- test_create_task_title_too_long() - Validate title length limit
- test_create_task_description_too_long() - Validate description length
- test_create_task_due_date_in_past() - Validate future due date requirement
- test_get_task_not_found() - Test TaskNotFoundError exception
- test_update_task_empty_title() - Validate update with empty title
- test_update_task_title_too_long() - Validate update title length
- test_delete_task_not_found() - Test delete non-existent task
- test_mark_task_complete_not_found() - Test complete non-existent task
- test_database_error_handling() - Test DatabaseError exception

**C. Database Session Tests** (tests/database/test_session.py):
- test_get_async_engine_singleton() - Verify engine singleton pattern
- test_session_commit_on_success() - Test successful commit
- test_session_rollback_on_error() - Test rollback on exception
- test_session_cleanup() - Test session close in finally block
- test_init_db() - Test database initialization
- test_close_db() - Test database cleanup

**Assigned To**: @agent-senior-coder (DO NOT modify source code, only add tests)

#### 2. Fix Mypy Type Errors
**Priority**: CRITICAL
**Count**: 8 errors in 3 files
**Effort**: 30 minutes

**Specific Fixes**:

**src/repositories/base.py**:
```python
# Add protocol for type constraint
from typing import Protocol

class HasID(Protocol):
    id: int

T = TypeVar("T", bound=HasID)
```

**src/services/task_service.py** (line ~207):
```python
# Change from:
updates: dict[str, str] = {}

# To:
updates: dict[str, Any] = {}
```

**src/models/base.py** (optional - not used by Task API):
- Fix dictionary comprehension key type
- Fix return type annotations

**Assigned To**: @agent-senior-coder

#### 3. Fix Code Formatting
**Priority**: CRITICAL
**Files**: 12 files
**Effort**: 2 minutes

**Command**:
```bash
black src/ tests/ --line-length 120
isort src/ tests/ --profile black --line-length 120
```

**Assigned To**: @agent-senior-coder

### MAJOR (Should Fix Before Production)

#### 4. Migrate from on_event to Lifespan
**Priority**: MAJOR
**Location**: src/api/main.py
**Effort**: 15 minutes

**Current Code**:
```python
@app.on_event("startup")
async def startup_event():
    await init_db()

@app.on_event("shutdown")
async def shutdown_event():
    await close_db()
```

**Recommended**:
```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()

app = FastAPI(lifespan=lifespan)
```

**Assigned To**: @agent-senior-coder

#### 5. Replace datetime.utcnow() with datetime.now(UTC)
**Priority**: MAJOR
**Locations**: 3 files
**Effort**: 10 minutes

**Files**:
- src/models/task.py
- src/services/task_service.py line 219
- src/api/v1/routes/health.py line 55

**Replace**:
```python
from datetime import datetime, UTC

# Old
datetime.utcnow()

# New
datetime.now(UTC)
```

**Assigned To**: @agent-senior-coder

### MINOR (Can Defer)

#### 6. Add Error Handler Tests
**Priority**: MINOR
**Location**: src/api/main.py
**Effort**: 1 hour

Test exception handlers for:
- TaskNotFoundError → 404
- TaskValidationError → 422
- DatabaseError → 500
- Generic Exception → 500

**Assigned To**: @agent-senior-coder (as part of coverage improvement)

#### 7. Remove Unnecessary Pass Statements
**Priority**: MINOR
**Count**: 3 instances
**Effort**: 2 minutes

**Locations**:
- src/models/database_base.py line 20
- src/api/exceptions.py lines 18, 71
- src/api/v1/schemas/task.py line 38

**Assigned To**: @agent-senior-coder (during code cleanup)

---

## DECISION

**FAIL - Critical issues prevent merge**

### Blocking Issues

1. **Test Coverage: 66% (Target: 90%)**
   - Gap: 24 percentage points
   - Risk: HIGH - Insufficient testing of critical paths
   - Action: Add repository, service, and database session tests

2. **Mypy Type Errors: 8 errors**
   - Risk: MEDIUM-HIGH - Type safety compromised
   - Action: Fix type constraints and annotations

3. **Code Formatting: 12 files**
   - Risk: MEDIUM - Code consistency issues
   - Action: Run black and isort formatters

### Ready to Proceed After

- [ ] Test coverage reaches 90%+
- [ ] All mypy type errors fixed
- [ ] All files formatted with black/isort
- [ ] All 12 tests still passing

### Recommended Workflow

1. **Phase 1** (Senior Coder): Add comprehensive tests
   - Repository layer tests (16 tests)
   - Service layer tests (10 tests)
   - Database session tests (6 tests)
   - Target: Push coverage from 66% to 90%+

2. **Phase 2** (Senior Coder): Fix type errors
   - Add Protocol constraint to BaseRepository
   - Fix updates dict type in TaskService
   - Verify mypy passes with 0 errors

3. **Phase 3** (Senior Coder): Code cleanup
   - Run black formatter
   - Run isort
   - Fix deprecation warnings (lifespan, datetime.now(UTC))

4. **Phase 4** (QA Engineer): Re-validate
   - Run full test suite
   - Verify coverage >= 90%
   - Verify all linters pass
   - Final sign-off

---

## TEST ENVIRONMENT

**Python Version**: 3.12.0
**Pytest Version**: 7.4.3
**Platform**: darwin (macOS)
**Virtual Environment**: /Users/sergeychernyakov/www/blank_python_project/.venv
**Database**: SQLite (sqlite+aiosqlite:///./tmp/tasks.db)
**API Server**: Uvicorn running on http://0.0.0.0:8000

---

## APPENDIX: DETAILED COVERAGE BY FILE

### 100% Coverage (6 files)
- src/api/v1/schemas/common.py
- src/api/v1/schemas/task.py
- src/helpers/logger.py
- src/models/database_base.py
- src/models/enums.py

### 90-99% Coverage (2 files)
- src/config/settings.py - 97%
- src/models/task.py - 94%

### 70-89% Coverage (3 files)
- src/api/exceptions.py - 79%
- src/api/dependencies.py - 78%
- src/api/v1/routes/tasks.py - 77%

### 60-69% Coverage (3 files)
- src/api/main.py - 68%
- src/api/v1/routes/health.py - 67%
- src/services/task_service.py - 60%

### 50-59% Coverage (1 file)
- src/repositories/task_repository.py - 54%

### Below 50% Coverage (3 files) - CRITICAL
- src/repositories/base.py - 49%
- src/models/base.py - 33% (not used by Task API)
- src/database/session.py - 30%

---

**Report Generated**: 2025-10-23
**Total Files Analyzed**: 30 Python files
**Total Tests**: 12 passing
**Total Coverage**: 66% (511 statements, 173 missed)
**Quality Gates**: 5/9 passed, 4/9 failed
**Recommendation**: DO NOT MERGE until coverage reaches 90% and type errors fixed
