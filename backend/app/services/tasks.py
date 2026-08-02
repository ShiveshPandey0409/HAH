from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Bounty, BountyStatus, Task, TaskStatus
from app.models.user import User
from app.schemas.task import BountyResponse, TaskCreate, TaskCreationSource, TaskResponse

CreationSource = TaskCreationSource


class CreatorNotFoundError(Exception):
    pass


class CreatorCannotCreateTasksError(Exception):
    pass


class TaskNotFoundError(Exception):
    pass


class TaskValidationError(Exception):
    pass


class TaskStateConflictError(Exception):
    pass


class TaskOwnershipError(Exception):
    pass


def _sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


def _is_task_rule_violation(error: DBAPIError) -> bool:
    return _sqlstate(error) in {
        "22003",
        "22021",
        "23503",
        "23514",
        "HTV01",
        "HTV02",
        "HTV03",
        "HTV04",
        "HTV05",
    }


def _task_rule_message(error: DBAPIError) -> str:
    return {
        "HTV01": "creator is not allowed to create tasks",
        "HTV02": "bounties exceed the task budget",
        "HTV03": "task budget is lower than its allocated bounties",
        "HTV04": "bounty deadline cannot be after the task deadline",
        "HTV05": "task deadline cannot be before a bounty deadline",
    }.get(_sqlstate(error), "task or bounty violates a database rule")


async def _claim_counts(session: AsyncSession, bounty_ids: list[UUID]) -> dict[UUID, int]:
    if not bounty_ids:
        return {}
    result = await session.execute(
        text(
            """
            SELECT bounty_id, count(*)::integer AS claim_count
             FROM bounty_claims
             WHERE bounty_id = ANY(:bounty_ids)
               AND hah_claim_occupies_slot(status, claim_expires_at)
             GROUP BY bounty_id
            """
        ).bindparams(bindparam("bounty_ids", type_=ARRAY(PG_UUID(as_uuid=True)))),
        {"bounty_ids": bounty_ids},
    )
    return {row.bounty_id: row.claim_count for row in result}


async def _task_response(session: AsyncSession, task: Task) -> TaskResponse:
    bounties = list(
        (
            await session.scalars(
                select(Bounty)
                .where(Bounty.task_id == task.id)
                .order_by(Bounty.created_at, Bounty.id)
            )
        ).all()
    )
    counts = await _claim_counts(session, [bounty.id for bounty in bounties])
    allocated = sum(
        bounty.reward_minor * bounty.slots_total
        for bounty in bounties
        if bounty.status != BountyStatus.CANCELLED
    )
    bounty_responses = [
        BountyResponse(
            id=bounty.id,
            platform=bounty.platform,
            action=bounty.action,
            title=bounty.title,
            instructions=bounty.instructions,
            reward_minor=bounty.reward_minor,
            slot_count=bounty.slots_total,
            influence_metric=bounty.influence_metric,
            min_influence=bounty.min_influence,
            max_influence=bounty.max_influence,
            proof_requirements=bounty.proof_requirements,
            status=bounty.status,
            deadline_at=bounty.deadline_at,
            claim_count=counts.get(bounty.id, 0),
            remaining_slots=max(bounty.slots_total - counts.get(bounty.id, 0), 0),
            created_at=bounty.created_at,
            updated_at=bounty.updated_at,
        )
        for bounty in bounties
    ]
    return TaskResponse(
        id=task.id,
        creator_id=task.creator_id,
        title=task.title,
        description=task.description,
        total_budget_minor=task.total_budget_minor,
        allocated_budget_minor=allocated,
        remaining_budget_minor=task.total_budget_minor - allocated,
        currency=task.currency.strip(),
        status=task.status,
        created_via=task.created_via,
        deadline_at=task.deadline_at,
        bounties=bounty_responses,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def get_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    authorized_creator_id: UUID | None = None,
) -> TaskResponse:
    task = await session.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError
    if authorized_creator_id is not None and task.creator_id != authorized_creator_id:
        raise TaskOwnershipError
    return await _task_response(session, task)


async def list_tasks(session: AsyncSession, creator_id: UUID) -> list[TaskResponse]:
    tasks = list(
        (
            await session.scalars(
                select(Task)
                .where(Task.creator_id == creator_id)
                .order_by(Task.created_at.desc(), Task.id.desc())
            )
        ).all()
    )
    return [await _task_response(session, task) for task in tasks]


