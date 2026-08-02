from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.v1.routes import social_profiles as social_profile_routes
from app.main import app
from app.models.task import SocialPlatform
from app.services import social_profiles as social_profile_service
from app.services.enrichment import (
    EnrichmentInvalidResponseError,
    EnrichmentRejectedError,
    EnrichmentResult,
    EnrichmentUnavailableError,
    HackathonSelfAttestedEnrichmentProvider,
    UnavailableEnrichmentProvider,
)
from tests.conftest import TEST_DATABASE_URL


class FakeProvider:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else reddit_result()
        self.error = error
        self.calls: list[tuple[SocialPlatform, str]] = []

    async def enrich(self, *, platform: SocialPlatform, profile_url: str) -> Any:
        self.calls.append((platform, profile_url))
        if self.error is not None:
            raise self.error
        return self.result


async def test_hackathon_provider_only_grants_zero_influence() -> None:
    provider = HackathonSelfAttestedEnrichmentProvider()
    reddit = await provider.enrich(
        platform=SocialPlatform.REDDIT,
        profile_url="https://www.reddit.com/user/example/",
    )
    linkedin = await provider.enrich(
        platform=SocialPlatform.LINKEDIN,
        profile_url="https://www.linkedin.com/in/example/",
    )

    assert reddit.is_verified is True
    assert reddit.follower_count == 0
    assert reddit.reddit_post_karma == 0
    assert reddit.reddit_comment_karma == 0
    assert linkedin.is_verified is True
    assert linkedin.follower_count == 0
    assert linkedin.reddit_post_karma is None
    assert linkedin.reddit_comment_karma is None


def reddit_result(**overrides: object) -> EnrichmentResult:
    values: dict[str, object] = {
        "provider_name": "fake-enrichment",
        "is_verified": True,
        "follower_count": 2_500,
        "following_count": 200,
        "reddit_post_karma": 4_000,
        "reddit_comment_karma": 6_000,
        "account_created_at": datetime(2020, 1, 2, tzinfo=UTC),
        "public_data": {"source": "fake"},
    }
    values.update(overrides)
    return EnrichmentResult.model_validate(values)


def linkedin_result(**overrides: object) -> EnrichmentResult:
    values: dict[str, object] = {
        "provider_name": "fake-enrichment",
        "is_verified": True,
        "follower_count": 1_200,
        "following_count": 350,
        "account_created_at": datetime(2019, 3, 4, tzinfo=UTC),
        "public_data": {"source": "fake"},
    }
    values.update(overrides)
    return EnrichmentResult.model_validate(values)


@pytest.fixture
def install_provider() -> Iterator[Callable[[object], None]]:
    sentinel = object()
    previous = getattr(app.state, "enrichment_provider", sentinel)

    def install(provider: object) -> None:
        app.state.enrichment_provider = provider

    yield install

    if previous is sentinel:
        delattr(app.state, "enrichment_provider")
    else:
        app.state.enrichment_provider = previous


async def create_user(
    client: AsyncClient,
    *,
    email: str,
    can_create_tasks: bool = False,
    can_work_tasks: bool = True,
) -> dict[str, object]:
    response = await client.post(
        "/v1/users",
        json={
            "email": email,
            "display_name": "Social Profile User",
            "can_create_tasks": can_create_tasks,
            "can_work_tasks": can_work_tasks,
            "bio": None,
        },
    )
    assert response.status_code == 201
    return response.json()


async def read_enrichment_data(profile_id: str) -> dict[str, Any]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT enrichment_data FROM social_accounts WHERE id = :profile_id"),
                {"profile_id": UUID(profile_id)},
            )
    finally:
        await engine.dispose()
    assert isinstance(value, dict)
    return value


