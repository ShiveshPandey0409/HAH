from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import AsyncSessionFactory


async def seed_oauth_delegation(
    *,
    email: str,
    issuer: str = "https://issuer.example.com",
    subject: str | None = None,
    oauth_client_id: str = "agent-client",
    authorization_id: str | None = None,
) -> tuple[UUID, UUID, UUID]:
    async with AsyncSessionFactory() as session:
        user_id = await session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, can_create_tasks)
                VALUES (:email, 'OAuth Guard', true)
                RETURNING id
                """
            ),
            {"email": email},
        )
        identity_id = await session.scalar(
            text(
                """
                INSERT INTO oauth_identities (user_id, issuer, subject)
                VALUES (:user_id, :issuer, :subject)
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "issuer": issuer,
                "subject": subject or f"subject-{uuid4()}",
            },
        )
        delegation_id = await session.scalar(
            text(
                """
                INSERT INTO oauth_delegations (
                  identity_id, oauth_client_id, authorization_id, approved_scopes
                ) VALUES (
                  :identity_id, :oauth_client_id, :authorization_id,
                  ARRAY['mcp:access', 'tasks:create']
                )
                RETURNING id
                """
            ),
            {
                "identity_id": identity_id,
                "oauth_client_id": oauth_client_id,
                "authorization_id": authorization_id or f"authorization-{uuid4()}",
            },
        )
        await session.commit()

    assert user_id is not None
    assert identity_id is not None
    assert delegation_id is not None
    return user_id, identity_id, delegation_id


async def assert_rejected(
    statement: str,
    parameters: dict[str, object],
    *,
    sqlstate: str,
) -> None:
    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(text(statement), parameters)
        assert getattr(caught.value.orig, "sqlstate", None) == sqlstate


async def test_identity_mapping_is_exact_and_never_stores_tokens() -> None:
    user_id, identity_id, _ = await seed_oauth_delegation(
        email="oauth-identity-guard@example.com",
        issuer="https://issuer.example.com/tenant-a",
        subject="stable-subject",
    )

    await assert_rejected(
        """
        INSERT INTO oauth_identities (user_id, issuer, subject)
        VALUES (:user_id, 'https://issuer.example.com/tenant-a', 'stable-subject')
        """,
        {"user_id": user_id},
        sqlstate="23505",
    )

    async with AsyncSessionFactory() as session:
        other_identity_id = await session.scalar(
            text(
                """
                INSERT INTO oauth_identities (user_id, issuer, subject)
                VALUES (:user_id, 'https://issuer.example.com/tenant-b', 'stable-subject')
                RETURNING id
                """
            ),
            {"user_id": user_id},
        )
        columns = (
            await session.execute(
                text(
                    """
                    SELECT table_name, column_name
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name IN ('oauth_identities', 'oauth_delegations')
                    """
                )
            )
        ).all()
        await session.commit()

    assert identity_id != other_identity_id
    assert all("token" not in column_name for _, column_name in columns)


async def test_delegation_is_unique_per_identity_and_client() -> None:
    _, identity_id, _ = await seed_oauth_delegation(
        email="oauth-delegation-guard@example.com",
        oauth_client_id="shared-agent",
    )

    await assert_rejected(
        """
        INSERT INTO oauth_delegations (
          identity_id, oauth_client_id, authorization_id, approved_scopes
        ) VALUES (
          :identity_id, 'shared-agent', 'duplicate-authorization', ARRAY['mcp:access']
        )
        """,
        {"identity_id": identity_id},
        sqlstate="23505",
    )
    await assert_rejected(
        """
        INSERT INTO oauth_delegations (
          identity_id, oauth_client_id, authorization_id, approved_scopes
        ) VALUES (
          :identity_id, 'duplicate-scopes', 'duplicate-scopes-authorization',
          ARRAY['mcp:access', 'mcp:access']
        )
        """,
        {"identity_id": identity_id},
        sqlstate="23514",
    )
    for client_id, scopes in (
        ("missing-base-scope", "ARRAY['tasks:create']"),
        ("unsupported-scope", "ARRAY['mcp:access', 'payments:admin']"),
    ):
        await assert_rejected(
            f"""
            INSERT INTO oauth_delegations (
              identity_id, oauth_client_id, authorization_id, approved_scopes
            ) VALUES (
              :identity_id, :client_id, :authorization_id, {scopes}
            )
            """,
            {
                "identity_id": identity_id,
                "client_id": client_id,
                "authorization_id": f"{client_id}-authorization",
            },
            sqlstate="23514",
        )