async def create_task(
    session: AsyncSession,
    data: TaskCreate,
    *,
    creation_source: CreationSource = "manual",
) -> TaskResponse:
    creator = await session.get(User, data.creator_id)
    if creator is None:
        raise CreatorNotFoundError
    if not creator.can_create_tasks:
        raise CreatorCannotCreateTasksError

    task = Task(
        creator_id=data.creator_id,
        title=data.title,
        description=data.description,
        total_budget_minor=data.total_budget_minor,
        currency=data.currency,
        status=TaskStatus.DRAFT,
        created_via=creation_source,
        deadline_at=data.deadline_at,
    )
    session.add(task)

    try:
        await session.flush()
        for bounty_data in data.bounties:
            session.add(
                Bounty(
                    task_id=task.id,
                    platform=bounty_data.platform,
                    action=bounty_data.action,
                    title=bounty_data.title,
                    instructions=bounty_data.instructions,
                    reward_minor=bounty_data.reward_minor,
                    slots_total=bounty_data.slot_count,
                    influence_metric=bounty_data.influence_metric,
                    min_influence=bounty_data.min_influence,
                    max_influence=bounty_data.max_influence,
                    proof_requirements=bounty_data.proof_requirements,
                    status=BountyStatus.DRAFT,
                    deadline_at=bounty_data.deadline_at,
                )
            )
        await session.flush()
    except DBAPIError as error:
        if _is_task_rule_violation(error):
            raise TaskValidationError(_task_rule_message(error)) from error
        raise

    return await get_task(session, task.id, authorized_creator_id=data.creator_id)


async def create_task_and_commit(
    session: AsyncSession,
    data: TaskCreate,
    *,
    creation_source: CreationSource = "manual",
) -> TaskResponse:
    try:
        response = await create_task(session, data, creation_source=creation_source)
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return response


async def open_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    authorized_creator_id: UUID,
) -> TaskResponse:
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise TaskNotFoundError
    if task.creator_id != authorized_creator_id:
        raise TaskOwnershipError
    if task.status != TaskStatus.DRAFT:
        raise TaskStateConflictError("task cannot be opened from its current state")
    if task.deadline_at is not None and task.deadline_at <= datetime.now(UTC):
        raise TaskStateConflictError("task deadline has passed")

    bounties = list((await session.scalars(select(Bounty).where(Bounty.task_id == task.id))).all())
    openable_bounties = [bounty for bounty in bounties if bounty.status != BountyStatus.CANCELLED]
    if not openable_bounties:
        raise TaskStateConflictError("task must contain at least one bounty")
    if any(bounty.status != BountyStatus.DRAFT for bounty in openable_bounties):
        raise TaskStateConflictError("all non-cancelled bounties must be drafts")
    if any(
        bounty.deadline_at is not None and bounty.deadline_at <= datetime.now(UTC)
        for bounty in openable_bounties
    ):
        raise TaskStateConflictError("a bounty deadline has passed")

    task.status = TaskStatus.OPEN
    for bounty in openable_bounties:
        bounty.status = BountyStatus.OPEN

    task_id = task.id
    try:
        await session.commit()
    except DBAPIError as error:
        await session.rollback()
        if _is_task_rule_violation(error):
            raise TaskValidationError("task could not be opened") from error
        raise

    session.expire_all()
    return await get_task(
        session,
        task_id,
        authorized_creator_id=authorized_creator_id,
    )


async def replace_task(
    session: AsyncSession,
    task_id: UUID,
    data: TaskCreate,
) -> TaskResponse:
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise TaskNotFoundError
    if task.creator_id != data.creator_id:
        raise TaskOwnershipError
    if task.status != TaskStatus.DRAFT:
        raise TaskStateConflictError("only a draft task can be replaced")

    try:
        await session.execute(delete(Bounty).where(Bounty.task_id == task.id))
        await session.flush()
        task.title = data.title
        task.description = data.description
        task.total_budget_minor = data.total_budget_minor
        task.currency = data.currency
        task.deadline_at = data.deadline_at
        await session.flush()
        for bounty_data in data.bounties:
            session.add(
                Bounty(
                    task_id=task.id,
                    platform=bounty_data.platform,
                    action=bounty_data.action,
                    title=bounty_data.title,
                    instructions=bounty_data.instructions,
                    reward_minor=bounty_data.reward_minor,
                    slots_total=bounty_data.slot_count,
                    influence_metric=bounty_data.influence_metric,
                    min_influence=bounty_data.min_influence,
                    max_influence=bounty_data.max_influence,
                    proof_requirements=bounty_data.proof_requirements,
                    status=BountyStatus.DRAFT,
                    deadline_at=bounty_data.deadline_at,
                )
            )
        await session.flush()
        await session.commit()
    except DBAPIError as error:
        await session.rollback()
        if _is_task_rule_violation(error):
            raise TaskValidationError(_task_rule_message(error)) from error
        raise
    except Exception:
        await session.rollback()
        raise

    session.expire_all()
    return await get_task(session, task_id, authorized_creator_id=data.creator_id)


async def delete_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    authorized_creator_id: UUID,
) -> None:
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise TaskNotFoundError
    if task.creator_id != authorized_creator_id:
        raise TaskOwnershipError
    if task.status != TaskStatus.DRAFT:
        raise TaskStateConflictError("only a draft task can be deleted")
    try:
        await session.delete(task)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
