from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import func, select, text

from app.db.session import AsyncSessionFactory
from app.models.claim import BountyClaim, ClaimStatus
from app.models.submission import Submission, SubmissionProof, VerificationMethod
from app.schemas.submission import SubmissionCreate, VerificationCommand
from app.services.submissions import create_submission_and_commit, verify_submission_and_commit
from tests.test_marketplace import (
    add_social_account,
    bounty_ids_by_title,
    bounty_payload,
    claim,
    create_open_task,
    create_user,
)


async def claimed_work(
    client: AsyncClient,
    label: str,
    *,
    proof_requirements: list[str] | None = None,
) -> tuple[UUID, UUID, UUID]:
    creator_id = await create_user(
        client,
        email=f"submission-creator-{label}@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(
        client,
        email=f"submission-worker-{label}@example.com",
    )
    social_account_id = await add_social_account(freelancer_id)
    task = await create_open_task(
        client,
        creator_id,
        [
            bounty_payload(
                f"Submission bounty {label}",
                proof_requirements=proof_requirements or ["url"],
            )
        ],
    )
    bounty_id = bounty_ids_by_title(task)[f"Submission bounty {label}"]
    claim_response = await claim(client, bounty_id, freelancer_id, social_account_id)
    assert claim_response.status_code == 201, claim_response.text
    return creator_id, freelancer_id, UUID(claim_response.json()["id"])


def url_proof(url: str = "https://www.reddit.com/r/example/comments/abc/post/") -> dict[str, Any]:
    return {"proof_type": "url", "url": url}


def screenshot_proof() -> dict[str, Any]:
    return {
        "proof_type": "screenshot",
        "storage_key": f"proofs/{uuid4()}.png",
        "mime_type": "image/png",
        "sha256": "a" * 64,
    }


async def submit(
    client: AsyncClient,
    claim_id: UUID,
    freelancer_id: UUID,
    proofs: list[dict[str, Any]],
):
    return await client.post(
        f"/v1/claims/{claim_id}/submissions",
        json={"freelancer_id": str(freelancer_id), "proofs": proofs},
    )


async def verify(
    client: AsyncClient,
    submission_id: UUID,
    creator_id: UUID,
    *,
    result: str,
    checks: dict[str, Any] | None = None,
    failure_reason: str | None = None,
):
    return await client.post(
        f"/v1/submissions/{submission_id}/verification",
        json={
            "verifier_user_id": str(creator_id),
            "method": "manual",
            "result": result,
            "checks": checks or {},
            "failure_reason": failure_reason,
        },
    )


async def test_submission_requires_owner_complete_strict_proofs(client: AsyncClient) -> None:
    _, freelancer_id, claim_id = await claimed_work(
        client,
        "strict",
        proof_requirements=["url", "screenshot"],
    )

    wrong_owner = await submit(client, claim_id, uuid4(), [url_proof(), screenshot_proof()])
    incomplete = await submit(client, claim_id, freelancer_id, [url_proof()])
    insecure_url = await submit(
        client,
        claim_id,
        freelancer_id,
        [url_proof("http://example.com/proof"), screenshot_proof()],
    )
    duplicate = await submit(
        client,
        claim_id,
        freelancer_id,
        [url_proof(), url_proof("https://example.com/second")],
    )
    wrong_shape = await submit(
        client,
        claim_id,
        freelancer_id,
        [
            url_proof(),
            {
                "proof_type": "screenshot",
                "storage_key": "proofs/screenshot.png",
                "url": "https://example.com/unexpected",
            },
        ],
    )

    assert wrong_owner.status_code == 422
    assert incomplete.status_code == 422
    assert insecure_url.status_code == 422
    assert duplicate.status_code == 422
    assert wrong_shape.status_code == 422

    created = await submit(client, claim_id, freelancer_id, [url_proof(), screenshot_proof()])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["claim_id"] == str(claim_id)
    assert body["revision"] == 1
    assert body["verification_status"] == "pending"
    assert body["claim_status"] == "submitted"
    assert [proof["proof_type"] for proof in body["proofs"]] == ["url", "screenshot"]

    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Submission)) == 1
        assert await session.scalar(select(func.count()).select_from(SubmissionProof)) == 2


async def test_expired_claim_and_second_active_submission_conflict(client: AsyncClient) -> None:
    _, freelancer_id, claim_id = await claimed_work(client, "expired")
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

    expired = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert expired.status_code == 409

    _, active_freelancer_id, active_claim_id = await claimed_work(client, "active")
    first = await submit(client, active_claim_id, active_freelancer_id, [url_proof()])
    second = await submit(client, active_claim_id, active_freelancer_id, [url_proof()])
    assert first.status_code == 201
    assert second.status_code == 409