async def test_put_and_get_reddit_profile_with_safe_enrichment(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="reddit-worker@example.com")
    provider = FakeProvider(
        reddit_result(
            public_data={
                "source": "fake",
                "nested": {
                    "access_token": "provider-secret",
                    "token": "generic-token",
                    "credential": "provider-credential",
                    "items": [{"private_key": "provider-key"}],
                    "visible": "safe",
                },
            }
        )
    )
    install_provider(provider)

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://OLD.REDDIT.COM/u/Example_User/?utm_source=test#profile"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == user["id"]
    assert body["platform"] == "reddit"
    assert body["profile_url"] == "https://www.reddit.com/user/example_user/"
    assert body["follower_count"] == 2_500
    assert body["following_count"] == 200
    assert body["reddit_post_karma"] == 4_000
    assert body["reddit_comment_karma"] == 6_000
    assert body["karma"] == 10_000
    assert body["is_verified"] is True
    assert body["verified_at"]
    assert body["enriched_at"]
    assert body["enrichment_provider"] == "fake-enrichment"
    assert "enrichment_data" not in body
    assert "provider-secret" not in response.text
    assert provider.calls == [(SocialPlatform.REDDIT, "https://www.reddit.com/user/example_user/")]

    stored_data = await read_enrichment_data(body["id"])
    assert stored_data == {
        "source": "fake",
        "nested": {
            "access_token": "[REDACTED]",
            "token": "[REDACTED]",
            "credential": "[REDACTED]",
            "items": [{"private_key": "[REDACTED]"}],
            "visible": "safe",
        },
    }

    listed = await client.get(f"/v1/users/{user['id']}/social-profiles")
    assert listed.status_code == 200
    assert listed.json() == [body]


async def test_linkedin_profile_is_normalized_and_maps_public_metrics(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="linkedin-worker@example.com")
    provider = FakeProvider(linkedin_result())
    install_provider(provider)

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/linkedin",
        json={"profile_url": "https://linkedin.com/in/Alice-Example?trk=public#about"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["profile_url"] == "https://www.linkedin.com/in/alice-example/"
    assert body["follower_count"] == 1_200
    assert body["following_count"] == 350
    assert body["reddit_post_karma"] is None
    assert body["reddit_comment_karma"] is None
    assert body["karma"] is None


@pytest.mark.parametrize(
    ("platform", "profile_url"),
    [
        ("reddit", "http://www.reddit.com/user/example/"),
        ("reddit", "https://reddit.com.evil.example/user/example/"),
        ("reddit", "https://www.linkedin.com/in/example/"),
        ("linkedin", "https://www.reddit.com/user/example/"),
        ("reddit", "https://user:password@reddit.com/user/example/"),
        ("reddit", "https://reddit.com:443/user/example/"),
        ("reddit", "https://reddit.com/user/example/posts/"),
        ("linkedin", "https://linkedin.com/company/example/"),
        ("linkedin", "https://linkedin.com/in/example/extra"),
        ("reddit", "https://reddit.com/user/example user/"),
    ],
)
async def test_invalid_or_mismatched_profile_urls_are_rejected_before_provider_call(
    client: AsyncClient,
    install_provider: Callable[[object], None],
    platform: str,
    profile_url: str,
) -> None:
    user = await create_user(client, email=f"invalid-{uuid4()}@example.com")
    provider = FakeProvider()
    install_provider(provider)

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/{platform}",
        json={"profile_url": profile_url},
    )

    assert response.status_code == 422
    assert provider.calls == []


async def test_request_rejects_social_credentials_and_unsupported_platform(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="extra-field@example.com")
    provider = FakeProvider()
    install_provider(provider)

    extra_field = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={
            "profile_url": "https://reddit.com/user/example/",
            "access_token": "must-not-be-accepted",
        },
    )
    unsupported = await client.put(
        f"/v1/users/{user['id']}/social-profiles/twitter",
        json={"profile_url": "https://twitter.com/example"},
    )

    assert extra_field.status_code == 422
    assert unsupported.status_code == 422
    assert "must-not-be-accepted" not in extra_field.text
    assert provider.calls == []


async def test_missing_and_creator_only_users_are_rejected(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    creator = await create_user(
        client,
        email="creator-only-social@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    provider = FakeProvider()
    install_provider(provider)

    missing = await client.put(
        f"/v1/users/{uuid4()}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/example/"},
    )
    creator_only = await client.put(
        f"/v1/users/{creator['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/example/"},
    )

    assert missing.status_code == 403
    assert creator_only.status_code == 422
    assert provider.calls == []


