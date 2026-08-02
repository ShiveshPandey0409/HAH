from __future__ import annotations

from uuid import UUID

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.marketplace import (
    BountyClaimCreate,
    BountyClaimResponse,
    EligibleBountyResponse,
)


class MarketplaceNotFoundError(Exception):
    pass


class MarketplaceConflictError(Exception):
    pass


class MarketplaceValidationError(Exception):
    pass


def _sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


def _constraint_name(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "constraint_name", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)


def _translate_database_error(error: DBAPIError) -> Exception | None:
    sqlstate = _sqlstate(error)
    if sqlstate == "HNF01":
        return MarketplaceNotFoundError("Resource not found")
    if sqlstate == "HCF01" or (
        sqlstate == "23505"
        and _constraint_name(error) == "bounty_claims_bounty_id_freelancer_id_key"
    ):
        return MarketplaceConflictError("Bounty cannot be claimed")
    if sqlstate == "HVL01":
        return MarketplaceValidationError("Claim is not valid")
    return None


async def get_eligible_bounties(
    session: AsyncSession,
    freelancer_id: UUID,
) -> list[EligibleBountyResponse]:
    freelancer = (
        await session.execute(select(User.id, User.can_work_tasks).where(User.id == freelancer_id))
    ).one_or_none()
    if freelancer is None:
        raise MarketplaceNotFoundError("Freelancer not found")
    if not freelancer.can_work_tasks:
        raise MarketplaceValidationError("Freelancer is not allowed to work tasks")

    statement = text(
        """
        SELECT bounty_id,
               task_id,
               task_title,
               task_description,
               bounty_title,
               instructions,
               platform,
               action,
               reward_minor,
               currency,
               effective_deadline,
               proof_requirements,
               remaining_slots,
               social_account_id
          FROM get_eligible_bounties(:freelancer_id)
         ORDER BY effective_deadline ASC NULLS LAST, bounty_id ASC
        """
    ).bindparams(bindparam("freelancer_id", type_=PG_UUID(as_uuid=True)))

    try:
        rows = (await session.execute(statement, {"freelancer_id": freelancer_id})).mappings()
    except DBAPIError as error:
        await session.rollback()
        translated = _translate_database_error(error)
        if translated is not None:
            raise translated from error
        raise

    return [EligibleBountyResponse.model_validate(dict(row)) for row in rows]


async def claim_bounty(
    session: AsyncSession,
    bounty_id: UUID,
    data: BountyClaimCreate,
) -> BountyClaimResponse:
    statement = text(
        """
        SELECT id,
               bounty_id,
               freelancer_id,
               social_account_id,
               platform,
               status,
               reward_minor,
               currency,
               claimed_at,
               claim_expires_at,
               updated_at
          FROM claim_bounty(:bounty_id, :freelancer_id, :social_account_id)
        """
    ).bindparams(
        bindparam("bounty_id", type_=PG_UUID(as_uuid=True)),
        bindparam("freelancer_id", type_=PG_UUID(as_uuid=True)),
        bindparam("social_account_id", type_=PG_UUID(as_uuid=True)),
    )

    try:
        row = (
            (
                await session.execute(
                    statement,
                    {
                        "bounty_id": bounty_id,
                        "freelancer_id": data.freelancer_id,
                        "social_account_id": data.social_account_id,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["id"] is None:
            raise MarketplaceNotFoundError("Bounty not found")
        response = BountyClaimResponse.model_validate(dict(row))
        await session.commit()
        return response
    except DBAPIError as error:
        await session.rollback()
        translated = _translate_database_error(error)
        if translated is not None:
            raise translated from error
        raise
    except Exception:
        await session.rollback()
        raise
