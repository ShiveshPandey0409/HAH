from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text

from app.db.session import AsyncSessionFactory
from app.models.task import Bounty, Task
from app.schemas.task import POSTGRES_BIGINT_MAX, POSTGRES_INTEGER_MAX


async def create_user(
    client: AsyncClient,
    *,
    email: str = "creator@example.com",
    can_create_tasks: bool = True,
    can_work_tasks: bool = False,
) -> UUID:
    response = await client.post(
        "/v1/users",
        json={
            "email": email,
            "display_name": "Task User",
            "can_create_tasks": can_create_tasks,
            "can_work_tasks": can_work_tasks,
            "bio": None,
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def bounty_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "platform": "reddit",
        "action": "comment",
        "title": "Comment on a launch thread",
        "instructions": "Write an original and relevant comment.",
        "reward_minor": 1_000,
        "slot_count": 2,
        "influence_metric": "karma",
        "min_influence": 500,
        "max_influence": 5_000,
        "proof_requirements": ["url", "screenshot"],
    }
    payload.update(overrides)
    return payload


def task_payload(creator_id: UUID, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "creator_id": str(creator_id),
        "title": "Launch campaign",
        "description": "Promote the product launch.",
        "total_budget_minor": 5_000,
        "currency": "usd",
        "deadline_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        "bounties": [bounty_payload()],
    }
    payload.update(overrides)
    return payload


async def test_create_read_and_open_task(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    response = await client.post(
        "/v1/tasks",
        json=task_payload(
            creator_id,
            bounties=[
                bounty_payload(),
                bounty_payload(
                    platform="linkedin",
                    action="post",
                    title="LinkedIn launch post",
                    reward_minor=1_500,
                    slot_count=2,
                    influence_metric="followers",
                    proof_requirements=["url"],
                ),
            ],
        ),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["created_via"] == "manual"
    assert body["currency"] == "USD"
    assert body["allocated_budget_minor"] == 5_000
    assert body["remaining_budget_minor"] == 0
    assert len(body["bounties"]) == 2
    assert all(bounty["status"] == "draft" for bounty in body["bounties"])
    assert all(bounty["claim_count"] == 0 for bounty in body["bounties"])

    task_id = body["id"]
    read = await client.get(f"/v1/tasks/{task_id}")
    assert read.status_code == 200
    assert read.json() == body

    opened = await client.post(f"/v1/tasks/{task_id}/open")
    assert opened.status_code == 200
    assert opened.json()["status"] == "open"
    assert all(bounty["status"] == "open" for bounty in opened.json()["bounties"])
    opened_read = await client.get(f"/v1/tasks/{task_id}")
    assert opened_read.json() == opened.json()

    repeated = await client.post(f"/v1/tasks/{task_id}/open")
    assert repeated.status_code == 409


async def test_list_replace_and_delete_draft_task_with_owner_enforcement(
    client: AsyncClient,
) -> None:
    creator_id = await create_user(client, email="task-crud-owner@example.com")
    other_id = await create_user(client, email="task-crud-other@example.com")
    created = await client.post(
        "/v1/tasks",
        json=task_payload(creator_id, title="Original draft"),
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    original_bounty_id = created.json()["bounties"][0]["id"]

    listed = await client.get("/v1/tasks", headers=client.auth_headers(creator_id))
    assert listed.status_code == 200
    assert [task["id"] for task in listed.json()] == [task_id]

    hidden = await client.get(
        f"/v1/tasks/{task_id}",
        headers=client.auth_headers(other_id),
    )
    assert hidden.status_code == 404

    replaced = await client.put(
        f"/v1/tasks/{task_id}",
        json=task_payload(
            creator_id,
            title="Replaced draft",
            total_budget_minor=3_000,
            bounties=[bounty_payload(title="Replacement bounty", slot_count=3)],
        ),
    )
    assert replaced.status_code == 200
    assert replaced.json()["id"] == task_id
    assert replaced.json()["title"] == "Replaced draft"
    assert replaced.json()["bounties"][0]["id"] != original_bounty_id

    deleted = await client.delete(f"/v1/tasks/{task_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/tasks/{task_id}")).status_code == 404


async def test_open_task_cannot_be_replaced_or_deleted(client: AsyncClient) -> None:
    creator_id = await create_user(client, email="task-crud-open@example.com")
    created = await client.post("/v1/tasks", json=task_payload(creator_id))
    task_id = created.json()["id"]
    assert (await client.post(f"/v1/tasks/{task_id}/open")).status_code == 200
    assert (
        await client.put(
            f"/v1/tasks/{task_id}",
            json=task_payload(creator_id, title="Too late"),
        )
    ).status_code == 409
    assert (await client.delete(f"/v1/tasks/{task_id}")).status_code == 409


@pytest.mark.parametrize(
    ("platform", "action", "influence_metric"),
    [
        ("reddit", "post", "followers"),
        ("reddit", "comment", "karma"),
        ("linkedin", "post", "followers"),
        ("linkedin", "comment", "followers"),
    ],
)
async def test_supports_all_bounty_types(
    client: AsyncClient,
    platform: str,
    action: str,
    influence_metric: str,
) -> None:
    creator_id = await create_user(client)
    response = await client.post(
        "/v1/tasks",
        json=task_payload(
            creator_id,
            bounties=[
                bounty_payload(
                    platform=platform,
                    action=action,
                    influence_metric=influence_metric,
                )
            ],
        ),
    )

    assert response.status_code == 201
    assert response.json()["bounties"][0]["platform"] == platform
    assert response.json()["bounties"][0]["action"] == action


async def test_creator_capability_and_missing_creator_errors(client: AsyncClient) -> None:
    freelancer_id = await create_user(
        client,
        email="freelancer@example.com",
        can_create_tasks=False,
        can_work_tasks=True,
    )
    forbidden = await client.post("/v1/tasks", json=task_payload(freelancer_id))
    missing = await client.post("/v1/tasks", json=task_payload(uuid4()))

    assert forbidden.status_code == 422
    assert missing.status_code == 401


async def test_dual_capability_user_can_create_task(client: AsyncClient) -> None:
    creator_id = await create_user(client, can_create_tasks=True, can_work_tasks=True)
    response = await client.post("/v1/tasks", json=task_payload(creator_id))

    assert response.status_code == 201


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_budget_minor": 1_999},
        {"currency": "US"},
        {"deadline_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        {"bounties": [bounty_payload(proof_requirements=["url", "url"])]},
        {"bounties": [bounty_payload(platform="linkedin", influence_metric="karma")]},
        {"bounties": [bounty_payload(min_influence=10, max_influence=9)]},
        {"total_budget_minor": POSTGRES_BIGINT_MAX + 1},
        {"bounties": [bounty_payload(reward_minor=POSTGRES_BIGINT_MAX + 1)]},
        {"bounties": [bounty_payload(slot_count=POSTGRES_INTEGER_MAX + 1)]},
        {"bounties": [bounty_payload(min_influence=POSTGRES_BIGINT_MAX + 1)]},
    ],
)
async def test_invalid_task_requests_roll_back(
    client: AsyncClient,
    overrides: dict[str, object],
) -> None:
    creator_id = await create_user(client)
    response = await client.post("/v1/tasks", json=task_payload(creator_id, **overrides))

    assert response.status_code == 422
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_database_only_failure_rolls_back_task_and_bounties(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    response = await client.post(
        "/v1/tasks",
        json=task_payload(
            creator_id,
            bounties=[
                bounty_payload(slot_count=1),
                bounty_payload(title="Invalid\x00PostgreSQL text", slot_count=1),
            ],
        ),
    )

    assert response.status_code == 422
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Task)) == 0
        assert await session.scalar(select(func.count()).select_from(Bounty)) == 0


async def test_bounty_deadline_cannot_exceed_task_deadline(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    task_deadline = datetime.now(UTC) + timedelta(days=5)
    response = await client.post(
        "/v1/tasks",
        json=task_payload(
            creator_id,
            deadline_at=task_deadline.isoformat(),
            bounties=[bounty_payload(deadline_at=(task_deadline + timedelta(days=1)).isoformat())],
        ),
    )

    assert response.status_code == 422


async def test_open_empty_task_returns_conflict(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    async with AsyncSessionFactory() as session:
        task_id = await session.scalar(
            text(
                """
                INSERT INTO tasks (
                  creator_id, title, description, total_budget_minor, currency
                ) VALUES (
                  :creator_id, 'Empty task', 'No bounties yet', 1000, 'USD'
                ) RETURNING id
                """
            ),
            {"creator_id": creator_id},
        )
        await session.commit()

    response = await client.post(
        f"/v1/tasks/{task_id}/open",
        headers=client.auth_headers(creator_id),
    )
    assert response.status_code == 409


async def test_open_task_does_not_reopen_closed_bounty(client: AsyncClient) -> None:
    creator_id = await create_user(client)
    created = await client.post("/v1/tasks", json=task_payload(creator_id))
    task_id = created.json()["id"]
    bounty_id = created.json()["bounties"][0]["id"]

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE bounties SET status = 'closed' WHERE id = :bounty_id"),
            {"bounty_id": bounty_id},
        )
        await session.commit()

    response = await client.post(f"/v1/tasks/{task_id}/open")
    assert response.status_code == 409
    read = await client.get(f"/v1/tasks/{task_id}")
    assert read.json()["status"] == "draft"
    assert read.json()["bounties"][0]["status"] == "closed"


async def test_openapi_contains_task_endpoints(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert paths["/v1/tasks"]["post"]
    assert paths["/v1/tasks/{task_id}"]["get"]
    assert paths["/v1/tasks/{task_id}/open"]["post"]
    proof_schema = response.json()["components"]["schemas"]["BountyCreate"]["properties"][
        "proof_requirements"
    ]
    assert proof_schema["uniqueItems"] is True
    assert proof_schema["items"]["enum"] == ["url", "screenshot", "image"]
