from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import AsyncSessionFactory
from tests.test_submissions import claimed_work, submit, url_proof, verify


def sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def test_database_rejects_expired_direct_submission(client: AsyncClient) -> None:
    _, _, claim_id = await claimed_work(client, "db-expired")
    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                UPDATE bounty_claims
                   SET claimed_at = :claimed_at,
                       claim_expires_at = :expires_at
                 WHERE id = :claim_id
                """
            ),
            {
                "claim_id": claim_id,
                "claimed_at": datetime.now(UTC) - timedelta(hours=2),
                "expires_at": datetime.now(UTC) - timedelta(hours=1),
            },
        )
        await session.commit()

        with pytest.raises(DBAPIError) as captured:
            await session.execute(
                text("INSERT INTO submissions (claim_id) VALUES (:claim_id)"),
                {"claim_id": claim_id},
            )
        assert sqlstate(captured.value) == "HCF01"
        await session.rollback()


async def test_database_requires_new_submissions_to_start_pending(client: AsyncClient) -> None:
    creator_id, _, claim_id = await claimed_work(client, "db-initial-state")
    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as captured:
            await session.execute(
                text(
                    """
                    INSERT INTO submissions (
                      claim_id, verification_method, verification_status,
                      verifier_user_id, verified_at
                    ) VALUES (
                      :claim_id, 'manual', 'passed', :creator_id, now()
                    )
                    """
                ),
                {"claim_id": claim_id, "creator_id": creator_id},
            )
        assert sqlstate(captured.value) == "HVL01"
        await session.rollback()


async def test_database_enforces_unique_strict_proof_shape(client: AsyncClient) -> None:
    _, freelancer_id, claim_id = await claimed_work(client, "db-proof")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as duplicate:
            await session.execute(
                text(
                    """
                    INSERT INTO submission_proofs (submission_id, kind, external_url)
                    VALUES (:submission_id, 'url', 'https://example.com/duplicate')
                    """
                ),
                {"submission_id": submission_id},
            )
        assert sqlstate(duplicate.value) == "23505"
        await session.rollback()

        with pytest.raises(DBAPIError) as insecure:
            await session.execute(
                text(
                    """
                    INSERT INTO submission_proofs (submission_id, kind, external_url)
                    VALUES (:submission_id, 'url', 'http://example.com/insecure')
                    """
                ),
                {"submission_id": submission_id},
            )
        assert sqlstate(insecure.value) == "23514"
        await session.rollback()

        with pytest.raises(DBAPIError) as ambiguous:
            await session.execute(
                text(
                    """
                    INSERT INTO submission_proofs (
                      submission_id, kind, external_url, storage_key
                    ) VALUES (
                      :submission_id, 'image', 'https://example.com/image', 'proofs/image.png'
                    )
                    """
                ),
                {"submission_id": submission_id},
            )
        assert sqlstate(ambiguous.value) == "23514"
        await session.rollback()


async def test_legacy_invalid_proof_cannot_be_approved(client: AsyncClient) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "db-legacy-proof")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])

    async with AsyncSessionFactory() as session:
        await session.execute(
            text(
                """
                ALTER TABLE submission_proofs
                  DROP CONSTRAINT submission_proofs_https_url_check
                """
            )
        )
        await session.execute(
            text(
                """
                UPDATE submission_proofs
                   SET external_url = 'http://legacy.example.com/proof'
                 WHERE submission_id = :submission_id
                """
            ),
            {"submission_id": submission_id},
        )
        await session.execute(
            text(
                """
                ALTER TABLE submission_proofs
                  ADD CONSTRAINT submission_proofs_https_url_check
                  CHECK (
                    external_url IS NULL
                    OR external_url ~* '^https://[^[:space:]]+$'
                  ) NOT VALID
                """
            )
        )
        await session.commit()

    rejected = await verify(client, submission_id, creator_id, result="passed")
    assert rejected.status_code == 422, rejected.text


async def test_database_prevents_terminal_verification_rewrite(client: AsyncClient) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "db-final")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])
    passed = await verify(client, submission_id, creator_id, result="passed")
    assert passed.status_code == 200, passed.text

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as captured:
            await session.execute(
                text(
                    """
                    UPDATE submissions
                       SET verification_status = 'failed',
                           verification_note = 'late rewrite',
                           verified_at = now()
                     WHERE id = :submission_id
                    """
                ),
                {"submission_id": submission_id},
            )
        assert sqlstate(captured.value) == "HCF01"
        await session.rollback()

        with pytest.raises(DBAPIError) as identity_change:
            await session.execute(
                text(
                    """
                    UPDATE submissions
                       SET revision = revision + 1
                     WHERE id = :submission_id
                    """
                ),
                {"submission_id": submission_id},
            )
        assert sqlstate(identity_change.value) == "HCF01"
        await session.rollback()


async def test_database_prevents_moving_proof_between_submissions(client: AsyncClient) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "db-proof-owner-old")
    first = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert first.status_code == 201, first.text
    first_submission_id = UUID(first.json()["id"])
    first_proof_id = UUID(first.json()["proofs"][0]["id"])
    passed = await verify(client, first_submission_id, creator_id, result="passed")
    assert passed.status_code == 200, passed.text

    _, second_freelancer_id, second_claim_id = await claimed_work(
        client,
        "db-proof-owner-new",
    )
    second = await submit(client, second_claim_id, second_freelancer_id, [url_proof()])
    assert second.status_code == 201, second.text
    second_submission_id = UUID(second.json()["id"])

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as captured:
            await session.execute(
                text(
                    """
                    UPDATE submission_proofs
                       SET submission_id = :second_submission_id
                     WHERE id = :proof_id
                    """
                ),
                {
                    "second_submission_id": second_submission_id,
                    "proof_id": first_proof_id,
                },
            )
        assert sqlstate(captured.value) == "HCF01"
        await session.rollback()


async def test_database_rejects_verification_when_claim_cannot_transition(
    client: AsyncClient,
) -> None:
    _, freelancer_id, claim_id = await claimed_work(client, "db-transition")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert created.status_code == 201, created.text
    submission_id = UUID(created.json()["id"])

    async with AsyncSessionFactory() as session:
        await session.execute(
            text("UPDATE bounty_claims SET status = 'cancelled' WHERE id = :claim_id"),
            {"claim_id": claim_id},
        )
        await session.commit()

        with pytest.raises(DBAPIError) as captured:
            await session.execute(
                text(
                    """
                    UPDATE submissions
                       SET verification_method = 'manual',
                           verification_status = 'passed',
                           verifier_user_id = (
                             SELECT t.creator_id
                               FROM bounty_claims c
                               JOIN bounties b ON b.id = c.bounty_id
                               JOIN tasks t ON t.id = b.task_id
                              WHERE c.id = :claim_id
                           ),
                           verified_at = now()
                     WHERE id = :submission_id
                    """
                ),
                {"claim_id": claim_id, "submission_id": submission_id},
            )
        assert sqlstate(captured.value) == "HCF01"
        await session.rollback()
