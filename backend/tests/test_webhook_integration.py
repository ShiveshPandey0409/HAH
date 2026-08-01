from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from random import Random
from uuid import UUID

from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.db.session import AsyncSessionFactory
from app.main import app
from app.models.webhook import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.services.webhooks import (
    ENCRYPTED_WEBHOOK_URL,
    ResolvedWebhookDestination,
    WebhookDeliveryPolicy,
    WebhookHTTPResponse,
    WebhookRuntime,
    build_webhook_cipher,
    process_next_webhook_delivery,
    webhook_signature,
)
from tests.test_submissions import claimed_work, submit, url_proof, verify


class FakeResolver:
    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        assert hostname == "hooks.example.com"
        assert port == 443
        return ("93.184.216.34",)


class RecordingTransport:
    def __init__(self, *responses: WebhookHTTPResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[ResolvedWebhookDestination, dict[str, str], bytes]] = []

    async def post(
        self,
        destination: ResolvedWebhookDestination,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        response_body_limit_bytes: int,
    ) -> WebhookHTTPResponse:
        assert timeout_seconds > 0
        assert response_body_limit_bytes > 0
        self.calls.append((destination, dict(headers), body))
        return self.responses.pop(0)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


CAPABILITY_URL = "https://hooks.example.com/events/capability-token?key=query-secret"


def webhook_runtime(
    transport: RecordingTransport | None = None,
    *,
    max_attempts: int = 3,
) -> WebhookRuntime:
    return WebhookRuntime(
        cipher=build_webhook_cipher([Fernet.generate_key()]),
        resolver=FakeResolver(),
        transport=transport,
        policy=WebhookDeliveryPolicy(
            max_attempts=max_attempts,
            timeout_seconds=1,
            lease_seconds=5,
            backoff_base_seconds=1,
            backoff_cap_seconds=2,
            poll_interval_seconds=0.01,
            dns_timeout_seconds=1,
        ),
    )


async def configure_endpoint(
    client: AsyncClient,
    creator_id: UUID,
    runtime: WebhookRuntime,
) -> tuple[UUID, str]:
    app.state.webhook_runtime = runtime
    response = await client.put(
        f"/v1/users/{creator_id}/webhook",
        json={
            "url": CAPABILITY_URL,
            "subscribed_events": ["submission.created", "verification.completed"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["signing_secret"].startswith("whsec_")
    assert body["url"] == CAPABILITY_URL
    return UUID(body["id"]), body["signing_secret"]


async def test_http_configuration_events_signing_and_worker_delivery(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "webhook-e2e")
    transport = RecordingTransport(
        WebhookHTTPResponse(status_code=204),
        WebhookHTTPResponse(status_code=200, body=b'{"access_token":"must-not-persist"}'),
    )
    runtime = webhook_runtime(transport)
    endpoint_id, first_secret = await configure_endpoint(client, creator_id, runtime)

    read = await client.get(f"/v1/users/{creator_id}/webhook")
    assert read.status_code == 200, read.text
    assert "signing_secret" not in read.json()
    assert read.json()["url"] == CAPABILITY_URL

    rotated = await client.put(
        f"/v1/users/{creator_id}/webhook",
        json={
            "url": CAPABILITY_URL,
            "subscribed_events": ["submission.created", "verification.completed"],
        },
    )
    assert rotated.status_code == 200
    rotated_body = rotated.json()
    signing_secret = rotated_body["signing_secret"]
    assert UUID(rotated_body["id"]) == endpoint_id
    assert signing_secret != first_secret
    assert rotated_body["url"] == CAPABILITY_URL

    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])
    approved = await verify(client, submission_id, creator_id, result="passed")
    replayed = await verify(client, submission_id, creator_id, result="passed")
    assert approved.status_code == 200, approved.text
    assert replayed.json() == approved.json()

    async with AsyncSessionFactory() as session:
        deliveries = list(
            (
                await session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.event_type))
            ).all()
        )
        assert [delivery.event_type for delivery in deliveries] == [
            "submission.created",
            "verification.completed",
        ]
        assert all(delivery.endpoint_id == endpoint_id for delivery in deliveries)
        assert all(delivery.status == DeliveryStatus.PENDING for delivery in deliveries)
        assert all(
            b"https://www.reddit.com" not in delivery.payload_body for delivery in deliveries
        )

    clock = FixedClock(datetime.now(UTC))
    assert await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )
    assert await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )
    assert not await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )

    assert len(transport.calls) == 2
    for destination, headers, body in transport.calls:
        assert destination.addresses == ("93.184.216.34",)
        assert destination.request_target == "/events/capability-token?key=query-secret"
        assert headers["X-HAH-Webhook-Signature"] == webhook_signature(
            signing_secret=signing_secret,
            event_id=UUID(headers["X-HAH-Event-Id"]),
            timestamp=int(headers["X-HAH-Webhook-Timestamp"]),
            body=body,
        )

    async with AsyncSessionFactory() as session:
        delivered = list((await session.scalars(select(WebhookDelivery))).all())
        assert all(item.status == DeliveryStatus.DELIVERED for item in delivered)
        assert {item.last_response_body for item in delivered} == {None, "[redacted]"}
        endpoint = await session.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None
        assert endpoint.url == ENCRYPTED_WEBHOOK_URL
        assert first_secret.encode() not in endpoint.secret_ciphertext
        assert signing_secret.encode() not in endpoint.secret_ciphertext
        assert b"capability-token" not in endpoint.secret_ciphertext
        assert b"query-secret" not in endpoint.secret_ciphertext


