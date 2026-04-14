# src/api/v1/routes/tasks.py

"""
Task CRUD endpoints.

This module provides all REST API endpoints for task management:
create, read, update, delete, and mark complete operations.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session, get_task_service
from src.api.v1.schemas.task import TaskCreate, TaskResponse, TaskUpdate
from src.helpers.logger import get_logger
from src.models.enums import PriorityEnum
from src.services.task_service import TaskService

router = APIRouter()
logger = get_logger(__name__)


@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate, service: TaskService = Depends(get_task_service), session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """
    Create a new task.

    Args:
        task: Task creation data
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        Created task with ID and timestamps

    Raises:
        HTTPException: 422 if validation fails

    Example:
        POST /api/v1/tasks
        Body: {"title": "Buy groceries", "priority": "HIGH"}
        Response: 201 with task object
    """
    logger.info("POST /tasks - Creating task: %s", task.title)

    created_task = await service.create_task(
        session=session, title=task.title, description=task.description, priority=task.priority, due_date=task.due_date
    )

    logger.info("Task created with ID: %d", created_task.id)
    return TaskResponse.model_validate(created_task)


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(
    completed: Optional[bool] = Query(None, description="Filter by completion status"),
    priority: Optional[PriorityEnum] = Query(None, description="Filter by priority level"),
    search: Optional[str] = Query(None, description="Search in title/description"),
    limit: int = Query(default=100, le=1000, ge=1, description="Maximum results"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session),
) -> list[TaskResponse]:
    """
    List tasks with optional filters and pagination.

    Args:
        completed: Filter by completion status (None = no filter)
        priority: Filter by priority level (None = no filter)
        search: Search text in title/description
        limit: Maximum results (max 1000)
        offset: Skip first N results
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        List of tasks matching criteria

    Example:
        GET /api/v1/tasks?completed=false&priority=HIGH&limit=10
        Response: 200 with array of tasks
    """
    logger.info(
        "GET /tasks - Listing tasks (completed=%s, priority=%s, search=%s, limit=%d, offset=%d)",
        completed,
        priority,
        search,
        limit,
        offset,
    )

    tasks = await service.get_tasks(
        session=session, completed=completed, priority=priority, search=search, limit=limit, offset=offset
    )

    logger.info("Retrieved %d tasks", len(tasks))
    return [TaskResponse.model_validate(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int, service: TaskService = Depends(get_task_service), session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """
    Get a single task by ID.

    Args:
        task_id: Task ID
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        Task object

    Raises:
        HTTPException: 404 if task not found

    Example:
        GET /api/v1/tasks/1
        Response: 200 with task object
    """
    logger.info("GET /tasks/%d - Retrieving task", task_id)

    task = await service.get_task_by_id(session=session, task_id=task_id)

    return TaskResponse.model_validate(task)


@router.put("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    task: TaskUpdate,
    service: TaskService = Depends(get_task_service),
    session: AsyncSession = Depends(get_db_session),
) -> TaskResponse:
    """
    Update a task.

    Partial update - only provided fields are updated.

    Args:
        task_id: Task ID
        task: Task update data
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        Updated task object

    Raises:
        HTTPException: 404 if task not found, 422 if validation fails

    Example:
        PUT /api/v1/tasks/1
        Body: {"title": "Updated title", "completed": true}
        Response: 200 with updated task
    """
    logger.info("PUT /tasks/%d - Updating task", task_id)

    updated_task = await service.update_task(
        session=session,
        task_id=task_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        completed=task.completed,
        due_date=task.due_date,
    )

    logger.info("Task %d updated", task_id)
    return TaskResponse.model_validate(updated_task)


@router.patch("/tasks/{task_id}/complete", response_model=TaskResponse)
async def mark_task_complete(
    task_id: int, service: TaskService = Depends(get_task_service), session: AsyncSession = Depends(get_db_session)
) -> TaskResponse:
    """
    Mark a task as complete.

    Args:
        task_id: Task ID
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        Updated task with completed=true

    Raises:
        HTTPException: 404 if task not found

    Example:
        PATCH /api/v1/tasks/1/complete
        Response: 200 with updated task
    """
    logger.info("PATCH /tasks/%d/complete - Marking task complete", task_id)

    task = await service.mark_task_complete(session=session, task_id=task_id)

    logger.info("Task %d marked complete", task_id)
    return TaskResponse.model_validate(task)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int, service: TaskService = Depends(get_task_service), session: AsyncSession = Depends(get_db_session)
) -> None:
    """
    Delete a task.

    Args:
        task_id: Task ID
        service: Task service instance (injected)
        session: Database session (injected)

    Returns:
        No content (204)

    Raises:
        HTTPException: 404 if task not found

    Example:
        DELETE /api/v1/tasks/1
        Response: 204 No Content
    """
    logger.info("DELETE /tasks/%d - Deleting task", task_id)

    await service.delete_task(session=session, task_id=task_id)

    logger.info("Task %d deleted", task_id)
