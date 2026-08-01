from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import BountyClaim, ClaimStatus
from app.models.submission import (
    Submission,
    SubmissionProof,
    VerificationMethod,
    VerificationStatus,
)
from app.models.task import Bounty, Task
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionProofResponse,
    SubmissionResponse,
    VerificationCommand,
)

SubmissionEventSink = Callable[
    [AsyncSession, str, str, UUID, UUID, dict[str, Any]],
    Awaitable[None],
]

_SUBMITTABLE_STATUSES = frozenset(
    {
        ClaimStatus.CLAIMED,
        ClaimStatus.CHANGES_REQUESTED,
    }
)
_FINAL_VERIFICATION_STATUSES = frozenset(
    {
        VerificationStatus.PASSED,
        VerificationStatus.FAILED,
    }
)


class SubmissionNotFoundError(Exception):
    pass


class SubmissionConflictError(Exception):
    pass


class SubmissionValidationError(Exception):
    pass


def _sqlstate(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


def _constraint_name(error: DBAPIError) -> str | None:
    direct = getattr(error.orig, "constraint_name", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)


def _translate_database_error(error: DBAPIError) -> Exception | None:
    sqlstate = _sqlstate(error)
    if sqlstate == "HNF01":
        return SubmissionNotFoundError("Resource not found")
    if sqlstate == "HCF01" or (
        sqlstate == "23505" and _constraint_name(error) == "submissions_claim_id_revision_key"
    ):
        return SubmissionConflictError("Submission state changed concurrently")
    if (
        sqlstate == "HVL01"
        or sqlstate == "23514"
        or (
            sqlstate == "23505"
            and _constraint_name(error) == "submission_proofs_submission_id_kind_key"
        )
    ):
        return SubmissionValidationError("Submission violates a proof or state rule")
    return None


async def _default_event_sink(
    session: AsyncSession,
    event_type: str,
    entity_type: str,
    entity_id: UUID,
    creator_id: UUID,
    payload: dict[str, Any],
) -> None:
    try:
        from app.schemas.webhook import (
            SubmissionCreatedData,
            VerificationCompletedData,
            WebhookEventType,
        )
        from app.services.webhooks import enqueue_webhook_event
    except ModuleNotFoundError as error:
        if error.name == "app.services.webhooks":
            return
        raise

    webhook_event_type = WebhookEventType(event_type)
    if webhook_event_type == WebhookEventType.SUBMISSION_CREATED:
        data = SubmissionCreatedData.model_validate(payload)
    elif webhook_event_type == WebhookEventType.VERIFICATION_COMPLETED:
        data = VerificationCompletedData.model_validate(payload)
    else:
        raise ValueError("unsupported submission webhook event")
    await enqueue_webhook_event(
        session,
        creator_id=creator_id,
        event_type=webhook_event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        data=data,
        deduplication_key=f"{event_type}:{entity_id}",
    )


async def _emit_event(
    event_sink: SubmissionEventSink | None,
    session: AsyncSession,
    event_type: str,
    entity_type: str,
    entity_id: UUID,
    creator_id: UUID,
    payload: dict[str, Any],
) -> None:
    await (event_sink or _default_event_sink)(
        session,
        event_type,
        entity_type,
        entity_id,
        creator_id,
        payload,
    )


async def _load_proofs(
    session: AsyncSession,
    submission_id: UUID,
) -> list[SubmissionProof]:
    return list(
        (
            await session.scalars(
                select(SubmissionProof)
                .where(SubmissionProof.submission_id == submission_id)
                .order_by(
                    case(
                        (SubmissionProof.kind == "url", 0),
                        (SubmissionProof.kind == "screenshot", 1),
                        else_=2,
                    ),
                    SubmissionProof.id,
                )
            )
        ).all()
    )


async def _submission_response(
    session: AsyncSession,
    submission: Submission,
    claim: BountyClaim,
) -> SubmissionResponse:
    proofs = await _load_proofs(session, submission.id)
    return SubmissionResponse(
        id=submission.id,
        claim_id=submission.claim_id,
        revision=submission.revision,
        proofs=[
            SubmissionProofResponse(
                id=proof.id,
                proof_type=proof.kind,
                url=proof.external_url,
                storage_key=proof.storage_key,
                mime_type=proof.mime_type,
                sha256=proof.sha256,
            )
            for proof in proofs
        ],
        verification_method=submission.verification_method,
        verification_status=submission.verification_status,
        checks=submission.verification_checks,
        verifier_user_id=submission.verifier_user_id,
        failure_reason=submission.verification_note,
        claim_status=claim.status,
        submitted_at=submission.submitted_at,
        verified_at=submission.verified_at,
        updated_at=submission.updated_at,
    )


async def _load_creator_id(
    session: AsyncSession,
    bounty: Bounty,
) -> UUID:
    creator_id = await session.scalar(select(Task.creator_id).where(Task.id == bounty.task_id))
    if creator_id is None:
        raise SubmissionNotFoundError("Task not found")
    return creator_id


async def create_submission(
    session: AsyncSession,
    claim_id: UUID,
    data: SubmissionCreate,
    *,
    event_sink: SubmissionEventSink | None = None,
) -> SubmissionResponse:
    claim = await session.scalar(
        select(BountyClaim).where(BountyClaim.id == claim_id).with_for_update()
    )
    if claim is None:
        raise SubmissionNotFoundError("Claim not found")
    if claim.freelancer_id != data.freelancer_id:
        raise SubmissionValidationError("Claim is assigned to another freelancer")
    if claim.status not in _SUBMITTABLE_STATUSES:
        raise SubmissionConflictError("Claim cannot accept a submission")
    if (
        claim.status == ClaimStatus.CLAIMED
        and claim.claim_expires_at is not None
        and claim.claim_expires_at <= datetime.now(UTC)
    ):
        raise SubmissionConflictError("Claim reservation has expired")

    bounty = await session.scalar(select(Bounty).where(Bounty.id == claim.bounty_id))
    if bounty is None:
        raise SubmissionNotFoundError("Bounty not found")

    supplied_types = {proof.proof_type for proof in data.proofs}
    missing_types = set(bounty.proof_requirements) - supplied_types
    if missing_types:
        raise SubmissionValidationError("Submission is missing a required proof type")

    latest_revision = await session.scalar(
        select(func.max(Submission.revision)).where(Submission.claim_id == claim.id)
    )
    if claim.status == ClaimStatus.CLAIMED and latest_revision is not None:
        raise SubmissionConflictError("Claim already has a submission")
    if claim.status == ClaimStatus.CHANGES_REQUESTED and latest_revision is None:
        raise SubmissionConflictError("Claim has no submission to revise")

    submission = Submission(
        claim_id=claim.id,
        revision=(latest_revision or 0) + 1,
        verification_status=VerificationStatus.PENDING,
        verification_checks={},
    )
    session.add(submission)

    try:
        await session.flush()
        for proof in data.proofs:
            session.add(
                SubmissionProof(
                    submission_id=submission.id,
                    kind=proof.proof_type,
                    external_url=proof.url,
                    storage_key=proof.storage_key,
                    mime_type=proof.mime_type,
                    sha256=proof.sha256,
                    proof_metadata={},
                )
            )
        await session.flush()
        await session.refresh(submission)
        await session.refresh(claim)
    except DBAPIError as error:
        translated = _translate_database_error(error)
        if translated is not None:
            raise translated from error
        raise

    creator_id = await _load_creator_id(session, bounty)
    response = await _submission_response(session, submission, claim)
    await _emit_event(
        event_sink,
        session,
        "submission.created",
        "submission",
        submission.id,
        creator_id,
        {
            "submission_id": str(submission.id),
            "claim_id": str(claim.id),
            "bounty_id": str(bounty.id),
            "task_id": str(bounty.task_id),
            "freelancer_id": str(claim.freelancer_id),
            "revision": submission.revision,
            "submitted_at": submission.submitted_at.isoformat(),
            "proof_types": [proof.proof_type for proof in data.proofs],
        },
    )
    return response


async def create_submission_and_commit(
    session: AsyncSession,
    claim_id: UUID,
    data: SubmissionCreate,
    *,
    event_sink: SubmissionEventSink | None = None,
) -> SubmissionResponse:
    try:
        response = await create_submission(
            session,
            claim_id,
            data,
            event_sink=event_sink,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def _locked_verification_rows(
    session: AsyncSession,
    submission_id: UUID,
) -> tuple[Submission, BountyClaim, Bounty, UUID]:
    claim_id = await session.scalar(
        select(Submission.claim_id).where(Submission.id == submission_id)
    )
    if claim_id is None:
        raise SubmissionNotFoundError("Submission not found")

    claim = await session.scalar(
        select(BountyClaim).where(BountyClaim.id == claim_id).with_for_update()
    )
    if claim is None:
        raise SubmissionNotFoundError("Claim not found")
    submission = await session.scalar(
        select(Submission).where(Submission.id == submission_id).with_for_update()
    )
    if submission is None:
        raise SubmissionNotFoundError("Submission not found")

    latest_revision = await session.scalar(
        select(func.max(Submission.revision)).where(Submission.claim_id == claim.id)
    )
    if submission.revision != latest_revision:
        raise SubmissionConflictError("Only the latest submission revision can be verified")

    bounty = await session.scalar(select(Bounty).where(Bounty.id == claim.bounty_id))
    if bounty is None:
        raise SubmissionNotFoundError("Bounty not found")
    creator_id = await _load_creator_id(session, bounty)
    return submission, claim, bounty, creator_id


def _verification_is_exact_replay(
    submission: Submission,
    claim: BountyClaim,
    *,
    target_status: VerificationStatus,
    method: VerificationMethod,
    command: VerificationCommand,
    verifier_user_id: UUID | None,
) -> bool:
    expected_claim_status = {
        VerificationStatus.PASSED: ClaimStatus.APPROVED,
        VerificationStatus.FAILED: ClaimStatus.REJECTED,
        VerificationStatus.REVIEW_REQUIRED: ClaimStatus.REVIEWING,
    }[target_status]
    return (
        submission.verification_status == target_status
        and submission.verification_method == method
        and submission.verification_checks == command.checks
        and submission.verification_note == command.failure_reason
        and submission.verifier_user_id == verifier_user_id
        and (
            (target_status == VerificationStatus.REVIEW_REQUIRED and submission.verified_at is None)
            or (
                target_status in _FINAL_VERIFICATION_STATUSES and submission.verified_at is not None
            )
        )
        and claim.status == expected_claim_status
    )


async def verify_submission(
    session: AsyncSession,
    submission_id: UUID,
    command: VerificationCommand,
    *,
    method: VerificationMethod,
    verifier_user_id: UUID | None = None,
    authorized_creator_id: UUID | None = None,
    event_sink: SubmissionEventSink | None = None,
) -> SubmissionResponse:
    submission, claim, bounty, creator_id = await _locked_verification_rows(
        session,
        submission_id,
    )

    if authorized_creator_id is not None and authorized_creator_id != creator_id:
        raise SubmissionNotFoundError("Submission not found")
    if method in {VerificationMethod.MANUAL, VerificationMethod.MCP}:
        if verifier_user_id is None:
            raise SubmissionValidationError("Verifier identity is required")
        if verifier_user_id != creator_id:
            raise SubmissionNotFoundError("Submission not found")
    elif verifier_user_id is not None and verifier_user_id != creator_id:
        raise SubmissionValidationError("Automatic verifier identity is invalid")

    target_status = VerificationStatus(command.result)
    if submission.verification_status in _FINAL_VERIFICATION_STATUSES:
        if _verification_is_exact_replay(
            submission,
            claim,
            target_status=target_status,
            method=method,
            command=command,
            verifier_user_id=verifier_user_id,
        ):
            return await _submission_response(session, submission, claim)
        raise SubmissionConflictError("Verification already has a final result")

    if submission.verification_status == VerificationStatus.REVIEW_REQUIRED:
        if target_status == VerificationStatus.REVIEW_REQUIRED:
            if _verification_is_exact_replay(
                submission,
                claim,
                target_status=target_status,
                method=method,
                command=command,
                verifier_user_id=verifier_user_id,
            ):
                return await _submission_response(session, submission, claim)
            raise SubmissionConflictError("Review-required verification already exists")
        if claim.status != ClaimStatus.REVIEWING:
            raise SubmissionConflictError("Claim is not awaiting review")
    elif submission.verification_status == VerificationStatus.PENDING:
        if claim.status != ClaimStatus.SUBMITTED:
            raise SubmissionConflictError("Claim is not awaiting verification")
    else:
        raise SubmissionConflictError("Submission cannot be verified from its current state")

    if target_status == VerificationStatus.PASSED:
        submitted_types = {proof.kind for proof in await _load_proofs(session, submission.id)}
        if set(bounty.proof_requirements) - submitted_types:
            raise SubmissionValidationError("Submission is missing a required proof type")

    submission.verification_method = method
    submission.verification_status = target_status
    submission.verification_checks = command.checks
    submission.verifier_user_id = verifier_user_id
    submission.verification_note = command.failure_reason
    submission.verified_at = (
        None if target_status == VerificationStatus.REVIEW_REQUIRED else datetime.now(UTC)
    )

    try:
        await session.flush()
        await session.refresh(submission)
        await session.refresh(claim)
    except DBAPIError as error:
        translated = _translate_database_error(error)
        if translated is not None:
            raise translated from error
        raise

    response = await _submission_response(session, submission, claim)
    if target_status in _FINAL_VERIFICATION_STATUSES:
        await _emit_event(
            event_sink,
            session,
            "verification.completed",
            "submission",
            submission.id,
            creator_id,
            {
                "submission_id": str(submission.id),
                "claim_id": str(claim.id),
                "bounty_id": str(bounty.id),
                "task_id": str(bounty.task_id),
                "revision": submission.revision,
                "method": method.value,
                "status": target_status.value,
                "verified_at": submission.verified_at.isoformat(),
                "reason_code": (
                    "verification_failed" if target_status == VerificationStatus.FAILED else None
                ),
            },
        )
    return response


async def verify_submission_and_commit(
    session: AsyncSession,
    submission_id: UUID,
    command: VerificationCommand,
    *,
    method: VerificationMethod,
    verifier_user_id: UUID | None = None,
    authorized_creator_id: UUID | None = None,
    event_sink: SubmissionEventSink | None = None,
) -> SubmissionResponse:
    try:
        response = await verify_submission(
            session,
            submission_id,
            command,
            method=method,
            verifier_user_id=verifier_user_id,
            authorized_creator_id=authorized_creator_id,
            event_sink=event_sink,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise
