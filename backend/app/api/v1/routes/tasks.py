from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.task import TaskCreate, TaskResponse
from app.services.tasks import (
    CreatorCannotCreateTasksError,
    CreatorNotFoundError,
    TaskNotFoundError,
    TaskStateConflictError,
    TaskValidationError,
    create_task_and_commit,
    get_task,
    open_task,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def _task_http_error(error: Exception) -> HTTPException:
    if isinstance(error, (CreatorNotFoundError, TaskNotFoundError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if isinstance(error, TaskStateConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, CreatorCannotCreateTasksError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Creator is not allowed to create tasks",
        )
    if isinstance(error, TaskValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task_endpoint(data: TaskCreate, session: SessionDependency) -> TaskResponse:
    try:
        return await create_task_and_commit(session, data)
    except (
        CreatorNotFoundError,
        CreatorCannotCreateTasksError,
        TaskValidationError,
    ) as error:
        raise _task_http_error(error) from error


@router.post("/{task_id}/open", response_model=TaskResponse)
async def open_task_endpoint(task_id: UUID, session: SessionDependency) -> TaskResponse:
    try:
        return await open_task(session, task_id)
    except (TaskNotFoundError, TaskStateConflictError, TaskValidationError) as error:
        raise _task_http_error(error) from error


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task_endpoint(task_id: UUID, session: SessionDependency) -> TaskResponse:
    try:
        return await get_task(session, task_id)
    except TaskNotFoundError as error:
        raise _task_http_error(error) from error
