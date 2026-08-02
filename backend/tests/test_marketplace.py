from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes import marketplace as marketplace_routes
from app.db.session import AsyncSessionFactory


def sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def create_user(
    client: AsyncClient,
    *,
    email: str,
    can_create_tasks: bool = False,
    can_work_tasks: bool = True,
) -> UUID:
    response = await client.post(
        "/v1/users",
        json={
            "email": email,
            "display_name": "Marketplace User",
            "can_create_tasks": can_create_tasks,
            "can_work_tasks": can_work_tasks,
            "bio": None,
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def add_social_account(
    user_id: UUID,
    *,
    platform: str = "reddit",
    follower_count: int | None = 100,
    following_count: int | None = 10,
    reddit_post_karma: int | None = 40,
    reddit_comment_karma: int | None = 60,
    is_verified: bool = True,
) -> UUID:
    if platform == "linkedin":
        reddit_post_karma = None
        reddit_comment_karma = None
        profile_url = f"https://www.linkedin.com/in/test-{uuid4().hex}/"
    else:
        profile_url = f"https://www.reddit.com/user/test-{uuid4().hex}/"

    now = datetime.now(UTC)
    async with AsyncSessionFactory() as session:
        social_account_id = await session.scalar(
            text(
                """
                INSERT INTO social_accounts (
                  user_id,
                  platform,
                  profile_url,
                  follower_count,
                  following_count,
                  reddit_post_karma,
                  reddit_comment_karma,
                  is_verified,
                  verified_at,
                  enrichment_provider,
                  enriched_at,
                  enrichment_data
                ) VALUES (
                  :user_id,
                  :platform,
                  :profile_url,
                  :follower_count,
                  :following_count,
                  :reddit_post_karma,
                  :reddit_comment_karma,
                  :is_verified,
                  :verified_at,
                  'test-provider',
                  :enriched_at,
                  CAST(:enrichment_data AS jsonb)
                )
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "platform": platform,
                "profile_url": profile_url,
                "follower_count": follower_count,
                "following_count": following_count,
                "reddit_post_karma": reddit_post_karma,
                "reddit_comment_karma": reddit_comment_karma,
                "is_verified": is_verified,
                "verified_at": now if is_verified else None,
                "enriched_at": now,
                "enrichment_data": '{"provider_secret":"must-not-leak"}',
            },
        )
        await session.commit()
    assert social_account_id is not None
    return social_account_id


def bounty_payload(
    title: str,
    *,
    platform: str = "reddit",
    action: str = "comment",
    reward_minor: int = 1_000,
    slot_count: int = 1,
    influence_metric: str = "karma",
    min_influence: int = 0,
    max_influence: int | None = None,
    proof_requirements: list[str] | None = None,
    deadline_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "platform": platform,
        "action": action,
        "title": title,
        "instructions": f"Complete {title} exactly as requested.",
        "reward_minor": reward_minor,
        "slot_count": slot_count,
        "influence_metric": influence_metric,
        "min_influence": min_influence,
        "max_influence": max_influence,
        "proof_requirements": proof_requirements or ["url"],
        "deadline_at": deadline_at.isoformat() if deadline_at is not None else None,
    }


async def create_open_task(
    client: AsyncClient,
    creator_id: UUID,
    bounties: list[dict[str, object]],
    *,
    title: str = "Marketplace campaign",
    description: str = "Complete the matching public social work.",
    deadline_at: datetime | None = None,
) -> dict[str, Any]:
    total_budget = sum(
        int(bounty["reward_minor"]) * int(bounty["slot_count"]) for bounty in bounties
    )
    response = await client.post(
        "/v1/tasks",
        json={
            "creator_id": str(creator_id),
            "title": title,
            "description": description,
            "total_budget_minor": total_budget,
            "currency": "USD",
            "deadline_at": deadline_at.isoformat() if deadline_at is not None else None,
            "bounties": bounties,
        },
    )
    assert response.status_code == 201, response.text
    opened = await client.post(f"/v1/tasks/{response.json()['id']}/open")
    assert opened.status_code == 200, opened.text
    return opened.json()


def bounty_ids_by_title(task: dict[str, Any]) -> dict[str, UUID]:
    return {bounty["title"]: UUID(bounty["id"]) for bounty in task["bounties"]}


async def claim(
    client: AsyncClient,
    bounty_id: UUID,
    freelancer_id: UUID,
    social_account_id: UUID,
) -> Response:
    return await client.post(
        f"/v1/bounties/{bounty_id}/claims",
        json={
            "freelancer_id": str(freelancer_id),
            "social_account_id": str(social_account_id),
        },
    )


async def test_feed_returns_deterministic_safe_matching_bounties(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="feed-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="feed-worker@example.com")
    reddit_id = await add_social_account(
        freelancer_id,
        follower_count=10,
        following_count=999_999,
        reddit_post_karma=40,
        reddit_comment_karma=60,
    )
    linkedin_id = await add_social_account(
        freelancer_id,
        platform="linkedin",
        follower_count=250,
        following_count=999_999,
    )

    now = datetime.now(UTC)
    task_deadline = now + timedelta(days=10)
    karma_deadline = now + timedelta(days=3)
    follower_deadline = now + timedelta(days=4)
    await create_open_task(
        client,
        creator_id,
        [
            bounty_payload(
                "Exact karma",
                min_influence=100,
                max_influence=100,
                proof_requirements=["url", "screenshot"],
                deadline_at=karma_deadline,
            ),
            bounty_payload(
                "Exact Reddit followers",
                action="post",
                influence_metric="followers",
                min_influence=10,
                max_influence=10,
                slot_count=2,
                deadline_at=follower_deadline,
            ),
            bounty_payload(
                "Exact LinkedIn followers",
                platform="linkedin",
                action="post",
                influence_metric="followers",
                min_influence=250,
                max_influence=250,
            ),
            bounty_payload(
                "Below minimum",
                influence_metric="followers",
                min_influence=11,
                max_influence=20,
            ),
            bounty_payload(
                "Above maximum",
                influence_metric="followers",
                min_influence=0,
                max_influence=9,
            ),
        ],
        title="Eligible campaign",
        description="Use the task and bounty instructions.",
        deadline_at=task_deadline,
    )

    response = await client.get(f"/v1/freelancers/{freelancer_id}/bounties")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["bounty_title"] for item in body] == [
        "Exact karma",
        "Exact Reddit followers",
        "Exact LinkedIn followers",
    ]
    assert body[0]["task_title"] == "Eligible campaign"
    assert body[0]["task_description"] == "Use the task and bounty instructions."
    assert body[0]["instructions"] == "Complete Exact karma exactly as requested."
    assert body[0]["proof_requirements"] == ["url", "screenshot"]
    assert body[0]["effective_deadline"] == karma_deadline.isoformat().replace("+00:00", "Z")
    assert body[1]["remaining_slots"] == 2
    assert body[2]["effective_deadline"] == task_deadline.isoformat().replace("+00:00", "Z")
    assert body[0]["social_account_id"] == str(reddit_id)
    assert body[1]["social_account_id"] == str(reddit_id)
    assert body[2]["social_account_id"] == str(linkedin_id)
    assert all(item["currency"] == "USD" for item in body)
    assert "provider_secret" not in response.text
    assert "enrichment_data" not in response.text


async def test_feed_distinguishes_missing_and_non_worker_users(client: AsyncClient) -> None:
    non_worker_id = await create_user(
        client,
        email="feed-non-worker@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )

    missing = await client.get(f"/v1/freelancers/{uuid4()}/bounties")
    non_worker = await client.get(f"/v1/freelancers/{non_worker_id}/bounties")

    assert missing.status_code == 404
    assert non_worker.status_code == 422


async def test_feed_excludes_unverified_profile(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="unverified-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="unverified-worker@example.com")
    await add_social_account(freelancer_id, is_verified=False)
    await create_open_task(client, creator_id, [bounty_payload("Verified only")])

    response = await client.get(f"/v1/freelancers/{freelancer_id}/bounties")

    assert response.status_code == 200
    assert response.json() == []


async def test_feed_excludes_closed_expired_full_paused_and_previously_claimed(
    client: AsyncClient,
) -> None:
    creator_id = await create_user(
        client,
        email="excluded-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="excluded-worker@example.com")
    other_id = await create_user(client, email="excluded-other@example.com")
    social_id = await add_social_account(freelancer_id)
    other_social_id = await add_social_account(other_id)

    task = await create_open_task(
        client,
        creator_id,
        [
            bounty_payload("Available"),
            bounty_payload("Closed"),
            bounty_payload("Expired"),
            bounty_payload("Full"),
            bounty_payload("Already claimed", slot_count=2),
        ],
    )
    bounty_ids = bounty_ids_by_title(task)
    paused_task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Paused task bounty")],
        title="Paused campaign",
    )

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE bounties SET status = 'closed' WHERE id = :bounty_id"),
            {"bounty_id": bounty_ids["Closed"]},
        )
        await session.execute(
            text("UPDATE bounties SET deadline_at = :deadline WHERE id = :bounty_id"),
            {
                "bounty_id": bounty_ids["Expired"],
                "deadline": datetime.now(UTC) - timedelta(seconds=1),
            },
        )
        await session.execute(
            text("UPDATE tasks SET status = 'paused' WHERE id = :task_id"),
            {"task_id": paused_task["id"]},
        )
        await session.commit()

    full_claim = await claim(client, bounty_ids["Full"], other_id, other_social_id)
    own_claim = await claim(client, bounty_ids["Already claimed"], freelancer_id, social_id)
    assert full_claim.status_code == 201
    assert own_claim.status_code == 201

    response = await client.get(f"/v1/freelancers/{freelancer_id}/bounties")

    assert response.status_code == 200, response.text
    assert [item["bounty_title"] for item in response.json()] == ["Available"]


async def test_claim_returns_and_persists_fixed_reward_snapshot(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="snapshot-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="snapshot-worker@example.com")
    social_id = await add_social_account(freelancer_id)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Snapshot reward", reward_minor=1_234)],
    )
    bounty_id = bounty_ids_by_title(task)["Snapshot reward"]

    extra_field = await client.post(
        f"/v1/bounties/{bounty_id}/claims",
        json={
            "freelancer_id": str(freelancer_id),
            "social_account_id": str(social_id),
            "unexpected": True,
        },
    )
    assert extra_field.status_code == 422

    response = await claim(client, bounty_id, freelancer_id, social_id)

    assert response.status_code == 201
    body = response.json()
    assert body["bounty_id"] == str(bounty_id)
    assert body["freelancer_id"] == str(freelancer_id)
    assert body["social_account_id"] == str(social_id)
    assert body["platform"] == "reddit"
    assert body["status"] == "claimed"
    assert body["reward_minor"] == 1_234
    assert body["currency"] == "USD"
    assert body["claim_expires_at"] is not None

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as bounty_pricing_error:
            await session.execute(
                text("UPDATE bounties SET reward_minor = 1000 WHERE id = :bounty_id"),
                {"bounty_id": bounty_id},
            )
        assert sqlstate(bounty_pricing_error.value) == "23514"
        await session.rollback()

        with pytest.raises(DBAPIError) as task_currency_error:
            await session.execute(
                text("UPDATE tasks SET currency = 'EUR' WHERE id = :task_id"),
                {"task_id": task["id"]},
            )
        assert sqlstate(task_currency_error.value) == "23514"
        await session.rollback()

        await session.execute(
            text("UPDATE tasks SET currency = currency WHERE id = :task_id"),
            {"task_id": task["id"]},
        )
        await session.commit()

        snapshot = (
            await session.execute(
                text(
                    """
                    SELECT reward_minor, currency
                      FROM bounty_claims
                     WHERE id = :claim_id
                    """
                ),
                {"claim_id": body["id"]},
            )
        ).one()

    assert snapshot.reward_minor == 1_234
    assert snapshot.currency == "USD"

    duplicate = await claim(client, bounty_id, freelancer_id, social_id)
    assert duplicate.status_code == 409


async def test_claim_expiration_is_capped_and_releases_capacity_after_time(
    client: AsyncClient,
) -> None:
    creator_id = await create_user(
        client,
        email="expiry-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="expiry-first@example.com")
    second_id = await create_user(client, email="expiry-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Expiring slot", deadline_at=deadline)],
        deadline_at=deadline,
    )
    bounty_id = bounty_ids_by_title(task)["Expiring slot"]

    first = await claim(client, bounty_id, first_id, first_social)
    assert first.status_code == 201
    assert datetime.fromisoformat(first.json()["claim_expires_at"]) <= deadline

    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                UPDATE bounty_claims
                   SET claimed_at = now() - interval '2 hours',
                       claim_expires_at = now() - interval '1 hour'
                 WHERE id = :claim_id
                """
            ),
            {"claim_id": first.json()["id"]},
        )
        await session.commit()

    feed = await client.get(f"/v1/freelancers/{second_id}/bounties")
    assert feed.status_code == 200
    assert feed.json()[0]["remaining_slots"] == 1

    second = await claim(client, bounty_id, second_id, second_social)
    assert second.status_code == 201
    task_response = await client.get(f"/v1/tasks/{task['id']}")
    assert task_response.json()["bounties"][0]["claim_count"] == 1


async def test_claim_not_found_errors_do_not_create_rows(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="not-found-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="not-found-worker@example.com")
    social_id = await add_social_account(freelancer_id)
    task = await create_open_task(client, creator_id, [bounty_payload("Not found checks")])
    bounty_id = bounty_ids_by_title(task)["Not found checks"]

    missing_bounty = await claim(client, uuid4(), freelancer_id, social_id)
    missing_freelancer = await claim(client, bounty_id, uuid4(), uuid4())
    missing_profile = await claim(client, bounty_id, freelancer_id, uuid4())

    assert missing_bounty.status_code == 404
    assert missing_freelancer.status_code == 404
    assert missing_profile.status_code == 404
    async with AsyncSessionFactory() as session:
        count = await session.scalar(text("SELECT count(*) FROM bounty_claims"))
    assert count == 0


async def test_claim_validation_errors_do_not_create_rows(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="validation-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Karma gate", min_influence=100, max_influence=200)],
    )
    bounty_id = bounty_ids_by_title(task)["Karma gate"]

    wrong_platform_id = await create_user(client, email="wrong-platform@example.com")
    wrong_platform_social = await add_social_account(
        wrong_platform_id,
        platform="linkedin",
    )
    unverified_id = await create_user(client, email="unverified-claim@example.com")
    unverified_social = await add_social_account(unverified_id, is_verified=False)
    low_influence_id = await create_user(client, email="low-influence@example.com")
    low_influence_social = await add_social_account(
        low_influence_id,
        reddit_post_karma=49,
        reddit_comment_karma=50,
    )
    non_worker_id = await create_user(
        client,
        email="claim-non-worker@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    non_worker_social = await add_social_account(non_worker_id)

    responses = [
        await claim(client, bounty_id, wrong_platform_id, wrong_platform_social),
        await claim(client, bounty_id, unverified_id, unverified_social),
        await claim(client, bounty_id, low_influence_id, low_influence_social),
        await claim(client, bounty_id, non_worker_id, non_worker_social),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422, 422]
    async with AsyncSessionFactory() as session:
        count = await session.scalar(text("SELECT count(*) FROM bounty_claims"))
    assert count == 0


async def test_claim_conflicts_for_closed_expired_and_full_bounties(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="conflict-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="conflict-first@example.com")
    second_id = await create_user(client, email="conflict-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    task = await create_open_task(
        client,
        creator_id,
        [
            bounty_payload("Closed claim"),
            bounty_payload("Expired claim"),
            bounty_payload("Full claim"),
        ],
    )
    bounty_ids = bounty_ids_by_title(task)

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE bounties SET status = 'closed' WHERE id = :bounty_id"),
            {"bounty_id": bounty_ids["Closed claim"]},
        )
        await session.execute(
            text("UPDATE bounties SET deadline_at = :deadline WHERE id = :bounty_id"),
            {
                "bounty_id": bounty_ids["Expired claim"],
                "deadline": datetime.now(UTC) - timedelta(seconds=1),
            },
        )
        await session.commit()

    first = await claim(client, bounty_ids["Full claim"], first_id, first_social)
    closed = await claim(client, bounty_ids["Closed claim"], second_id, second_social)
    expired = await claim(client, bounty_ids["Expired claim"], second_id, second_social)
    full = await claim(client, bounty_ids["Full claim"], second_id, second_social)

    assert first.status_code == 201
    assert closed.status_code == 409
    assert expired.status_code == 409
    assert full.status_code == 409


async def test_freelancer_can_claim_different_platform_bounties(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="multi-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(client, email="multi-worker@example.com")
    reddit_id = await add_social_account(freelancer_id)
    linkedin_id = await add_social_account(
        freelancer_id,
        platform="linkedin",
        follower_count=500,
    )
    task = await create_open_task(
        client,
        creator_id,
        [
            bounty_payload("Reddit work"),
            bounty_payload(
                "LinkedIn work",
                platform="linkedin",
                influence_metric="followers",
                min_influence=500,
                max_influence=500,
            ),
        ],
    )
    bounty_ids = bounty_ids_by_title(task)

    reddit_claim = await claim(client, bounty_ids["Reddit work"], freelancer_id, reddit_id)
    linkedin_claim = await claim(
        client,
        bounty_ids["LinkedIn work"],
        freelancer_id,
        linkedin_id,
    )

    assert reddit_claim.status_code == 201
    assert linkedin_claim.status_code == 201


async def test_concurrent_final_slot_is_claimed_once(client: AsyncClient) -> None:
    creator_id = await create_user(
        client,
        email="concurrent-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_id = await create_user(client, email="concurrent-first@example.com")
    second_id = await create_user(client, email="concurrent-second@example.com")
    first_social = await add_social_account(first_id)
    second_social = await add_social_account(second_id)
    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Final slot", slot_count=1)],
    )
    bounty_id = bounty_ids_by_title(task)["Final slot"]

    first_response, second_response = await asyncio.gather(
        claim(client, bounty_id, first_id, first_social),
        claim(client, bounty_id, second_id, second_social),
    )

    assert sorted([first_response.status_code, second_response.status_code]) == [201, 409]
    async with AsyncSessionFactory() as session:
        active_count = await session.scalar(
            text(
                """
                SELECT count(*)
                  FROM bounty_claims
                 WHERE bounty_id = :bounty_id
                   AND status NOT IN ('expired', 'cancelled', 'rejected')
                """
            ),
            {"bounty_id": bounty_id},
        )
    assert active_count == 1

    task_response = await client.get(f"/v1/tasks/{task['id']}")
    assert task_response.status_code == 200
    bounty = task_response.json()["bounties"][0]
    assert bounty["claim_count"] == 1
    assert bounty["remaining_slots"] == 0


async def test_openapi_contains_marketplace_endpoints(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert paths["/v1/freelancers/{freelancer_id}/bounties"]["get"]
    claim_operation = paths["/v1/bounties/{bounty_id}/claims"]["post"]
    assert claim_operation["responses"]["201"]
    claim_schema = response.json()["components"]["schemas"]["BountyClaimCreate"]
    assert claim_schema["additionalProperties"] is False


async def test_unexpected_database_error_returns_safe_generic_500(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_feed(*args: object, **kwargs: object) -> object:
        raise DBAPIError(
            "SELECT private_data",
            {"api_key": "database-secret"},
            RuntimeError("database-secret"),
            False,
        )

    monkeypatch.setattr(marketplace_routes, "get_eligible_bounties", fail_feed)

    response = await client.get(f"/v1/freelancers/{uuid4()}/bounties")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "database-secret" not in response.text
