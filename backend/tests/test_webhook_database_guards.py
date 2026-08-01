from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import AsyncSessionFactory
from app.services.webhooks import (
    ENCRYPTED_WEBHOOK_URL,
    ExhaustedWebhookDelivery,
    LeasedWebhookDelivery,
    WebhookDeliveryPolicy,
    lease_due_webhook_delivery,
)


async def seed_creator_and_endpoint(
    *,
    email: str = "webhook-guard@example.com",
    status: str = "active",
) -> tuple[UUID, UUID]:
    async with AsyncSessionFactory() as session:
        creator_id = await session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, can_create_tasks)
                VALUES (:email, 'Webhook Guard', true)
                RETURNING id
                """
            ),
            {"email": email},
        )
        endpoint_id = await session.scalar(
            text(
                """
                INSERT INTO webhook_endpoints (
                  creator_id, url, secret_hash, secret_ciphertext,
                  subscribed_events, status
                ) VALUES (
                  :creator_id, :url,
                  'sha256:test', :secret_ciphertext,
                  ARRAY['submission.created'], :status
                ) RETURNING id
                """
            ),
            {
                "creator_id": creator_id,
                "url": ENCRYPTED_WEBHOOK_URL,
                "secret_ciphertext": b"encrypted-secret" if status == "active" else None,
                "status": status,
            },
        )
        await session.commit()
    assert creator_id is not None
    assert endpoint_id is not None
    return creator_id, endpoint_id


async def insert_pending_delivery(endpoint_id: UUID, entity_id: UUID) -> UUID:
    async with AsyncSessionFactory() as session:
        delivery_id = await session.scalar(
            text(
                """
                INSERT INTO webhook_deliveries (
                  endpoint_id, event_type, entity_type, entity_id,
                  deduplication_key, payload, payload_body, next_attempt_at
                ) VALUES (
                  :endpoint_id, 'submission.created', 'submission', :entity_id,
                  :deduplication_key, CAST(:payload AS jsonb), :payload_body, now()
                ) RETURNING id
                """
            ),
            {
                "endpoint_id": endpoint_id,
                "entity_id": entity_id,
                "deduplication_key": f"submission.created:{entity_id}",
                "payload": '{"safe":true}',
                "payload_body": b'{"safe":true}',
            },
        )
        await session.commit()
    assert delivery_id is not None
    return delivery_id


async def test_only_one_active_endpoint_per_creator_but_disabled_history_is_allowed() -> None:
    creator_id, _ = await seed_creator_and_endpoint()
    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                INSERT INTO webhook_endpoints (
                  creator_id, url, secret_hash, subscribed_events, status
                ) VALUES (
                  :creator_id, :url,
                  'sha256:legacy', ARRAY[]::text[], 'disabled'
                )
                """
            ),
            {"creator_id": creator_id, "url": ENCRYPTED_WEBHOOK_URL},
        )
        await session.commit()

        with pytest.raises(DBAPIError) as plaintext_disabled_error:
            await session.execute(
                text(
                    """
                    INSERT INTO webhook_endpoints (
                      creator_id, url, secret_hash, subscribed_events, status
                    ) VALUES (
                      :creator_id, 'https://legacy.example.com/capability-token',
                      'sha256:legacy-plaintext', ARRAY[]::text[], 'disabled'
                    )
                    """
                ),
                {"creator_id": creator_id},
            )
        assert getattr(plaintext_disabled_error.value.orig, "sqlstate", None) == "23514"
        await session.rollback()

        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text(
                    """
                    INSERT INTO webhook_endpoints (
                      creator_id, url, secret_hash, secret_ciphertext,
                      subscribed_events, status
                    ) VALUES (
                      :creator_id, :url,
                      'sha256:duplicate', :ciphertext,
                      ARRAY[]::text[], 'active'
                    )
                    """
                ),
                {
                    "creator_id": creator_id,
                    "url": ENCRYPTED_WEBHOOK_URL,
                    "ciphertext": b"encrypted",
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23505"


@pytest.mark.parametrize(
    ("url", "ciphertext", "events"),
    [
        ("https://hooks.example.com/events", b"encrypted", ["submission.created"]),
        ("", b"encrypted", ["submission.created"]),
        (ENCRYPTED_WEBHOOK_URL, None, ["submission.created"]),
        (ENCRYPTED_WEBHOOK_URL, b"encrypted", ["submission.approved"]),
        (
            ENCRYPTED_WEBHOOK_URL,
            b"encrypted",
            ["submission.created", "submission.created"],
        ),
    ],
)
async def test_endpoint_database_checks(
    url: str,
    ciphertext: bytes | None,
    events: list[str],
) -> None:
    async with AsyncSessionFactory() as session:
        creator_id = await session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, can_create_tasks)
                VALUES ('endpoint-check@example.com', 'Endpoint Check', true)
                RETURNING id
                """
            )
        )
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text(
                    """
                    INSERT INTO webhook_endpoints (
                      creator_id, url, secret_hash, secret_ciphertext,
                      subscribed_events, status
                    ) VALUES (
                      :creator_id, :url, 'sha256:test', :ciphertext, :events, 'active'
                    )
                    """
                ),
                {
                    "creator_id": creator_id,
                    "url": url,
                    "ciphertext": ciphertext,
                    "events": events,
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"


async def test_delivery_identity_and_payload_guards() -> None:
    creator_id, endpoint_id = await seed_creator_and_endpoint()
    event_id = uuid4()
    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                INSERT INTO webhook_deliveries (
                  endpoint_id, event_id, event_type, entity_type, entity_id,
                  deduplication_key, payload, payload_body, next_attempt_at
                ) VALUES (
                  :endpoint_id, :event_id, 'submission.created', 'submission', :entity_id,
                  'submission.created:first', '{}', '{}', now()
                )
                """
            ),
            {"endpoint_id": endpoint_id, "event_id": event_id, "entity_id": creator_id},
        )
        await session.commit()

        for values in (
            {
                "event_id": event_id,
                "event_type": "submission.created",
                "dedupe": "submission.created:second",
                "body": b"{}",
            },
            {
                "event_id": uuid4(),
                "event_type": "submission.created",
                "dedupe": "submission.created:first",
                "body": b"{}",
            },
            {
                "event_id": uuid4(),
                "event_type": "submission.approved",
                "dedupe": "submission.created:third",
                "body": b"{}",
            },
            {
                "event_id": uuid4(),
                "event_type": "submission.created",
                "dedupe": "",
                "body": b"{}",
            },
            {
                "event_id": uuid4(),
                "event_type": "submission.created",
                "dedupe": "submission.created:empty-body",
                "body": b"",
            },
        ):
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        """
                        INSERT INTO webhook_deliveries (
                          endpoint_id, event_id, event_type, entity_type, entity_id,
                          deduplication_key, payload, payload_body, next_attempt_at
                        ) VALUES (
                          :endpoint_id, :event_id, :event_type, 'submission', :entity_id,
                          :dedupe, '{}', :body, now()
                        )
                        """
                    ),
                    {
                        "endpoint_id": endpoint_id,
                        "entity_id": creator_id,
                        **values,
                    },
                )
            await session.rollback()