async def test_retryable_failures_exhaust_without_leaking_response(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "webhook-retry")
    transport = RecordingTransport(
        WebhookHTTPResponse(status_code=503, body=b"private upstream details"),
        WebhookHTTPResponse(status_code=503, body=b"another private response"),
    )
    runtime = webhook_runtime(transport, max_attempts=2)
    await configure_endpoint(client, creator_id, runtime)
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text

    clock = FixedClock(datetime.now(UTC))
    assert await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )
    async with AsyncSessionFactory() as session:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.status == DeliveryStatus.RETRYING)
            .values(next_attempt_at=clock.now())
        )
        await session.commit()

    assert await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )
    async with AsyncSessionFactory() as session:
        delivery = await session.scalar(select(WebhookDelivery))
        assert delivery is not None
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.attempt_count == 2
        assert delivery.last_response_code == 503
        assert delivery.last_response_body == "[redacted]"
        assert delivery.last_error == "http_503"
        assert await session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1


async def test_unreadable_endpoint_credentials_return_safe_configuration_error(
    client: AsyncClient,
) -> None:
    creator_id, _, _ = await claimed_work(client, "webhook-unreadable")
    runtime = webhook_runtime()
    endpoint_id, _ = await configure_endpoint(client, creator_id, runtime)

    async with AsyncSessionFactory() as session:
        endpoint = await session.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None
        endpoint.secret_ciphertext = b"not-a-fernet-envelope"
        await session.commit()

    response = await client.get(f"/v1/users/{creator_id}/webhook")
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook configuration is unavailable"}
    assert "capability-token" not in response.text
    assert "query-secret" not in response.text


async def test_worker_fails_closed_without_transport_for_corrupt_credentials(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "webhook-corrupt-worker")
    transport = RecordingTransport(WebhookHTTPResponse(status_code=204))
    runtime = webhook_runtime(transport)
    endpoint_id, _ = await configure_endpoint(client, creator_id, runtime)
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text

    async with AsyncSessionFactory() as session:
        endpoint = await session.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None
        endpoint.secret_ciphertext = b"not-a-fernet-envelope"
        await session.commit()

    clock = FixedClock(datetime.now(UTC))
    assert await process_next_webhook_delivery(
        AsyncSessionFactory,
        runtime=runtime,
        clock=clock,
        random_source=Random(0),
    )
    assert transport.calls == []

    async with AsyncSessionFactory() as session:
        delivery = await session.scalar(select(WebhookDelivery))
    assert delivery is not None
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.last_error == "secret_unavailable"
    assert delivery.attempt_count == 1