async def test_revision_increments_and_only_latest_can_be_verified(client: AsyncClient) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "revision")
    first = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert first.status_code == 201, first.text

    async with AsyncSessionFactory() as session:
        claim_row = await session.get(BountyClaim, claim_id)
        assert claim_row is not None
        claim_row.status = ClaimStatus.CHANGES_REQUESTED
        await session.commit()

    second = await submit(
        client,
        claim_id,
        freelancer_id,
        [url_proof("https://www.reddit.com/r/example/comments/revised/post/")],
    )
    assert second.status_code == 201, second.text
    assert second.json()["revision"] == 2

    stale = await verify(client, UUID(first.json()["id"]), creator_id, result="passed")
    latest = await verify(client, UUID(second.json()["id"]), creator_id, result="passed")
    assert stale.status_code == 409
    assert latest.status_code == 200, latest.text
    assert latest.json()["claim_status"] == "approved"


async def test_review_then_pass_is_shared_safe_and_finally_idempotent(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "review-pass")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    submission_id = UUID(created.json()["id"])

    review = await verify(
        client,
        submission_id,
        creator_id,
        result="review_required",
        checks={"provider_secret": "must-not-persist", "reachable": True},
        failure_reason="A person must review this proof",
    )
    assert review.status_code == 200, review.text
    assert review.json()["verification_status"] == "review_required"
    assert review.json()["claim_status"] == "reviewing"
    assert review.json()["checks"]["provider_secret"] == "[REDACTED]"

    passed = await verify(
        client,
        submission_id,
        creator_id,
        result="passed",
        checks={"reachable": True, "platform_matches": True},
    )
    replay = await verify(
        client,
        submission_id,
        creator_id,
        result="passed",
        checks={"reachable": True, "platform_matches": True},
    )
    conflict = await verify(
        client,
        submission_id,
        creator_id,
        result="failed",
        failure_reason="Changed after finalization",
    )

    assert passed.status_code == 200, passed.text
    assert replay.status_code == 200
    assert replay.json() == passed.json()
    assert passed.json()["claim_status"] == "approved"
    assert conflict.status_code == 409


async def test_failed_verification_rejects_claim_and_wrong_creator_is_hidden(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "failed")
    created = await submit(client, claim_id, freelancer_id, [url_proof()])
    submission_id = UUID(created.json()["id"])

    wrong_creator = await verify(client, submission_id, uuid4(), result="passed")
    missing_reason = await verify(client, submission_id, creator_id, result="failed")
    failed = await verify(
        client,
        submission_id,
        creator_id,
        result="failed",
        checks={"content_matches": False},
        failure_reason="The submitted content does not match the task",
    )

    assert wrong_creator.status_code == 404
    assert missing_reason.status_code == 422
    assert failed.status_code == 200, failed.text
    assert failed.json()["verification_status"] == "failed"
    assert failed.json()["claim_status"] == "rejected"

    async with AsyncSessionFactory() as session:
        payment_count = await session.scalar(text("SELECT count(*) FROM payments"))
    assert payment_count == 0


async def test_injected_event_sink_receives_only_safe_transactional_events(
    client: AsyncClient,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "event-sink")
    events: list[tuple[str, str, UUID, UUID, dict[str, Any]]] = []

    async def capture_event(
        session,
        event_type: str,
        entity_type: str,
        entity_id: UUID,
        event_creator_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        assert session.in_transaction()
        events.append((event_type, entity_type, entity_id, event_creator_id, payload))

    async with AsyncSessionFactory() as session:
        created = await create_submission_and_commit(
            session,
            claim_id,
            SubmissionCreate.model_validate(
                {
                    "freelancer_id": freelancer_id,
                    "proofs": [url_proof("https://example.com/private-proof-location")],
                }
            ),
            event_sink=capture_event,
        )

    assert [event[0] for event in events] == ["submission.created"]
    assert events[0][3] == creator_id
    assert "private-proof-location" not in str(events[0][4])

    async with AsyncSessionFactory() as session:
        reviewed = await verify_submission_and_commit(
            session,
            created.id,
            VerificationCommand(
                result="review_required",
                checks={"authorization": "must-not-leak"},
                failure_reason="A person must review this proof",
            ),
            method=VerificationMethod.MANUAL,
            verifier_user_id=creator_id,
            authorized_creator_id=creator_id,
            event_sink=capture_event,
        )
    assert reviewed.verification_status.value == "review_required"
    assert [event[0] for event in events] == ["submission.created"]

    async with AsyncSessionFactory() as session:
        approved = await verify_submission_and_commit(
            session,
            created.id,
            VerificationCommand(
                result="passed",
                checks={"authorization": "must-not-leak", "reachable": True},
            ),
            method=VerificationMethod.MANUAL,
            verifier_user_id=creator_id,
            authorized_creator_id=creator_id,
            event_sink=capture_event,
        )
    assert approved.claim_status == ClaimStatus.APPROVED
    assert [event[0] for event in events] == [
        "submission.created",
        "verification.completed",
    ]
    assert "must-not-leak" not in str(events[1][4])