async def test_normalized_profile_url_cannot_belong_to_two_users(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    first = await create_user(client, email="first-profile-owner@example.com")
    second = await create_user(client, email="second-profile-owner@example.com")
    provider = FakeProvider()
    install_provider(provider)

    created = await client.put(
        f"/v1/users/{first['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/SharedName"},
    )
    conflict = await client.put(
        f"/v1/users/{second['id']}/social-profiles/reddit",
        json={"profile_url": "https://old.reddit.com/u/sharedname/"},
    )

    assert created.status_code == 200
    assert conflict.status_code == 409
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (EnrichmentUnavailableError("provider down"), 503),
        (TimeoutError("provider timeout"), 503),
        (EnrichmentRejectedError("provider rejected"), 502),
        (EnrichmentInvalidResponseError("bad provider payload"), 502),
    ],
)
async def test_provider_failure_for_changed_url_clears_old_metrics(
    client: AsyncClient,
    install_provider: Callable[[object], None],
    error: Exception,
    expected_status: int,
) -> None:
    user = await create_user(client, email=f"provider-failure-{uuid4()}@example.com")
    install_provider(FakeProvider())
    initial = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/old-profile"},
    )
    assert initial.status_code == 200

    install_provider(FakeProvider(error=error))
    failed = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/new-profile"},
    )

    assert failed.status_code == expected_status
    listed = await client.get(f"/v1/users/{user['id']}/social-profiles")
    assert listed.status_code == 200
    profile = listed.json()[0]
    assert profile["id"] == initial.json()["id"]
    assert profile["profile_url"] == "https://www.reddit.com/user/new-profile/"
    assert profile["is_verified"] is False
    assert profile["follower_count"] is None
    assert profile["karma"] is None
    assert profile["verified_at"] is None
    assert profile["enriched_at"] is None
    assert profile["enrichment_provider"] is None


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (EnrichmentUnavailableError("provider down"), 503),
        (TimeoutError("provider timeout"), 503),
        (EnrichmentRejectedError("provider rejected"), 502),
        (EnrichmentInvalidResponseError("bad provider payload"), 502),
    ],
)
async def test_provider_failure_for_same_normalized_url_preserves_verified_metrics(
    client: AsyncClient,
    install_provider: Callable[[object], None],
    error: Exception,
    expected_status: int,
) -> None:
    user = await create_user(client, email=f"same-profile-failure-{uuid4()}@example.com")
    install_provider(FakeProvider())
    initial = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/existing-profile"},
    )
    assert initial.status_code == 200
    initial_profile = initial.json()

    install_provider(FakeProvider(error=error))
    failed = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://old.reddit.com/u/EXISTING-PROFILE/?utm_source=retry"},
    )

    assert failed.status_code == expected_status
    listed = await client.get(f"/v1/users/{user['id']}/social-profiles")
    assert listed.status_code == 200
    profile = listed.json()[0]
    for field_name in (
        "id",
        "profile_url",
        "follower_count",
        "following_count",
        "reddit_post_karma",
        "reddit_comment_karma",
        "karma",
        "account_created_at",
        "is_verified",
        "verified_at",
        "enrichment_provider",
        "enriched_at",
    ):
        assert profile[field_name] == initial_profile[field_name]
    assert await read_enrichment_data(profile["id"]) == {"source": "fake"}


async def test_valid_unverified_provider_result_returns_safe_pending_profile(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="unverified-profile@example.com")
    provider = FakeProvider(
        EnrichmentResult(
            provider_name="fake-enrichment",
            is_verified=False,
            public_data={"reason": "not_found"},
        )
    )
    install_provider(provider)

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/linkedin",
        json={"profile_url": "https://www.linkedin.com/in/not-found/"},
    )

    assert response.status_code == 200
    assert response.json()["is_verified"] is False
    assert response.json()["verified_at"] is None
    assert response.json()["enriched_at"]


async def test_verified_reddit_profile_accepts_karma_without_followers(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="karma-only-profile@example.com")
    install_provider(FakeProvider(reddit_result(follower_count=None)))

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/karma-only"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_verified"] is True
    assert response.json()["follower_count"] is None
    assert response.json()["karma"] == 10_000


@pytest.mark.parametrize(
    "invalid_result",
    [
        EnrichmentResult.model_construct(
            provider_name="fake",
            is_verified=True,
            follower_count=True,
            following_count=1,
            reddit_post_karma=1,
            reddit_comment_karma=1,
            public_data={},
        ),
        EnrichmentResult.model_construct(
            provider_name="fake",
            is_verified=True,
            follower_count=-1,
            following_count=1,
            reddit_post_karma=1,
            reddit_comment_karma=1,
            public_data={},
        ),
        EnrichmentResult.model_construct(
            provider_name="fake",
            is_verified=True,
            follower_count=1,
            following_count=1,
            reddit_post_karma=None,
            reddit_comment_karma=None,
            public_data={},
        ),
    ],
)
async def test_invalid_provider_metrics_return_502_and_leave_profile_unverified(
    client: AsyncClient,
    install_provider: Callable[[object], None],
    invalid_result: EnrichmentResult,
) -> None:
    user = await create_user(client, email=f"invalid-metrics-{uuid4()}@example.com")
    install_provider(FakeProvider(invalid_result))

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/invalid-metrics"},
    )

    assert response.status_code == 502
    listed = await client.get(f"/v1/users/{user['id']}/social-profiles")
    assert listed.status_code == 200
    assert listed.json()[0]["is_verified"] is False


