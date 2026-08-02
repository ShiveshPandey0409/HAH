from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.db.session import AsyncSessionFactory
from tests.conftest import TEST_DATABASE_URL


async def seed_task() -> tuple[object, object]:
    async with AsyncSessionFactory() as session:
        creator_id = await session.scalar(
            text(
                """
                INSERT INTO users (
                  email, display_name, can_create_tasks
                ) VALUES (
                  'database-creator@example.com', 'Database Creator', true
                ) RETURNING id
                """
            )
        )
        task_id = await session.scalar(
            text(
                """
                INSERT INTO tasks (
                  creator_id, title, description, total_budget_minor, currency, deadline_at
                ) VALUES (
                  :creator_id, 'Guarded task', 'Guard tests', 5000, 'USD', :deadline_at
                ) RETURNING id
                """
            ),
            {
                "creator_id": creator_id,
                "deadline_at": datetime.now(UTC) + timedelta(days=10),
            },
        )
        await session.commit()
    return creator_id, task_id


async def test_database_rejects_duplicate_proof_requirements() -> None:
    _, task_id = await seed_task()
    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    """
                    INSERT INTO bounties (
                      task_id, platform, action, title, instructions,
                      reward_minor, slots_total, influence_metric, proof_requirements
                    ) VALUES (
                      :task_id, 'reddit', 'post', 'Duplicate proof', 'Guard test',
                      1000, 1, 'followers', '["url", "url"]'
                    )
                    """
                ),
                {"task_id": task_id},
            )


async def test_database_rejects_bounty_after_task_deadline() -> None:
    _, task_id = await seed_task()
    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text(
                    """
                    INSERT INTO bounties (
                      task_id, platform, action, title, instructions, reward_minor,
                      slots_total, influence_metric, proof_requirements, deadline_at
                    ) VALUES (
                      :task_id, 'reddit', 'post', 'Late bounty', 'Guard test',
                      1000, 1, 'followers', '["url"]', :deadline_at
                    )
                    """
                ),
                {
                    "task_id": task_id,
                    "deadline_at": datetime.now(UTC) + timedelta(days=11),
                },
            )
        assert caught.value.orig.sqlstate == "HTV04"


async def test_database_rejects_shortened_task_deadline() -> None:
    _, task_id = await seed_task()
    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                INSERT INTO bounties (
                  task_id, platform, action, title, instructions, reward_minor,
                  slots_total, influence_metric, proof_requirements, deadline_at
                ) VALUES (
                  :task_id, 'reddit', 'post', 'Timed bounty', 'Guard test',
                  1000, 1, 'followers', '["url"]', :deadline_at
                )
                """
            ),
            {
                "task_id": task_id,
                "deadline_at": datetime.now(UTC) + timedelta(days=8),
            },
        )
        await session.commit()

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text("UPDATE tasks SET deadline_at = :deadline_at WHERE id = :task_id"),
                {
                    "task_id": task_id,
                    "deadline_at": datetime.now(UTC) + timedelta(days=7),
                },
            )
        assert caught.value.orig.sqlstate == "HTV05"


async def test_database_task_guards_use_stable_application_codes() -> None:
    creator_id, task_id = await seed_task()
    async with AsyncSessionFactory() as session:
        freelancer_id = await session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, can_work_tasks)
                VALUES ('database-worker@example.com', 'Database Worker', true)
                RETURNING id
                """
            )
        )
        with pytest.raises(DBAPIError) as creator_error:
            await session.execute(
                text(
                    """
                    INSERT INTO tasks (
                      creator_id, title, description, total_budget_minor, currency
                    ) VALUES (
                      :creator_id, 'Invalid creator', 'Guard test', 1000, 'USD'
                    )
                    """
                ),
                {"creator_id": freelancer_id},
            )
        assert creator_error.value.orig.sqlstate == "HTV01"
        await session.rollback()

        await session.execute(
            text(
                """
                INSERT INTO bounties (
                  task_id, platform, action, title, instructions, reward_minor,
                  slots_total, influence_metric, proof_requirements
                ) VALUES (
                  :task_id, 'reddit', 'post', 'Allocated bounty', 'Guard test',
                  4000, 1, 'followers', '["url"]'
                )
                """
            ),
            {"task_id": task_id},
        )
        await session.commit()

        with pytest.raises(DBAPIError) as budget_error:
            await session.execute(
                text(
                    "UPDATE tasks SET total_budget_minor = 3000 "
                    "WHERE id = :task_id AND creator_id = :creator_id"
                ),
                {"task_id": task_id, "creator_id": creator_id},
            )
        assert budget_error.value.orig.sqlstate == "HTV03"


async def test_concurrent_bounty_inserts_cannot_exceed_task_budget() -> None:
    _, task_id = await seed_task()
    database_url = make_url(TEST_DATABASE_URL).set(drivername="postgresql")
    dsn = database_url.render_as_string(hide_password=False)
    barrier = asyncio.Barrier(2)

    async def insert_bounty(title: str) -> bool:
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await barrier.wait()
                await connection.execute(
                    """
                    INSERT INTO bounties (
                      task_id, platform, action, title, instructions, reward_minor,
                      slots_total, influence_metric, proof_requirements
                    ) VALUES (
                      $1, 'reddit', 'post', $2, 'Concurrency test',
                      4000, 1, 'followers', '["url"]'
                    )
                    """,
                    task_id,
                    title,
                )
            return True
        except asyncpg.PostgresError as error:
            assert error.sqlstate == "HTV02"
            assert "bounties allocate" in str(error)
            assert "task budget" in str(error)
            return False
        finally:
            await connection.close()

    results = await asyncio.gather(insert_bounty("First"), insert_bounty("Second"))
    assert sorted(results) == [False, True]

    connection = await asyncpg.connect(dsn)
    try:
        row = await connection.fetchrow(
            """
            SELECT count(*) AS bounty_count,
                   coalesce(sum(reward_minor * slots_total), 0) AS allocated
              FROM bounties
             WHERE task_id = $1
            """,
            task_id,
        )
    finally:
        await connection.close()

    assert row["bounty_count"] == 1
    assert row["allocated"] <= 5_000