async def test_identity_and_delegation_principal_bindings_are_immutable() -> None:
    user_id, identity_id, delegation_id = await seed_oauth_delegation(
        email="oauth-immutable-guard@example.com"
    )
    await assert_rejected(
        """
        UPDATE oauth_identities
           SET subject = 'replacement-subject'
         WHERE id = :identity_id
        """,
        {"identity_id": identity_id},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        UPDATE oauth_identities
           SET user_id = :replacement_user_id
         WHERE id = :identity_id
        """,
        {"identity_id": identity_id, "replacement_user_id": uuid4()},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET oauth_client_id = 'replacement-client'
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET identity_id = :replacement_identity_id
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id, "replacement_identity_id": uuid4()},
        sqlstate="HVL01",
    )

    async with AsyncSessionFactory() as session:
        stored_user_id = await session.scalar(
            text("SELECT user_id FROM oauth_identities WHERE id = :identity_id"),
            {"identity_id": identity_id},
        )
    assert stored_user_id == user_id

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE oauth_identities SET status = 'disabled' WHERE id = :identity_id"),
            {"identity_id": identity_id},
        )
        await session.commit()
    await assert_rejected(
        "UPDATE oauth_identities SET status = 'active' WHERE id = :identity_id",
        {"identity_id": identity_id},
        sqlstate="HVL01",
    )


async def test_authorization_changes_require_rotated_and_fresh_consent() -> None:
    _, _, delegation_id = await seed_oauth_delegation(
        email="oauth-consent-guard@example.com",
        authorization_id="authorization-grant-v1",
    )
    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET approved_scopes = ARRAY['mcp:access']
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET approved_scopes = ARRAY['mcp:access'],
               consent_version = consent_version + 1,
               consented_at = consented_at + interval '1 second'
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )

    async with AsyncSessionFactory() as session:
        approved = (
            await session.execute(
                text(
                    """
                    UPDATE oauth_delegations
                       SET approved_scopes = ARRAY['mcp:access'],
                           consent_version = consent_version + 1,
                           consented_at = consented_at + interval '1 second',
                           authorization_id = 'authorization-grant-v2'
                     WHERE id = :delegation_id
                    RETURNING consent_version, consented_at, updated_at
                    """
                ),
                {"delegation_id": delegation_id},
            )
        ).one()
        await session.commit()

    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET approved_scopes = ARRAY['mcp:access', 'tasks:create'],
               consent_version = consent_version + 1,
               consented_at = consented_at + interval '1 second',
               authorization_id = 'authorization-grant-v1'
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        DELETE FROM oauth_authorization_grants
         WHERE delegation_id = :delegation_id
           AND authorization_id = 'authorization-grant-v1'
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )

    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET status = 'disabled', revoked_at = consented_at + interval '1 second'
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )

    async with AsyncSessionFactory() as session:
        revoked = (
            await session.execute(
                text(
                    """
                    UPDATE oauth_delegations
                       SET status = 'disabled',
                           revoked_at = consented_at + interval '1 second',
                           consent_version = consent_version + 1
                     WHERE id = :delegation_id
                    RETURNING consent_version, consented_at
                    """
                ),
                {"delegation_id": delegation_id},
            )
        ).one()
        await session.commit()

    await assert_rejected(
        """
        UPDATE oauth_delegations
           SET status = 'active', revoked_at = NULL,
               consent_version = consent_version + 1
         WHERE id = :delegation_id
        """,
        {"delegation_id": delegation_id},
        sqlstate="HVL01",
    )

    async with AsyncSessionFactory() as session:
        reactivated = (
            await session.execute(
                text(
                    """
                    UPDATE oauth_delegations
                       SET status = 'active', revoked_at = NULL,
                           consent_version = consent_version + 1,
                           consented_at = consented_at + interval '1 second',
                           authorization_id = 'authorization-grant-v3'
                     WHERE id = :delegation_id
                    RETURNING status, consent_version, consented_at
                    """
                ),
                {"delegation_id": delegation_id},
            )
        ).one()
        touched_updated_at = await session.scalar(
            text(
                """
                UPDATE oauth_delegations
                   SET last_used_at = clock_timestamp()
                 WHERE id = :delegation_id
                RETURNING updated_at
                """
            ),
            {"delegation_id": delegation_id},
        )
        await session.commit()

    assert approved.consent_version == 2
    assert revoked.consent_version == 3
    assert reactivated.status == "active"
    assert reactivated.consent_version == 4
    assert revoked.consented_at == approved.consented_at
    assert reactivated.consented_at > revoked.consented_at
    assert touched_updated_at is not None
    assert touched_updated_at >= approved.updated_at


async def test_legacy_mcp_audit_inherits_client_owner_and_scopes() -> None:
    async with AsyncSessionFactory() as session:
        user_id = await session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, can_create_tasks)
                VALUES ('legacy-audit-guard@example.com', 'Legacy Audit Guard', true)
                RETURNING id
                """
            )
        )
        api_client_id = await session.scalar(
            text(
                """
                INSERT INTO api_clients (
                  creator_id, name, client_key, secret_hash, scopes
                ) VALUES (
                  :user_id, 'Legacy Client', :client_key, 'sha256:test',
                  ARRAY['tasks:create']
                )
                RETURNING id
                """
            ),
            {"user_id": user_id, "client_key": f"legacy-{uuid4()}"},
        )
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO mcp_requests (
                      api_client_id, method, idempotency_key
                    ) VALUES (
                      :api_client_id, 'create_task', 'legacy-request'
                    )
                    RETURNING actor_user_id, auth_scopes, oauth_consent_version,
                              oauth_authorization_id
                    """
                ),
                {"api_client_id": api_client_id},
            )
        ).one()
        await session.commit()

    assert row.actor_user_id == user_id
    assert row.auth_scopes == ["tasks:create"]
    assert row.oauth_consent_version is None
    assert row.oauth_authorization_id is None


async def test_oauth_mcp_audit_enforces_actor_scope_consent_and_idempotency() -> None:
    user_id, identity_id, delegation_id = await seed_oauth_delegation(
        email="oauth-request-guard@example.com"
    )
    async with AsyncSessionFactory() as session:
        second_delegation_id = await session.scalar(
            text(
                """
                INSERT INTO oauth_delegations (
                  identity_id, oauth_client_id, authorization_id, approved_scopes
                ) VALUES (
                  :identity_id, 'second-agent', 'second-agent-authorization',
                  ARRAY['mcp:access', 'tasks:create']
                )
                RETURNING id
                """
            ),
            {"identity_id": identity_id},
        )
        first_row = (
            await session.execute(
                text(
                    """
                    INSERT INTO mcp_requests (
                      oauth_delegation_id, actor_user_id, auth_scopes,
                      oauth_consent_version, method, idempotency_key
                    ) VALUES (
                      :delegation_id, :user_id, ARRAY['mcp:access', 'tasks:create'],
                      1, 'create_task', 'oauth-request'
                    )
                    RETURNING id, api_client_id, actor_user_id, auth_scopes,
                              oauth_authorization_id
                    """
                ),
                {"delegation_id": delegation_id, "user_id": user_id},
            )
        ).one()
        await session.execute(
            text(
                """
                INSERT INTO mcp_requests (
                  oauth_delegation_id, actor_user_id, auth_scopes,
                  oauth_consent_version, method, idempotency_key
                ) VALUES (
                  :delegation_id, :user_id, ARRAY['mcp:access', 'tasks:create'],
                  1, 'create_task', 'oauth-request'
                )
                """
            ),
            {"delegation_id": second_delegation_id, "user_id": user_id},
        )
        await session.commit()

    assert first_row.api_client_id is None
    assert first_row.actor_user_id == user_id
    assert first_row.auth_scopes == ["mcp:access", "tasks:create"]
    assert first_row.oauth_authorization_id is not None

    await assert_rejected(
        """
        UPDATE mcp_requests
           SET auth_scopes = ARRAY['mcp:access']
         WHERE id = :request_id
        """,
        {"request_id": first_row.id},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        UPDATE mcp_requests
           SET request_data = '{"tampered": true}'::jsonb
         WHERE id = :request_id
        """,
        {"request_id": first_row.id},
        sqlstate="HVL01",
    )

    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, method, idempotency_key
        ) VALUES (
          :delegation_id, :user_id, ARRAY['mcp:access', 'tasks:create'],
          1, 'create_task', 'oauth-request'
        )
        """,
        {"delegation_id": delegation_id, "user_id": user_id},
        sqlstate="23505",
    )
    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, method, idempotency_key
        ) VALUES (
          :delegation_id, :other_user_id, ARRAY['mcp:access', 'tasks:create'],
          1, 'create_task', 'wrong-actor'
        )
        """,
        {"delegation_id": delegation_id, "other_user_id": uuid4()},
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, method, idempotency_key
        ) VALUES (
          :delegation_id, :user_id, ARRAY['mcp:access', 'payments:execute'],
          1, 'create_task', 'unapproved-scope'
        )
        """,
        {"delegation_id": delegation_id, "user_id": user_id},
        sqlstate="HAU01",
    )
    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, oauth_authorization_id, method, idempotency_key
        ) VALUES (
          :delegation_id, :user_id, ARRAY['mcp:access', 'tasks:create'],
          1, 'stale-authorization-grant', 'create_task', 'stale-authorization'
        )
        """,
        {"delegation_id": delegation_id, "user_id": user_id},
        sqlstate="HAU01",
    )
    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, method, idempotency_key
        ) VALUES (
          :delegation_id, :user_id, ARRAY['mcp:access', 'tasks:create'],
          2, 'create_task', 'stale-consent'
        )
        """,
        {"delegation_id": delegation_id, "user_id": user_id},
        sqlstate="HAU01",
    )


async def test_mcp_audit_requires_exactly_one_authorization_source() -> None:
    user_id, _, delegation_id = await seed_oauth_delegation(email="oauth-source-guard@example.com")
    async with AsyncSessionFactory() as session:
        api_client_id = await session.scalar(
            text(
                """
                INSERT INTO api_clients (
                  creator_id, name, client_key, secret_hash, scopes
                ) VALUES (
                  :user_id, 'Source Guard', :client_key, 'sha256:test',
                  ARRAY['tasks:create']
                )
                RETURNING id
                """
            ),
            {"user_id": user_id, "client_key": f"source-{uuid4()}"},
        )
        await session.commit()

    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          api_client_id, oauth_delegation_id, actor_user_id, auth_scopes,
          oauth_consent_version, method, idempotency_key
        ) VALUES (
          :api_client_id, :delegation_id, :user_id, ARRAY['tasks:create'],
          1, 'create_task', 'dual-source'
        )
        """,
        {
            "api_client_id": api_client_id,
            "delegation_id": delegation_id,
            "user_id": user_id,
        },
        sqlstate="HVL01",
    )
    await assert_rejected(
        """
        INSERT INTO mcp_requests (
          actor_user_id, auth_scopes, method, idempotency_key
        ) VALUES (
          :user_id, ARRAY['tasks:create'], 'create_task', 'missing-source'
        )
        """,
        {"user_id": user_id},
        sqlstate="HVL01",
    )
