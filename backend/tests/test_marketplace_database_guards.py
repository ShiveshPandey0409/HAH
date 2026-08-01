from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.db.session import AsyncSessionFactory
from tests.conftest import TEST_DATABASE_URL
from tests.test_marketplace import (
    add_social_account,
    bounty_ids_by_title,
    bounty_payload,
    claim,
    create_open_task,
    create_user,
)


def sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def test_capacity_reactivation_and_reward_snapshot_guards(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="guard-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="guard-first@example.com")
    second_id = await create_user(client, email="guard-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Guarded bounty", reward_minor=2_000, slot_count=2)],
    )
    bounty_id = bounty_ids_by_title(task)["Guarded bounty"]

    first_claim = await claim(client, bounty_id, first_id, first_social)
    second_claim = await claim(client, bounty_id, second_id, second_social)
    assert first_claim.status_code == 201
    assert second_claim.status_code == 201

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as cancellation_error:
            await session.execute(
                text("UPDATE bounties SET status = 'cancelled' WHERE id = :bounty_id"),
                {"bounty_id": bounty_id},
            )
        assert sqlstate(cancellation_error.value) == "23514"
        await session.rollback()

        with pytest.raises(DBAPIError) as reduction_error:
            await session.execute(
                text("UPDATE bounties SET slots_total = 1 WHERE id = :bounty_id"),
                {"bounty_id": bounty_id},
            )
        assert sqlstate(reduction_error.value) == "23514"
        await session.rollback()

        await session.execute(
            text(
                """
                UPDATE bounty_claims
                   SET claimed_at = now() - interval '2 hours',
                       claim_expires_at = now() - interval '1 hour'
                 WHERE id = :claim_id
                """
            ),
            {"claim_id": first_claim.json()["id"]},
        )
        await session.execute(
            text("UPDATE bounties SET slots_total = 1 WHERE id = :bounty_id"),
            {"bounty_id": bounty_id},
        )
        await session.commit()

        with pytest.raises(DBAPIError) as submitted_reactivation_error:
            await session.execute(
                text("UPDATE bounty_claims SET status = 'submitted' WHERE id = :claim_id"),
                {"claim_id": first_claim.json()["id"]},
            )
        assert sqlstate(submitted_reactivation_error.value) == "HCF01"
        await session.rollback()

        with pytest.raises(DBAPIError) as expiry_reactivation_error:
            await session.execute(
                text(
                    """
                    UPDATE bounty_claims
                       SET claim_expires_at = now() + interval '1 hour'
                     WHERE id = :claim_id
                    """
                ),
                {"claim_id": first_claim.json()["id"]},
            )
        assert sqlstate(expiry_reactivation_error.value) == "HCF01"
        await session.rollback()

        await session.execute(
            text(
                """
                UPDATE bounty_claims
                   SET status = 'rejected',
                       claim_expires_at = now() + interval '1 hour'
                 WHERE id = :claim_id
                """
            ),
            {"claim_id": first_claim.json()["id"]},
        )
        await session.commit()

        with pytest.raises(DBAPIError) as reactivation_error:
            await session.execute(
                text("UPDATE bounty_claims SET status = 'claimed' WHERE id = :claim_id"),
                {"claim_id": first_claim.json()["id"]},
            )
        assert sqlstate(reactivation_error.value) == "HCF01"
        await session.rollback()

        with pytest.raises(DBAPIError) as snapshot_error:
            await session.execute(
                text("UPDATE bounty_claims SET reward_minor = 1 WHERE id = :claim_id"),
                {"claim_id": second_claim.json()["id"]},
            )
        assert sqlstate(snapshot_error.value) == "23514"
        await session.rollback()

        state = (
            await session.execute(
                text(
                    """
                    SELECT b.slots_total,
                           count(c.id) FILTER (
                             WHERE c.status NOT IN ('expired', 'cancelled', 'rejected')
                           ) AS active_claims,
                           max(c.reward_minor) AS reward_minor
                      FROM bounties b
                      JOIN bounty_claims c ON c.bounty_id = b.id
                     WHERE b.id = :bounty_id
                     GROUP BY b.id
                    """
                ),
                {"bounty_id": bounty_id},
            )
        ).one()

    assert state.slots_total == 1
    assert state.active_claims == 1
    assert state.reward_minor == 2_000


