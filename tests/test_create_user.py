from httpx import AsyncClient


def creator_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "email": "creator@example.com",
        "display_name": "Campaign Creator",
        "can_create_tasks": True,
        "can_work_tasks": False,
        "bio": None,
    }
    payload.update(overrides)
    return payload


async def test_health_checks_database(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_task_creator(client: AsyncClient) -> None:
    response = await client.post("/v1/users", json=creator_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "creator@example.com"
    assert body["display_name"] == "Campaign Creator"
    assert body["can_create_tasks"] is True
    assert body["can_work_tasks"] is False
    assert body["bio"] is None
    assert body["id"]
    assert body["created_at"]
    assert body["updated_at"]
    assert "prava_account_ref" not in body
    assert "prava_account_status" not in body


async def test_create_freelancer(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/users",
        json=creator_payload(
            email="worker@example.com",
            display_name="  Marketing Freelancer  ",
            can_create_tasks=False,
            can_work_tasks=True,
            bio="  Reddit and LinkedIn marketing  ",
        ),
    )

    assert response.status_code == 201
    assert response.json()["display_name"] == "Marketing Freelancer"
    assert response.json()["bio"] == "Reddit and LinkedIn marketing"


async def test_create_dual_capability_user(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/users",
        json=creator_payload(can_create_tasks=True, can_work_tasks=True),
    )

    assert response.status_code == 201
    assert response.json()["can_create_tasks"] is True
    assert response.json()["can_work_tasks"] is True


async def test_email_is_normalized_and_case_insensitively_unique(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/users",
        json=creator_payload(email="Creator@Example.COM"),
    )
    duplicate = await client.post(
        "/v1/users",
        json=creator_payload(email="creator@example.com"),
    )

    assert first.status_code == 201
    assert first.json()["email"] == "creator@example.com"
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "A user with this email already exists"}


async def test_invalid_email_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/users",
        json=creator_payload(email="not-an-email"),
    )

    assert response.status_code == 422


async def test_blank_display_name_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/users",
        json=creator_payload(display_name="   "),
    )

    assert response.status_code == 422


async def test_user_without_capability_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/users",
        json=creator_payload(can_create_tasks=False, can_work_tasks=False),
    )

    assert response.status_code == 422


async def test_session_recovers_after_duplicate_email(client: AsyncClient) -> None:
    assert (await client.post("/v1/users", json=creator_payload())).status_code == 201
    assert (await client.post("/v1/users", json=creator_payload())).status_code == 409

    response = await client.post(
        "/v1/users",
        json=creator_payload(email="another@example.com"),
    )
    assert response.status_code == 201


async def test_openapi_contains_user_creation_endpoint(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    operation = response.json()["paths"]["/v1/users"]["post"]
    assert operation["responses"]["201"]