class SupersedingProvider:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult:
        del platform
        if profile_url.endswith("/first-profile/"):
            self.first_started.set()
            await self.release_first.wait()
            return reddit_result(follower_count=111)
        return reddit_result(follower_count=222)


async def test_stale_concurrent_enrichment_cannot_overwrite_replacement(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="concurrent-profile@example.com")
    provider = SupersedingProvider()
    install_provider(provider)

    first_request = asyncio.create_task(
        client.put(
            f"/v1/users/{user['id']}/social-profiles/reddit",
            json={"profile_url": "https://reddit.com/user/first-profile"},
        )
    )
    await asyncio.wait_for(provider.first_started.wait(), timeout=2)

    second = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/second-profile"},
    )
    provider.release_first.set()
    first = await first_request

    assert second.status_code == 200
    assert first.status_code == 409
    listed = (await client.get(f"/v1/users/{user['id']}/social-profiles")).json()
    assert listed[0]["profile_url"] == "https://www.reddit.com/user/second-profile/"
    assert listed[0]["follower_count"] == 222


class SameURLSupersedingProvider:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls = 0

    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult:
        del platform, profile_url
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
            return reddit_result(follower_count=111)
        return reddit_result(follower_count=222)


async def test_same_url_concurrent_enrichment_uses_request_generation(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="same-url-concurrent@example.com")
    provider = SameURLSupersedingProvider()
    install_provider(provider)
    path = f"/v1/users/{user['id']}/social-profiles/reddit"
    payload = {"profile_url": "https://reddit.com/user/same-profile"}

    first_request = asyncio.create_task(client.put(path, json=payload))
    await asyncio.wait_for(provider.first_started.wait(), timeout=2)
    second = await client.put(path, json=payload)
    provider.release_first.set()
    first = await first_request

    assert second.status_code == 200
    assert first.status_code == 409
    listed = (await client.get(f"/v1/users/{user['id']}/social-profiles")).json()
    assert listed[0]["follower_count"] == 222


class HangingProvider:
    async def enrich(
        self,
        *,
        platform: SocialPlatform,
        profile_url: str,
    ) -> EnrichmentResult:
        del platform, profile_url
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_service_enforces_provider_timeout(
    client: AsyncClient,
    install_provider: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await create_user(client, email="provider-timeout@example.com")
    install_provider(HangingProvider())
    monkeypatch.setattr(social_profile_service, "ENRICHMENT_TIMEOUT_SECONDS", 0.01)

    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/timeout-profile"},
    )

    assert response.status_code == 503
    listed = (await client.get(f"/v1/users/{user['id']}/social-profiles")).json()
    assert listed[0]["is_verified"] is False


async def test_unconfigured_provider_fails_safe_and_preserves_url(
    client: AsyncClient,
    install_provider: Callable[[object], None],
) -> None:
    user = await create_user(client, email="unconfigured-provider@example.com")
    install_provider(UnavailableEnrichmentProvider())
    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/linkedin",
        json={"profile_url": "https://linkedin.com/in/pending-profile"},
    )

    assert response.status_code == 503
    listed = (await client.get(f"/v1/users/{user['id']}/social-profiles")).json()
    assert listed[0]["profile_url"] == "https://www.linkedin.com/in/pending-profile/"
    assert listed[0]["is_verified"] is False


async def test_unexpected_database_error_returns_safe_generic_500(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_put(*args: object, **kwargs: object) -> object:
        raise DBAPIError(
            "INSERT secret_table",
            {"token": "database-secret"},
            RuntimeError("database-secret"),
            False,
        )

    monkeypatch.setattr(social_profile_routes, "put_social_profile", fail_put)
    user = await create_user(client, email="safe-profile-error@example.com")
    response = await client.put(
        f"/v1/users/{user['id']}/social-profiles/reddit",
        json={"profile_url": "https://reddit.com/user/safe-error"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "database-secret" not in response.text


async def test_profile_list_is_bare_and_missing_user_returns_404(
    client: AsyncClient,
) -> None:
    user = await create_user(client, email="empty-profile-list@example.com")

    empty = await client.get(f"/v1/users/{user['id']}/social-profiles")
    missing = await client.get(f"/v1/users/{uuid4()}/social-profiles")

    assert empty.status_code == 200
    assert empty.json() == []
    assert missing.status_code == 403