async def test_slot_reduction_and_claim_reactivation_serialize(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="guard-race-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="guard-race-first@example.com")
    second_id = await create_user(client, email="guard-race-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Guarded race", slot_count=2)],
    )
    bounty_id = bounty_ids_by_title(task)["Guarded race"]
    first_claim = await claim(client, bounty_id, first_id, first_social)
    second_claim = await claim(client, bounty_id, second_id, second_social)
    assert first_claim.status_code == 201
    assert second_claim.status_code == 201

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE bounty_claims SET status = 'rejected' WHERE id = :claim_id"),
            {"claim_id": second_claim.json()["id"]},
        )
        await session.commit()

    dsn = (
        make_url(TEST_DATABASE_URL)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    barrier = asyncio.Barrier(2)

    async def reduce_slots() -> bool:
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await barrier.wait()
                await connection.execute(
                    "UPDATE bounties SET slots_total = 1 WHERE id = $1",
                    bounty_id,
                )
            return True
        except asyncpg.PostgresError as error:
            assert error.sqlstate == "23514"
            return False
        finally:
            await connection.close()

    async def reactivate_claim() -> bool:
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await barrier.wait()
                await connection.execute(
                    "UPDATE bounty_claims SET status = 'claimed' WHERE id = $1",
                    UUID(second_claim.json()["id"]),
                )
            return True
        except asyncpg.PostgresError as error:
            assert error.sqlstate == "HCF01"
            return False
        finally:
            await connection.close()

    results = await asyncio.gather(reduce_slots(), reactivate_claim())
    assert sorted(results) == [False, True]

    async with AsyncSessionFactory() as session:
        state = (
            await session.execute(
                text(
                    """
                    SELECT b.slots_total,
                           count(c.id) FILTER (
                             WHERE hah_claim_occupies_slot(c.status, c.claim_expires_at)
                           ) AS active_claims
                      FROM bounties AS b
                      JOIN bounty_claims AS c ON c.bounty_id = b.id
                     WHERE b.id = :bounty_id
                     GROUP BY b.id
                    """
                ),
                {"bounty_id": bounty_id},
            )
        ).one()

    assert state.active_claims <= state.slots_total


async def test_database_claim_function_serializes_the_final_slot(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="db-race-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="db-race-first@example.com")
    second_id = await create_user(client, email="db-race-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Database final slot", slot_count=1)],
    )
    bounty_id = bounty_ids_by_title(task)["Database final slot"]
    dsn = (
        make_url(TEST_DATABASE_URL)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    barrier = asyncio.Barrier(2)

    async def claim_once(freelancer_id: UUID, social_account_id: UUID) -> bool:
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await barrier.wait()
                row = await connection.fetchrow(
                    "SELECT id FROM claim_bounty($1, $2, $3)",
                    bounty_id,
                    freelancer_id,
                    social_account_id,
                )
                assert row is not None
            return True
        except asyncpg.PostgresError as error:
            assert error.sqlstate == "HCF01"
            return False
        finally:
            await connection.close()

    results = await asyncio.gather(
        claim_once(first_id, first_social),
        claim_once(second_id, second_social),
    )

    assert sorted(results) == [False, True]
    async with AsyncSessionFactory() as session:
        count = await session.scalar(
            text(
                """
                SELECT count(*)
                  FROM bounty_claims
                 WHERE bounty_id = :bounty_id
                   AND hah_claim_occupies_slot(status, claim_expires_at)
                """
            ),
            {"bounty_id": bounty_id},
        )
    assert count == 1
