from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import UserCreate


class EmailAlreadyExistsError(Exception):
    pass


def _sqlstate(error: IntegrityError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    user = User(
        email=str(data.email).strip().lower(),
        display_name=data.display_name,
        can_create_tasks=data.can_create_tasks,
        can_work_tasks=data.can_work_tasks,
        bio=data.bio,
    )
    session.add(user)

    try:
        await session.flush()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if _sqlstate(error) == "23505":
            raise EmailAlreadyExistsError from error
        raise
    except Exception:
        await session.rollback()
        raise

    await session.refresh(user)
    return user
