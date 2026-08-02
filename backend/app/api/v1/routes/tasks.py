from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import AuthenticatedSessionDependency
from app.db.session import get_db_session
from app.schemas.task import HTTPTaskCreateInput, HTTPTaskReplaceInput, TaskCreate, TaskResponse
from app.services.tasks import (
    CreatorCannotCreateTasksError,
    CreatorNotFoundError,
    TaskNotFoundError,
    TaskOwnershipError,
    TaskStateConflictError,
    TaskValidationError,
    create_task_and_commit,
    delete_task,
    get_task,
    list_tasks,
    open_task,
    replace_task,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def _task_http_error(error: Exception) -> HTTPException:
    if isinstance(error, (CreatorNotFoundError, TaskNotFoundError, TaskOwnershipError)):
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
async def create_task_endpoint(
    data: HTTPTaskCreateInput,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> TaskResponse:
    command = TaskCreate(creator_id=authenticated.user.id, **data.model_dump())
    try:
        return await create_task_and_commit(session, command)
    except (
        CreatorNotFoundError,
        CreatorCannotCreateTasksError,
        TaskValidationError,
    ) as error:
        raise _task_http_error(error) from error


@router.post("/{task_id}/open", response_model=TaskResponse)
async def open_task_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> TaskResponse:
    try:
        return await open_task(
            session,
            task_id,
            authorized_creator_id=authenticated.user.id,
        )
    except (
        TaskNotFoundError,
        TaskOwnershipError,
        TaskStateConflictError,
        TaskValidationError,
    ) as error:
        raise _task_http_error(error) from error


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> TaskResponse:
    try:
        return await get_task(
            session,
            task_id,
            authorized_creator_id=authenticated.user.id,
        )
    except (TaskNotFoundError, TaskOwnershipError) as error:
        raise _task_http_error(error) from error


@router.get("", response_model=list[TaskResponse])
async def list_tasks_endpoint(
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> list[TaskResponse]:
    return await list_tasks(session, authenticated.user.id)


@router.put("/{task_id}", response_model=TaskResponse)
async def replace_task_endpoint(
    task_id: UUID,
    data: HTTPTaskReplaceInput,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> TaskResponse:
    command = TaskCreate(creator_id=authenticated.user.id, **data.model_dump())
    try:
        return await replace_task(session, task_id, command)
    except (
        TaskNotFoundError,
        TaskOwnershipError,
        TaskStateConflictError,
        TaskValidationError,
    ) as error:
        raise _task_http_error(error) from error


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_endpoint(
    task_id: UUID,
    session: SessionDependency,
    authenticated: AuthenticatedSessionDependency,
) -> Response:
    try:
        await delete_task(
            session,
            task_id,
            authorized_creator_id=authenticated.user.id,
        )
    except (TaskNotFoundError, TaskOwnershipError, TaskStateConflictError) as error:
        raise _task_http_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