@pytest.mark.parametrize(
    "columns",
    [
        "status = 'pending', next_attempt_at = NULL",
        "status = 'delivered', delivered_at = NULL, next_attempt_at = NULL",
        "status = 'failed', failed_at = NULL, next_attempt_at = NULL",
        "lease_token = gen_random_uuid(), lease_expires_at = NULL",
        "last_response_code = 99",
        "last_response_body = repeat('x', 1025)",
        "last_error = repeat('x', 101)",
    ],
)
async def test_delivery_state_and_size_guards(columns: str) -> None:
    creator_id, endpoint_id = await seed_creator_and_endpoint()
    delivery_id = await insert_pending_delivery(endpoint_id, creator_id)
    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text(f"UPDATE webhook_deliveries SET {columns} WHERE id = :delivery_id"),
                {"delivery_id": delivery_id},
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "23514"


async def test_skip_locked_lease_allows_only_one_worker_to_claim_a_due_delivery() -> None:
    creator_id, endpoint_id = await seed_creator_and_endpoint()
    delivery_id = await insert_pending_delivery(endpoint_id, creator_id)
    barrier = asyncio.Barrier(2)
    policy = WebhookDeliveryPolicy()

    async def lease_once() -> LeasedWebhookDelivery | ExhaustedWebhookDelivery | None:
        async with AsyncSessionFactory() as session:
            await barrier.wait()
            return await lease_due_webhook_delivery(
                session,
                now=datetime.now(UTC),
                policy=policy,
            )

    leases = await asyncio.gather(lease_once(), lease_once())
    claimed = [lease for lease in leases if isinstance(lease, LeasedWebhookDelivery)]

    assert len(claimed) == 1
    assert claimed[0].delivery_id == delivery_id
    assert claimed[0].attempt_number == 1
    assert leases.count(None) == 1
