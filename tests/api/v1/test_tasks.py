# tests/api/v1/test_tasks.py

"""
Test task API endpoints.

This module tests all REST API endpoints for task management.
"""

import pytest
from httpx import AsyncClient

from src.models.enums import PriorityEnum
from src.models.task import Task


@pytest.mark.asyncio
async def test_create_task(async_client: AsyncClient) -> None:
    """Test creating a task."""
    response = await async_client.post(
        "/api/v1/tasks", json={"title": "Test Task", "description": "Test Description", "priority": "HIGH"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Task"
    assert data["description"] == "Test Description"
    assert data["priority"] == "HIGH"
    assert data["completed"] is False
    assert "id" in data


@pytest.mark.asyncio
async def test_create_task_validation_error(async_client: AsyncClient) -> None:
    """Test POST with invalid data returns 422."""
    response = await async_client.post("/api/v1/tasks", json={"title": ""})  # Empty title should fail

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_tasks(async_client: AsyncClient, sample_tasks: list[Task]) -> None:
    """Test GET /api/v1/tasks."""
    response = await async_client.get("/api/v1/tasks")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 5


@pytest.mark.asyncio
async def test_list_tasks_with_filters(async_client: AsyncClient, sample_tasks: list[Task]) -> None:
    """Test GET /api/v1/tasks with query params."""
    # Filter by completed
    response = await async_client.get("/api/v1/tasks?completed=false")
    assert response.status_code == 200
    data = response.json()
    assert all(not task["completed"] for task in data)

    # Filter by priority
    response = await async_client.get("/api/v1/tasks?priority=HIGH")
    assert response.status_code == 200
    data = response.json()
    assert all(task["priority"] == "HIGH" for task in data)

    # Search
    response = await async_client.get("/api/v1/tasks?search=groceries")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "groceries" in data[0]["title"].lower()


@pytest.mark.asyncio
async def test_get_task(async_client: AsyncClient, sample_task: Task) -> None:
    """Test GET /api/v1/tasks/{id}."""
    response = await async_client.get(f"/api/v1/tasks/{sample_task.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == sample_task.id
    assert data["title"] == sample_task.title


@pytest.mark.asyncio
async def test_get_task_not_found(async_client: AsyncClient) -> None:
    """Test GET /api/v1/tasks/{id} returns 404."""
    response = await async_client.get("/api/v1/tasks/9999")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_task(async_client: AsyncClient, sample_task: Task) -> None:
    """Test PUT /api/v1/tasks/{id}."""
    response = await async_client.put(
        f"/api/v1/tasks/{sample_task.id}", json={"title": "Updated Title", "completed": True}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Updated Title"
    assert data["completed"] is True


@pytest.mark.asyncio
async def test_update_task_not_found(async_client: AsyncClient) -> None:
    """Test PUT /api/v1/tasks/{id} returns 404."""
    response = await async_client.put("/api/v1/tasks/9999", json={"title": "Updated"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mark_complete(async_client: AsyncClient, sample_task: Task) -> None:
    """Test PATCH /api/v1/tasks/{id}/complete."""
    response = await async_client.patch(f"/api/v1/tasks/{sample_task.id}/complete")

    assert response.status_code == 200
    data = response.json()
    assert data["completed"] is True


@pytest.mark.asyncio
async def test_delete_task(async_client: AsyncClient, sample_task: Task) -> None:
    """Test DELETE /api/v1/tasks/{id}."""
    response = await async_client.delete(f"/api/v1/tasks/{sample_task.id}")

    assert response.status_code == 204

    # Verify task is deleted
    response = await async_client.get(f"/api/v1/tasks/{sample_task.id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_task_not_found(async_client: AsyncClient) -> None:
    """Test DELETE /api/v1/tasks/{id} returns 404."""
    response = await async_client.delete("/api/v1/tasks/9999")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_health_check(async_client: AsyncClient) -> None:
    """Test GET /api/v1/health."""
    response = await async_client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_mark_complete_not_found(async_client: AsyncClient) -> None:
    """Test PATCH /api/v1/tasks/{id}/complete returns 404 for non-existent task."""
    response = await async_client.patch("/api/v1/tasks/9999/complete")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_task_without_optional_fields(async_client: AsyncClient) -> None:
    """Test creating task with only required fields."""
    response = await async_client.post("/api/v1/tasks", json={"title": "Minimal Task"})

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Minimal Task"
    assert data["description"] is None
    assert data["priority"] == "MEDIUM"  # default
    assert data["completed"] is False


@pytest.mark.asyncio
async def test_list_tasks_empty(async_client: AsyncClient) -> None:
    """Test GET /api/v1/tasks with no tasks in database."""
    response = await async_client.get("/api/v1/tasks")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 0


@pytest.mark.asyncio
async def test_update_task_partial(async_client: AsyncClient, sample_task: Task) -> None:
    """Test partial update of task."""
    response = await async_client.put(f"/api/v1/tasks/{sample_task.id}", json={"completed": True})

    assert response.status_code == 200
    data = response.json()
    assert data["completed"] is True
    assert data["title"] == sample_task.title  # Unchanged


@pytest.mark.asyncio
async def test_list_tasks_with_limit_offset(async_client: AsyncClient, sample_tasks: list[Task]) -> None:
    """Test GET /api/v1/tasks with pagination parameters."""
    response = await async_client.get("/api/v1/tasks?limit=2&offset=1")

    assert response.status_code == 200
    data = response.json()
    assert len(data) <= 2


@pytest.mark.asyncio
async def test_create_task_with_all_params(async_client: AsyncClient) -> None:
    """Test creating task with all optional parameters."""
    response = await async_client.post(
        "/api/v1/tasks",
        json={
            "title": "Full Task",
            "description": "Complete description",
            "priority": "HIGH",
            "due_date": "2025-12-31T23:59:59Z",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Full Task"
    assert data["priority"] == "HIGH"
