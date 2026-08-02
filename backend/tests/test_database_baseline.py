from __future__ import annotations

import asyncio
import json

import pytest
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.db.alembic_config import escape_alembic_config_value
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL


def _migration_config() -> Config:
    config = Config(BACKEND_DIR / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        escape_alembic_config_value(TEST_DATABASE_URL),
    )
    return config


def _downgrade(revision: str) -> None:
    command.downgrade(_migration_config(), revision)


def _upgrade(revision: str) -> None:
    command.upgrade(_migration_config(), revision)


async def test_sql_baseline_is_upgraded_to_alembic_head() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()

    assert revision == "20260802_0008"


async def test_legacy_mcp_actor_and_scopes_are_backfilled_on_upgrade() -> None:
    await asyncio.to_thread(_downgrade, "20260802_0005")
    upgraded = False
    try:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                user_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO users (email, display_name, can_create_tasks)
                        VALUES ('oauth-backfill@example.com', 'OAuth Backfill', true)
                        RETURNING id
                        """
                    )
                )
                api_client_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO api_clients (
                          creator_id, name, client_key, secret_hash, scopes
                        ) VALUES (
                          :user_id, 'Backfill Client', 'backfill-client',
                          'sha256:test', ARRAY['tasks:create', 'tasks:create', NULL]
                        )
                        RETURNING id
                        """
                    ),
                    {"user_id": user_id},
                )
                request_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO mcp_requests (
                          api_client_id, method, idempotency_key
                        ) VALUES (
                          :api_client_id, 'create_task', 'backfill-request'
                        )
                        RETURNING id
                        """
                    ),
                    {"api_client_id": api_client_id},
                )
        finally:
            await engine.dispose()

        await asyncio.to_thread(_upgrade, "head")
        upgraded = True

        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT actor_user_id, auth_scopes, oauth_delegation_id,
                                   oauth_consent_version, oauth_authorization_id
                              FROM mcp_requests
                             WHERE id = :request_id
                            """
                        ),
                        {"request_id": request_id},
                    )
                ).one()
        finally:
            await engine.dispose()
    finally:
        if not upgraded:
            await asyncio.to_thread(_upgrade, "head")

    assert row.actor_user_id == user_id
    assert row.auth_scopes == ["tasks:create"]
    assert row.oauth_delegation_id is None
    assert row.oauth_consent_version is None
    assert row.oauth_authorization_id is None


async def test_oauth_rows_block_lossy_downgrade() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text(
                    """
                    INSERT INTO users (email, display_name, can_create_tasks)
                    VALUES ('oauth-downgrade@example.com', 'OAuth Downgrade', true)
                    RETURNING id
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO oauth_identities (user_id, issuer, subject)
                    VALUES (:user_id, 'https://issuer.example.com', 'downgrade-subject')
                    """
                ),
                {"user_id": user_id},
            )
    finally:
        await engine.dispose()

    with pytest.raises(DBAPIError) as caught:
        await asyncio.to_thread(_downgrade, "20260802_0005")
    assert getattr(caught.value.orig, "sqlstate", None) == "HMG01"

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    assert revision == "20260802_0008"


async def test_http_auth_rows_block_lossy_downgrade(client: AsyncClient) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "email": "auth-downgrade@example.com",
            "password": "correct horse battery staple",
            "display_name": "Auth Downgrade",
            "can_create_tasks": True,
            "can_work_tasks": False,
        },
    )
    assert signup.status_code == 201

    with pytest.raises(DBAPIError) as caught:
        await asyncio.to_thread(_downgrade, "20260802_0006")
    assert getattr(caught.value.orig, "sqlstate", None) == "HMG02"

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    assert revision == "20260802_0008"


async def test_legacy_webhook_destination_is_disabled_and_scrubbed_on_upgrade() -> None:
    await asyncio.to_thread(_downgrade, "20260802_0004")
    upgraded = False
    try:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as connection:
                creator_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO users (email, display_name, can_create_tasks)
                        VALUES (
                          'legacy-webhook-migration@example.com',
                          'Legacy Webhook Migration',
                          true
                        )
                        RETURNING id
                        """
                    )
                )
                task_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO tasks (
                          creator_id, title, description, total_budget_minor,
                          currency, created_via
                        ) VALUES (
                          :creator_id,
                          'Legacy MCP task',
                          'Business task retained after audit scrubbing',
                          1000,
                          'USD',
                          'mcp'
                        )
                        RETURNING id
                        """
                    ),
                    {"creator_id": creator_id},
                )
                api_client_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO api_clients (
                          creator_id, name, client_key, secret_hash, scopes
                        ) VALUES (
                          :creator_id,
                          'Legacy MCP Client',
                          'legacy-mcp-client',
                          'sha256:legacy',
                          ARRAY['tasks:create']
                        )
                        RETURNING id
                        """
                    ),
                    {"creator_id": creator_id},
                )
                mcp_request_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO mcp_requests (
                          api_client_id, method, idempotency_key, status,
                          request_data, response_data, task_id, completed_at
                        ) VALUES (
                          :api_client_id,
                          'create_task',
                          'legacy-mcp-idempotency',
                          'succeeded',
                          jsonb_build_object(
                            'description', 'api_key=legacy-mcp-request-secret',
                            'bounties', jsonb_build_array(jsonb_build_object(
                              'instructions', 'password=legacy-bounty-secret'
                            ))
                              ),
                              jsonb_build_object(
                                'id', CAST(CAST(:task_id AS uuid) AS text),
                                'description', 'token=legacy-mcp-response-secret'
                              ),
                              CAST(:task_id AS uuid),
                          now()
                        )
                        RETURNING id
                        """
                    ),
                    {"api_client_id": api_client_id, "task_id": task_id},
                )
                endpoint_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO webhook_endpoints (
                          creator_id, url, secret_hash, subscribed_events, status
                        ) VALUES (
                          :creator_id,
                          'https://legacy.example.com/capability-token?key=query-secret',
                          'sha256:legacy',
                          ARRAY['submission.approved'],
                          'active'
                        )
                        RETURNING id
                        """
                    ),
                    {"creator_id": creator_id},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO webhook_deliveries (
                          endpoint_id, event_type, entity_type, entity_id,
                          payload, status, delivered_at, last_error
                        ) VALUES (
                          :endpoint_id,
                          'submission.approved',
                          'submission',
                          :creator_id,
                          jsonb_build_object('api_key', 'legacy-payload-secret'),
                          'delivered',
                          now(),
                          'password=legacy-error-secret'
                        )
                        """
                    ),
                    {"endpoint_id": endpoint_id, "creator_id": creator_id},
                )
        finally:
            await engine.dispose()

        await asyncio.to_thread(_upgrade, "head")
        upgraded = True

        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT endpoint.status,
                                   endpoint.url,
                                   endpoint.secret_ciphertext,
                                   endpoint.subscribed_events,
                                   delivery.payload,
                                   convert_from(delivery.payload_body, 'UTF8') AS payload_body,
                                   delivery.last_error
                              FROM webhook_endpoints AS endpoint
                              JOIN webhook_deliveries AS delivery
                                ON delivery.endpoint_id = endpoint.id
                             WHERE endpoint.creator_id = :creator_id
                            """
                        ),
                        {"creator_id": creator_id},
                    )
                ).one()
                mcp_row = (
                    await connection.execute(
                        text(
                            """
                            SELECT status, request_data, response_data, error_message
                              FROM mcp_requests
                             WHERE id = :request_id
                            """
                        ),
                        {"request_id": mcp_request_id},
                    )
                ).one()
        finally:
            await engine.dispose()
    finally:
        if not upgraded:
            await asyncio.to_thread(_upgrade, "head")

    assert row.status == "disabled"
    assert row.url == "https://encrypted.invalid/"
    assert row.secret_ciphertext is None
    assert row.subscribed_events == ["verification.completed"]
    assert row.payload == {"reason": "legacy_delivery_scrubbed", "redacted": True}
    assert json.loads(row.payload_body) == {
        "reason": "legacy_delivery_scrubbed",
        "redacted": True,
    }
    assert row.last_error is None
    assert mcp_row.status == "succeeded"
    assert mcp_row.request_data == {
        "legacy_redacted": True,
        "creator_id": str(creator_id),
        "method": "create_task",
        "task_id": str(task_id),
    }
    assert mcp_row.response_data == {
        "legacy_redacted": True,
        "replayable": False,
        "task_id": str(task_id),
    }
    assert mcp_row.error_message is None
