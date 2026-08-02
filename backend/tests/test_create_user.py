from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.api.v1.routes.auth import get_password_reset_notifier
from app.db.session import AsyncSessionFactory
from app.main import app
from app.models.user import User, UserSession
from app.services.password_reset_delivery import PasswordResetNotifier

PASSWORD = "correct horse battery staple"


def signup_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "email": "creator@example.com",
        "password": PASSWORD,
        "display_name": "Campaign Creator",
        "can_create_tasks": True,
        "can_work_tasks": False,
        "bio": None,
    }
    payload.update(overrides)
    return payload


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class RecordingNotifier(PasswordResetNotifier):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return True

    async def send(self, *, email: str, token: str) -> None:
        self.sent.append((email, token))


async def test_signup_login_me_logout_and_safe_storage(client: AsyncClient) -> None:
    signed_up = await client.post("/v1/auth/signup", json=signup_payload())
    assert signed_up.status_code == 201
    body = signed_up.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"].startswith("hah_session_")
    assert body["user"]["email"] == "creator@example.com"
    assert "password" not in body
    assert "password_hash" not in body["user"]

    token = body["access_token"]
    user_id = UUID(body["user"]["id"])
    me = await client.get("/v1/auth/me", headers=bearer(token))
    assert me.status_code == 200
    assert me.json() == body["user"]

    async with AsyncSessionFactory() as session:
        user = await session.get(User, user_id)
        stored_session = await session.scalar(
            select(UserSession).where(UserSession.user_id == user_id)
        )
    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash.startswith("scrypt$16384$8$1$")
    assert PASSWORD not in user.password_hash
    assert stored_session is not None
    assert stored_session.token_hash != token
    assert len(stored_session.token_hash) == 64

    logged_in = await client.post(
        "/v1/auth/login",
        json={"email": "CREATOR@example.com", "password": PASSWORD},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["id"] == str(user_id)

    logged_out = await client.post("/v1/auth/logout", headers=bearer(token))
    assert logged_out.status_code == 204
    assert (await client.get("/v1/auth/me", headers=bearer(token))).status_code == 401


async def test_signup_validation_and_duplicate_email(client: AsyncClient) -> None:
    assert (
        await client.post(
            "/v1/auth/signup",
            json=signup_payload(email="not-an-email"),
        )
    ).status_code == 422
    assert (
        await client.post(
            "/v1/auth/signup",
            json=signup_payload(display_name="   "),
        )
    ).status_code == 422
    assert (
        await client.post(
            "/v1/auth/signup",
            json=signup_payload(can_create_tasks=False, can_work_tasks=False),
        )
    ).status_code == 422
    assert (
        await client.post(
            "/v1/auth/signup",
            json=signup_payload(password="short"),
        )
    ).status_code == 422

    first = await client.post(
        "/v1/auth/signup",
        json=signup_payload(email="Creator@Example.COM"),
    )
    duplicate = await client.post(
        "/v1/auth/signup",
        json=signup_payload(email="creator@example.com"),
    )
    assert first.status_code == 201
    assert first.json()["user"]["email"] == "creator@example.com"
    assert duplicate.status_code == 409


async def test_login_does_not_reveal_whether_email_exists(client: AsyncClient) -> None:
    await client.post("/v1/auth/signup", json=signup_payload())
    wrong_password = await client.post(
        "/v1/auth/login",
        json={"email": "creator@example.com", "password": "wrong password"},
    )
    missing_email = await client.post(
        "/v1/auth/login",
        json={"email": "missing@example.com", "password": "wrong password"},
    )
    assert wrong_password.status_code == missing_email.status_code == 401
    assert wrong_password.json() == missing_email.json() == {"detail": "Invalid email or password"}


async def test_change_password_keeps_current_session_and_revokes_other_sessions(
    client: AsyncClient,
) -> None:
    signed_up = await client.post("/v1/auth/signup", json=signup_payload())
    current_token = signed_up.json()["access_token"]
    second_login = await client.post(
        "/v1/auth/login",
        json={"email": "creator@example.com", "password": PASSWORD},
    )
    second_token = second_login.json()["access_token"]

    changed = await client.post(
        "/v1/auth/change-password",
        headers=bearer(current_token),
        json={"current_password": PASSWORD, "new_password": "a new secure password"},
    )
    assert changed.status_code == 204
    assert (await client.get("/v1/auth/me", headers=bearer(current_token))).status_code == 200
    assert (await client.get("/v1/auth/me", headers=bearer(second_token))).status_code == 401
    assert (
        await client.post(
            "/v1/auth/login",
            json={"email": "creator@example.com", "password": PASSWORD},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/v1/auth/login",
            json={"email": "creator@example.com", "password": "a new secure password"},
        )
    ).status_code == 200


async def test_forgot_and_reset_password_is_single_use_and_revokes_sessions(
    client: AsyncClient,
) -> None:
    signed_up = await client.post("/v1/auth/signup", json=signup_payload())
    old_token = signed_up.json()["access_token"]
    notifier = RecordingNotifier()
    app.dependency_overrides[get_password_reset_notifier] = lambda: notifier
    try:
        missing = await client.post(
            "/v1/auth/forgot-password",
            json={"email": "missing@example.com"},
        )
        requested = await client.post(
            "/v1/auth/forgot-password",
            json={"email": "creator@example.com"},
        )
    finally:
        app.dependency_overrides.pop(get_password_reset_notifier, None)

    assert missing.status_code == requested.status_code == 202
    assert len(notifier.sent) == 1
    email, reset_token = notifier.sent[0]
    assert email == "creator@example.com"
    assert reset_token.startswith("hah_reset_")

    reset = await client.post(
        "/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "reset password value"},
    )
    assert reset.status_code == 204
    assert (await client.get("/v1/auth/me", headers=bearer(old_token))).status_code == 401
    assert (
        await client.post(
            "/v1/auth/reset-password",
            json={"token": reset_token, "new_password": "another password value"},
        )
    ).status_code == 400
    assert (
        await client.post(
            "/v1/auth/login",
            json={"email": "creator@example.com", "password": "reset password value"},
        )
    ).status_code == 200


async def test_forgot_password_fails_closed_without_delivery(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/forgot-password",
        json={"email": "anyone@example.com"},
    )
    assert response.status_code == 503


async def test_all_business_routes_require_bearer_auth(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/tasks",
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_openapi_contains_auth_and_authenticated_task_crud(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    for path in (
        "/v1/auth/signup",
        "/v1/auth/login",
        "/v1/auth/me",
        "/v1/auth/logout",
        "/v1/auth/change-password",
        "/v1/auth/forgot-password",
        "/v1/auth/reset-password",
    ):
        assert path in paths
    assert "/v1/users" not in paths
    assert set(paths["/v1/tasks"]) >= {"get", "post"}
    assert set(paths["/v1/tasks/{task_id}"]) >= {"get", "put", "delete"}
    assert paths["/v1/tasks"]["post"]["security"] == [{"HTTP session": []}]
    public_operations = {
        ("/v1/auth/signup", "post"),
        ("/v1/auth/login", "post"),
        ("/v1/auth/forgot-password", "post"),
        ("/v1/auth/reset-password", "post"),
    }
    for path, operations in paths.items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in operations.items():
            if method == "parameters" or (path, method) in public_operations:
                continue
            assert operation["security"] == [{"HTTP session": []}], (path, method)
