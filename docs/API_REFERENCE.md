# Task Management API Reference

**Version**: 1.0
**Base URL**: `http://localhost:8000/api/v1`
**Documentation**: [Swagger UI](http://localhost:8000/docs) | [ReDoc](http://localhost:8000/redoc)

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Error Responses](#error-responses)
4. [Data Models](#data-models)
5. [Endpoints](#endpoints)
   - [Health Check](#health-check)
   - [Create Task](#create-task)
   - [List Tasks](#list-tasks)
   - [Get Task](#get-task)
   - [Update Task](#update-task)
   - [Mark Task Complete](#mark-task-complete)
   - [Delete Task](#delete-task)

---

## Overview

The Task Management API provides RESTful endpoints for managing tasks with full CRUD operations, filtering, search, and pagination capabilities.

### Base URL

```
Development: http://localhost:8000/api/v1
Production:  https://your-domain.com/api/v1
```

### Content Type

All requests and responses use `application/json` content type.

### Request Headers

```
Content-Type: application/json
```

### Response Format

All successful responses return JSON with appropriate HTTP status codes. Error responses follow a consistent format (see [Error Responses](#error-responses)).

---

## Authentication

**Current Version**: No authentication required.

**Future Versions**: Will support JWT token-based authentication.

---

## Error Responses

### Error Format

All errors follow this format:

```json
{
  "detail": "Error message describing what went wrong"
}
```

### HTTP Status Codes

| Code | Meaning | Description |
|------|---------|-------------|
| 200 | OK | Request succeeded |
| 201 | Created | Resource created successfully |
| 204 | No Content | Resource deleted successfully |
| 400 | Bad Request | Invalid request parameters |
| 404 | Not Found | Resource not found |
| 422 | Unprocessable Entity | Validation error |
| 500 | Internal Server Error | Server error |

### Common Error Responses

**404 Not Found**
```json
{
  "detail": "Task with ID 999 not found"
}
```

**422 Validation Error**
```json
{
  "detail": [
    {
      "loc": ["body", "title"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

---

## Data Models

### Task Object

Represents a task in the system.

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

#### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | integer | Read-only | Unique task identifier (auto-generated) |
| `title` | string | Yes | Task title (1-200 characters) |
| `description` | string | No | Task description (max 2000 characters) |
| `completed` | boolean | Read-only | Task completion status (default: false) |
| `priority` | string | No | Priority level: LOW, MEDIUM, HIGH (default: MEDIUM) |
| `created_at` | datetime | Read-only | Creation timestamp (auto-generated) |
| `updated_at` | datetime | Read-only | Last update timestamp (auto-updated) |
| `due_date` | datetime | No | Due date (must be in future) |

### Priority Enum

Valid priority values:

- `LOW` - Low priority task
- `MEDIUM` - Medium priority task (default)
- `HIGH` - High priority task

### Timestamps

All timestamps use ISO 8601 format with UTC timezone:
```
2025-10-23T12:00:00Z
```

---

## Endpoints

### Health Check

Check API and database connectivity status.

#### Request

```http
GET /api/v1/health
```

#### Response

**Status**: `200 OK`

```json
{
  "status": "ok",
  "timestamp": "2025-10-23T12:00:00Z",
  "database": "connected"
}
```

#### Fields

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | API status ("ok" if healthy) |
| `timestamp` | datetime | Current server time |
| `database` | string | Database status ("connected" if healthy) |

#### Example

```bash
curl http://localhost:8000/api/v1/health
```

---

### Create Task

Create a new task.

#### Request

```http
POST /api/v1/tasks
Content-Type: application/json
```

**Body**:
```json
{
  "title": "Buy groceries",
  "description": "Milk, bread, eggs",
  "priority": "MEDIUM",
  "due_date": "2025-10-25T18:00:00Z"
}
```

#### Request Schema

| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `title` | string | Yes | 1-200 characters, not empty |
| `description` | string | No | Max 2000 characters |
| `priority` | string | No | LOW, MEDIUM, or HIGH |
| `due_date` | datetime | No | Must be in future |

#### Response

**Status**: `201 Created`

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

#### Validation Errors

**Empty Title**:
```json
{
  "detail": "Title cannot be empty"
}
```

**Title Too Long**:
```json
{
  "detail": "Title must be 200 characters or less"
}
```

**Description Too Long**:
```json
{
  "detail": "Description must be 2000 characters or less"
}
```

**Due Date in Past**:
```json
{
  "detail": "Due date must be in the future"
}
```

**Invalid Priority**:
```json
{
  "detail": [
    {
      "loc": ["body", "priority"],
      "msg": "value is not a valid enumeration member; permitted: 'LOW', 'MEDIUM', 'HIGH'",
      "type": "type_error.enum"
    }
  ]
}
```

#### Example

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Buy groceries",
    "description": "Milk, bread, eggs",
    "priority": "MEDIUM",
    "due_date": "2025-10-25T18:00:00Z"
  }'
```

---

### List Tasks

Retrieve a list of tasks with optional filtering, search, and pagination.

#### Request

```http
GET /api/v1/tasks?completed=false&priority=HIGH&search=grocery&limit=10&offset=0
```

#### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `completed` | boolean | No | - | Filter by completion status |
| `priority` | string | No | - | Filter by priority (LOW, MEDIUM, HIGH) |
| `search` | string | No | - | Search in title and description (case-insensitive) |
| `limit` | integer | No | 100 | Max results to return (1-1000) |
| `offset` | integer | No | 0 | Number of results to skip (≥0) |

#### Response

**Status**: `200 OK`

```json
[
  {
    "id": 1,
    "title": "Buy groceries",
    "description": "Milk, bread, eggs",
    "completed": false,
    "priority": "HIGH",
    "created_at": "2025-10-23T12:00:00Z",
    "updated_at": "2025-10-23T12:00:00Z",
    "due_date": "2025-10-25T18:00:00Z"
  },
  {
    "id": 2,
    "title": "Grocery shopping",
    "description": "Weekly shopping",
    "completed": false,
    "priority": "HIGH",
    "created_at": "2025-10-23T11:00:00Z",
    "updated_at": "2025-10-23T11:00:00Z",
    "due_date": null
  }
]
```

**Empty Result**:
```json
[]
```

#### Sorting

Results are sorted by `created_at` in descending order (newest first).

#### Filtering Examples

**Get all incomplete tasks**:
```bash
curl "http://localhost:8000/api/v1/tasks?completed=false"
```

**Get high priority tasks**:
```bash
curl "http://localhost:8000/api/v1/tasks?priority=HIGH"
```

**Search for tasks**:
```bash
curl "http://localhost:8000/api/v1/tasks?search=grocery"
```

**Combined filters**:
```bash
curl "http://localhost:8000/api/v1/tasks?completed=false&priority=HIGH&search=urgent"
```

**Pagination**:
```bash
# Get first 10 tasks
curl "http://localhost:8000/api/v1/tasks?limit=10&offset=0"

# Get next 10 tasks
curl "http://localhost:8000/api/v1/tasks?limit=10&offset=10"
```

#### Validation Errors

**Invalid Limit**:
```json
{
  "detail": [
    {
      "loc": ["query", "limit"],
      "msg": "ensure this value is less than or equal to 1000",
      "type": "value_error.number.not_le"
    }
  ]
}
```

**Invalid Offset**:
```json
{
  "detail": [
    {
      "loc": ["query", "offset"],
      "msg": "ensure this value is greater than or equal to 0",
      "type": "value_error.number.not_ge"
    }
  ]
}
```

---

### Get Task

Retrieve a single task by ID.

#### Request

```http
GET /api/v1/tasks/{task_id}
```

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | integer | Yes | Task ID |

#### Response

**Status**: `200 OK`

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

#### Errors

**Task Not Found** (`404 Not Found`):
```json
{
  "detail": "Task with ID 999 not found"
}
```

**Invalid Task ID** (`422 Unprocessable Entity`):
```json
{
  "detail": [
    {
      "loc": ["path", "task_id"],
      "msg": "value is not a valid integer",
      "type": "type_error.integer"
    }
  ]
}
```

#### Example

```bash
curl http://localhost:8000/api/v1/tasks/1
```

---

### Update Task

Update an existing task. All fields are optional (partial update supported).

#### Request

```http
PUT /api/v1/tasks/{task_id}
Content-Type: application/json
```

**Body** (all fields optional):
```json
{
  "title": "Updated title",
  "description": "Updated description",
  "priority": "HIGH",
  "completed": true,
  "due_date": "2025-10-26T18:00:00Z"
}
```

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | integer | Yes | Task ID |

#### Request Schema

All fields are optional. Only provided fields will be updated.

| Field | Type | Validation |
|-------|------|------------|
| `title` | string | 1-200 characters, not empty |
| `description` | string | Max 2000 characters |
| `priority` | string | LOW, MEDIUM, or HIGH |
| `completed` | boolean | true or false |
| `due_date` | datetime | Must be in future |

#### Response

**Status**: `200 OK`

```json
{
  "id": 1,
  "title": "Updated title",
  "description": "Updated description",
  "completed": true,
  "priority": "HIGH",
  "created_at": "2025-10-23T12:00:00Z",
  "updated_at": "2025-10-23T13:30:00Z",
  "due_date": "2025-10-26T18:00:00Z"
}
```

#### Errors

**Task Not Found** (`404 Not Found`):
```json
{
  "detail": "Task with ID 999 not found"
}
```

**Validation Errors** (same as Create Task)

#### Example

**Update title and mark complete**:
```bash
curl -X PUT http://localhost:8000/api/v1/tasks/1 \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Buy groceries - DONE",
    "completed": true
  }'
```

**Update priority only**:
```bash
curl -X PUT http://localhost:8000/api/v1/tasks/1 \
  -H "Content-Type: application/json" \
  -d '{
    "priority": "HIGH"
  }'
```

**Clear due date**:
```bash
curl -X PUT http://localhost:8000/api/v1/tasks/1 \
  -H "Content-Type: application/json" \
  -d '{
    "due_date": null
  }'
```

---

### Mark Task Complete

Mark a task as complete. Convenience endpoint that sets `completed = true`.

#### Request

```http
PATCH /api/v1/tasks/{task_id}/complete
```

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | integer | Yes | Task ID |

#### Response

**Status**: `200 OK`

```json
{
  "id": 1,
  "title": "Buy groceries",
  "description": "Milk, bread, eggs",
  "completed": true,
  "priority": "MEDIUM",
  "created_at": "2025-10-23T12:00:00Z",
  "updated_at": "2025-10-23T14:00:00Z",
  "due_date": "2025-10-25T18:00:00Z"
}
```

#### Errors

**Task Not Found** (`404 Not Found`):
```json
{
  "detail": "Task with ID 999 not found"
}
```

#### Example

```bash
curl -X PATCH http://localhost:8000/api/v1/tasks/1/complete
```

#### Notes

- This endpoint is idempotent - calling it multiple times has the same effect as calling it once
- Equivalent to `PUT /api/v1/tasks/{task_id}` with `{"completed": true}`
- The `updated_at` timestamp will be updated even if task was already complete

---

### Delete Task

Permanently delete a task.

#### Request

```http
DELETE /api/v1/tasks/{task_id}
```

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | integer | Yes | Task ID |

#### Response

**Status**: `204 No Content`

No response body.

#### Errors

**Task Not Found** (`404 Not Found`):
```json
{
  "detail": "Task with ID 999 not found"
}
```

#### Example

```bash
curl -X DELETE http://localhost:8000/api/v1/tasks/1
```

#### Notes

- This is a hard delete - the task is permanently removed from the database
- Cannot be undone
- Returns 404 if task doesn't exist (not idempotent)

---

## Rate Limiting

**Current Version**: No rate limiting.

**Future Versions**: May implement rate limiting for public APIs.

---

## Pagination

### How Pagination Works

Use `limit` and `offset` parameters to paginate through results:

- `limit` - Maximum number of results per page (1-1000, default: 100)
- `offset` - Number of results to skip (default: 0)

### Example Pagination Flow

**Page 1** (results 0-9):
```bash
curl "http://localhost:8000/api/v1/tasks?limit=10&offset=0"
```

**Page 2** (results 10-19):
```bash
curl "http://localhost:8000/api/v1/tasks?limit=10&offset=10"
```

**Page 3** (results 20-29):
```bash
curl "http://localhost:8000/api/v1/tasks?limit=10&offset=20"
```

### Calculating Total Pages

To determine total number of pages, you need to:

1. Count total matching results
2. Divide by page size
3. Round up

Note: The API doesn't currently return total count in the response. Consider adding a separate count endpoint if needed.

### Best Practices

- Use consistent `limit` values across pages
- Cache results when possible to reduce database load
- Consider using cursor-based pagination for large datasets (future enhancement)

---

## Filtering & Search

### Filter by Completion Status

```bash
# Get incomplete tasks
curl "http://localhost:8000/api/v1/tasks?completed=false"

# Get completed tasks
curl "http://localhost:8000/api/v1/tasks?completed=true"
```

### Filter by Priority

```bash
# Get high priority tasks
curl "http://localhost:8000/api/v1/tasks?priority=HIGH"

# Get medium priority tasks
curl "http://localhost:8000/api/v1/tasks?priority=MEDIUM"

# Get low priority tasks
curl "http://localhost:8000/api/v1/tasks?priority=LOW"
```

### Search in Title and Description

The `search` parameter performs case-insensitive search in both title and description:

```bash
# Search for "grocery"
curl "http://localhost:8000/api/v1/tasks?search=grocery"

# Results include:
# - "Buy GROCERY items"
# - "Weekly grocery shopping"
# - Task with description containing "grocery"
```

### Combine Filters

Filters can be combined (AND logic):

```bash
# High priority incomplete tasks containing "urgent"
curl "http://localhost:8000/api/v1/tasks?completed=false&priority=HIGH&search=urgent"
```

---

## Best Practices

### Error Handling

Always check the HTTP status code and handle errors appropriately:

```javascript
const response = await fetch('/api/v1/tasks', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'New task' })
});

if (!response.ok) {
  const error = await response.json();
  console.error('API Error:', error.detail);
  throw new Error(error.detail);
}

const task = await response.json();
```

### Input Validation

Validate input on the client side before sending to API:

- Title: 1-200 characters, not empty/whitespace
- Description: Max 2000 characters
- Priority: Must be LOW, MEDIUM, or HIGH
- Due date: Must be in future

### Performance

- Use pagination for large result sets
- Filter at API level rather than client side
- Cache frequently accessed data
- Use appropriate page sizes (10-50 for UI, 100-1000 for batch operations)

### Date Handling

Always use ISO 8601 format with UTC timezone:

```javascript
// JavaScript example
const dueDate = new Date('2025-10-25T18:00:00Z');
const isoString = dueDate.toISOString(); // "2025-10-25T18:00:00.000Z"
```

```python
# Python example
from datetime import datetime, UTC

due_date = datetime(2025, 10, 25, 18, 0, 0, tzinfo=UTC)
iso_string = due_date.isoformat()  # "2025-10-25T18:00:00+00:00"
```

---

## Code Examples

### Python (requests)

```python
import requests

BASE_URL = "http://localhost:8000/api/v1"

# Create task
response = requests.post(
    f"{BASE_URL}/tasks",
    json={
        "title": "Buy groceries",
        "priority": "HIGH",
        "due_date": "2025-10-25T18:00:00Z"
    }
)
task = response.json()
print(f"Created task {task['id']}")

# List tasks
response = requests.get(
    f"{BASE_URL}/tasks",
    params={"completed": False, "limit": 10}
)
tasks = response.json()
print(f"Found {len(tasks)} tasks")

# Update task
response = requests.put(
    f"{BASE_URL}/tasks/{task['id']}",
    json={"completed": True}
)
updated_task = response.json()

# Delete task
response = requests.delete(f"{BASE_URL}/tasks/{task['id']}")
print(f"Deleted: {response.status_code == 204}")
```

### JavaScript (fetch)

```javascript
const BASE_URL = 'http://localhost:8000/api/v1';

// Create task
const createTask = async () => {
  const response = await fetch(`${BASE_URL}/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: 'Buy groceries',
      priority: 'HIGH',
      due_date: '2025-10-25T18:00:00Z'
    })
  });
  const task = await response.json();
  console.log('Created task:', task.id);
  return task;
};

// List tasks
const listTasks = async () => {
  const params = new URLSearchParams({
    completed: 'false',
    limit: '10'
  });
  const response = await fetch(`${BASE_URL}/tasks?${params}`);
  const tasks = await response.json();
  console.log('Found tasks:', tasks.length);
  return tasks;
};

// Update task
const updateTask = async (id) => {
  const response = await fetch(`${BASE_URL}/tasks/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ completed: true })
  });
  return await response.json();
};

// Delete task
const deleteTask = async (id) => {
  const response = await fetch(`${BASE_URL}/tasks/${id}`, {
    method: 'DELETE'
  });
  return response.status === 204;
};
```

### cURL Examples

See individual endpoint sections for cURL examples.

---

## OpenAPI Specification

The API provides an OpenAPI (Swagger) specification at:

```
GET /openapi.json
```

You can use this specification to:
- Generate client libraries
- Import into Postman or Insomnia
- Generate documentation
- Validate requests/responses

---

## Interactive Documentation

FastAPI provides interactive API documentation:

- **Swagger UI**: http://localhost:8000/docs
  - Try out API endpoints directly in browser
  - See request/response schemas
  - Test authentication (when added)

- **ReDoc**: http://localhost:8000/redoc
  - Alternative documentation format
  - Better for reading and reference

---

## Changelog

### Version 1.0 (2025-10-23)

**Initial Release**:
- 7 API endpoints (health, CRUD operations)
- Filtering by completion status and priority
- Search in title and description
- Pagination with limit/offset
- Full validation and error handling
- Interactive documentation

---

## Support

For questions, issues, or feature requests:

- **GitHub**: [sergeychernyakov/blank_python_project](https://github.com/sergeychernyakov/blank_python_project)
- **Telegram**: [@AIBotsTech](https://t.me/AIBotsTech)
- **Documentation**: [README.md](../README.md)

---

**API Reference Version**: 1.0
**Last Updated**: 2025-10-23
**Status**: Production Ready
