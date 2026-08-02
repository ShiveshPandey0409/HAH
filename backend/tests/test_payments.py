from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import AsyncClient
from mcp import Client
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1.routes import payments as payment_routes
from app.db.session import AsyncSessionFactory
from app.main import app
from app.mcp import server as mcp_server
from app.mcp.oauth import use_oauth_principal
from app.models.claim import BountyClaim, ClaimStatus
from app.models.payment import Payment, PaymentAttempt, WalletEntry
from app.models.task import Bounty
from app.services import submissions as submission_service
from app.services.api_clients import PAYMENTS_READ_SCOPE, PAYMENTS_WRITE_SCOPE
from app.services.payments import (
    HTTPPravaGateway,
    PaymentRuntime,
    PaymentWorkerPolicy,
    PravaGatewayError,
    PravaMandate,
    PravaMandateSession,
    PravaPaymentResult,
    process_next_payment,
)
from tests.test_marketplace import (
    add_social_account,
    bounty_ids_by_title,
    bounty_payload,
    claim,
    create_open_task,
    create_user,
)
from tests.test_mcp_create_task import issue_client
from tests.test_submissions import claimed_work, submit, url_proof, verify
from tests.test_tasks import task_payload


class FakePravaGateway:
    def __init__(self) -> None:
        self.created_customers: list[str] = []
        self.approved_amounts: dict[str, str] = {}
        self.charge_references: list[str] = []
        self.failures: list[PravaGatewayError] = []

    async def create_mandate_session(self, **kwargs) -> PravaMandateSession:
        self.created_customers.append(kwargs["customer_id"])
        self.approved_amounts[kwargs["customer_id"]] = kwargs["amount"]
        return PravaMandateSession(
            session_id=f"sess_{len(self.created_customers)}",
            approval_url="https://sandbox.collect.prava.space/approve-demo",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def list_mandates(self, *, customer_id: str) -> list[PravaMandate]:
        assert customer_id in self.created_customers
        return [
            PravaMandate(
                mandate_id=f"mdt_hah_test_{self.created_customers.index(customer_id) + 1}",
                status="active",
                state="available",
                approved_amount=self.approved_amounts[customer_id],
                currency="USD",
                valid_until=datetime.now(UTC) + timedelta(days=30),
                updated_at=datetime.now(UTC),
            )
        ]

    async def execute_sandbox_payment(self, **kwargs) -> PravaPaymentResult:
        self.charge_references.append(kwargs["reference"])
        if self.failures:
            raise self.failures.pop(0)
        return PravaPaymentResult(
            transaction_id="txn_hah_test",
            order_id="ord_hah_test",
            status="completed",
            deduplicated=len(self.charge_references) > 1,
            visa_confirmation="SUCCESS",
        )


def _http_prava_gateway() -> HTTPPravaGateway:
    return HTTPPravaGateway(
        base_url="https://sandbox.api.prava.space",
        secret_key=SecretStr("sk_test_unit"),
        timeout_seconds=5,
    )


async def _create_http_prava_session(gateway: HTTPPravaGateway) -> PravaMandateSession:
    return await gateway.create_mandate_session(
        customer_id="payer-1",
        customer_email="payer@example.com",
        amount="5.00",
        currency="USD",
        merchant_name="Hire a Human",
        merchant_url="https://hah-api-prava.onrender.com",
        merchant_country="IN",
        task_title="Contract regression",
        max_charges=1,
    )


async def test_http_prava_session_accepts_omitted_authorize_only(monkeypatch) -> None:
    gateway = _http_prava_gateway()

    async def response_without_flag(*args, **kwargs):
        del args, kwargs
        return {
            "session_id": "ses_live_contract",
            "iframe_url": "https://sandbox.collect.prava.space/session",
            "expires_at": "2026-08-02T12:16:53.592Z",
        }

    monkeypatch.setattr(gateway, "_request", response_without_flag)
    session = await _create_http_prava_session(gateway)
    assert session.session_id == "ses_live_contract"


async def test_http_prava_session_rejects_explicit_non_authorize_flow(monkeypatch) -> None:
    gateway = _http_prava_gateway()

    async def response_with_false_flag(*args, **kwargs):
        del args, kwargs
        return {
            "session_id": "ses_wrong_flow",
            "iframe_url": "https://sandbox.collect.prava.space/session",
            "expires_at": "2026-08-02T12:16:53.592Z",
            "authorizeOnly": False,
        }

    monkeypatch.setattr(gateway, "_request", response_with_false_flag)
    with pytest.raises(PravaGatewayError, match="Prava sandbox request failed"):
        await _create_http_prava_session(gateway)


def payment_runtime(gateway: FakePravaGateway) -> PaymentRuntime:
    return PaymentRuntime(
        gateway=gateway,
        merchant_name="Hire a Human",
        merchant_url="https://hah-api-prava.onrender.com",
        merchant_country="IN",
        payer_user_id="universal-hackathon-payer",
        payer_email="payer@example.com",
        policy=PaymentWorkerPolicy(
            max_attempts=3,
            lease_seconds=30,
            retry_base_seconds=1,
            retry_cap_seconds=2,
            poll_interval_seconds=0.01,
        ),
    )


async def _task_id_for_claim(claim_id: UUID) -> UUID:
    async with AsyncSessionFactory() as session:
        claim = await session.get(BountyClaim, claim_id)
        assert claim is not None
        bounty = await session.get(Bounty, claim.bounty_id)
        assert bounty is not None
        return bounty.task_id


async def _authorize_task(
    client: AsyncClient,
    *,
    task_id: UUID,
    creator_id: UUID,
    runtime: PaymentRuntime,
    monkeypatch,
) -> None:
    monkeypatch.setattr(payment_routes, "runtime_from_settings", lambda: runtime)
    started = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert started.status_code == 201, started.text
    assert started.json()["status"] == "pending"
    assert started.json()["approval_url"].startswith("https://sandbox.collect.prava.space/")

    refreshed = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization/refresh",
        headers=client.auth_headers(creator_id),
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["status"] == "active"
    assert refreshed.json()["provider_authorization_ref"].startswith("mdt_hah_test_")
    assert refreshed.json()["funding_status"] == "created"
    assert refreshed.json()["approval_url"] is None


async def test_prava_authorization_is_owned_and_never_returns_card_data(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "payment-auth")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)

    await _authorize_task(
        client,
        task_id=task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    read = await client.get(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    forbidden = await client.get(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(freelancer_id),
    )
    assert read.status_code == 200
    assert forbidden.status_code == 404
    serialized = read.text.lower()
    assert "card_number" not in serialized
    assert "dynamiccvv" not in serialized
    assert '"cvv"' not in serialized


async def test_verified_submission_auto_pays_once_and_credits_internal_wallet(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "payment-success")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    await _authorize_task(
        client,
        task_id=task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        submission_service,
        "get_settings",
        lambda: SimpleNamespace(prava_payment_automation_enabled=True),
    )

    submitted = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert submitted.status_code == 201, submitted.text
    submission_id = UUID(submitted.json()["id"])
    approved = await verify(
        client,
        submission_id,
        creator_id,
        result="passed",
        checks={"proof_reviewed": True},
    )
    assert approved.status_code == 200, approved.text

    pending = await client.get(
        f"/v1/submissions/{submission_id}/payment",
        headers=client.auth_headers(creator_id),
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "created"
    payment_id = UUID(pending.json()["id"])

    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True
    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is False

    paid = await client.get(
        f"/v1/payments/{payment_id}",
        headers=client.auth_headers(freelancer_id),
    )
    wallet = await client.get(
        "/v1/wallet",
        headers=client.auth_headers(freelancer_id),
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "succeeded"
    assert paid.json()["attempt_count"] == 1
    assert wallet.status_code == 200
    assert wallet.json()["redeemable"] is False
    assert wallet.json()["balances"] == [{"currency": "USD", "balance_minor": 1_000}]
    assert wallet.json()["entries"][0]["payment_id"] == str(payment_id)
    creator_wallet = await client.get(
        "/v1/wallet",
        headers=client.auth_headers(creator_id),
    )
    assert creator_wallet.status_code == 200
    assert creator_wallet.json()["balances"] == []

    replay = await verify(
        client,
        submission_id,
        creator_id,
        result="passed",
        checks={"proof_reviewed": True},
    )
    assert replay.status_code == 200, replay.text
    assert gateway.charge_references == [f"hah-task-funding:{task_id}"]

    async with AsyncSessionFactory() as session:
        claim_row = await session.get(BountyClaim, claim_id)
        assert claim_row is not None
        assert claim_row.status == ClaimStatus.PAID
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(WalletEntry)) == 1


async def test_retryable_prava_failure_does_not_credit_wallet_before_success(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "payment-retry")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    gateway.failures.append(PravaGatewayError("PRAVA_UNAVAILABLE", retryable=True))
    runtime = payment_runtime(gateway)
    await _authorize_task(
        client,
        task_id=task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        submission_service,
        "get_settings",
        lambda: SimpleNamespace(prava_payment_automation_enabled=True),
    )
    submitted = await submit(client, claim_id, freelancer_id, [url_proof()])
    submission_id = UUID(submitted.json()["id"])
    approved = await verify(client, submission_id, creator_id, result="passed")
    assert approved.status_code == 200, approved.text

    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True
    wallet_before = await client.get(
        "/v1/wallet",
        headers=client.auth_headers(freelancer_id),
    )
    assert wallet_before.json()["balances"] == []

    async with AsyncSessionFactory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.submission_id == submission_id)
        )
        assert payment is not None
        payment.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True
    wallet_after = await client.get(
        "/v1/wallet",
        headers=client.auth_headers(freelancer_id),
    )
    assert wallet_after.json()["balances"] == [{"currency": "USD", "balance_minor": 1_000}]
    async with AsyncSessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttempt)) == 2
        assert await session.scalar(select(func.count()).select_from(WalletEntry)) == 1


async def test_one_prava_task_funding_charge_backs_multiple_verified_wallet_credits(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id = await create_user(
        client,
        email="payment-multi-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    workers: list[tuple[UUID, UUID]] = []
    for number in (1, 2):
        worker_id = await create_user(
            client,
            email=f"payment-multi-worker-{number}@example.com",
        )
        workers.append((worker_id, await add_social_account(worker_id)))

    task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Two-person reward", slot_count=2)],
    )
    task_id = UUID(task["id"])
    bounty_id = bounty_ids_by_title(task)["Two-person reward"]
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    await _authorize_task(
        client,
        task_id=task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        submission_service,
        "get_settings",
        lambda: SimpleNamespace(prava_payment_automation_enabled=True),
    )

    payment_ids: list[UUID] = []
    for number, (worker_id, social_account_id) in enumerate(workers, start=1):
        claimed = await claim(client, bounty_id, worker_id, social_account_id)
        assert claimed.status_code == 201, claimed.text
        submitted = await submit(
            client,
            UUID(claimed.json()["id"]),
            worker_id,
            [url_proof(f"https://example.com/proof-{number}")],
        )
        assert submitted.status_code == 201, submitted.text
        submission_id = UUID(submitted.json()["id"])
        approved = await verify(client, submission_id, creator_id, result="passed")
        assert approved.status_code == 200, approved.text
        payment = await client.get(
            f"/v1/submissions/{submission_id}/payment",
            headers=client.auth_headers(creator_id),
        )
        payment_ids.append(UUID(payment.json()["id"]))

    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True
    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True
    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is False
    assert gateway.charge_references == [f"hah-task-funding:{task_id}"]

    for worker_id, _ in workers:
        wallet = await client.get(
            "/v1/wallet",
            headers=client.auth_headers(worker_id),
        )
        assert wallet.json()["balances"] == [{"currency": "USD", "balance_minor": 1_000}]
    async with AsyncSessionFactory() as session:
        payments = list(
            (await session.scalars(select(Payment).where(Payment.id.in_(payment_ids)))).all()
        )
        assert {payment.status.value for payment in payments} == {"succeeded"}
        assert sum(payment.provider_transaction_ref is not None for payment in payments) == 1


async def test_each_task_gets_its_own_approval_and_reports_other_blocked_budget(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id = await create_user(
        client,
        email="payment-reservations-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("First reservation")],
    )
    second_task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Second reservation")],
    )
    first_task_id = UUID(first_task["id"])
    second_task_id = UUID(second_task["id"])
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    monkeypatch.setattr(payment_routes, "runtime_from_settings", lambda: runtime)

    await _authorize_task(
        client,
        task_id=first_task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    first_authorization = await client.get(
        f"/v1/tasks/{first_task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert first_authorization.status_code == 200
    first_budget = first_authorization.json()["total_cap_minor"]
    assert first_authorization.json()["blocked_minor"] == first_budget
    assert first_authorization.json()["other_tasks_blocked_minor"] == 0

    second_started = await client.post(
        f"/v1/tasks/{second_task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert second_started.status_code == 201, second_started.text
    second_budget = second_started.json()["total_cap_minor"]
    assert second_started.json()["status"] == "pending"
    assert second_started.json()["blocked_minor"] == 0
    assert second_started.json()["other_tasks_blocked_minor"] == first_budget
    assert second_started.json()["total_creator_blocked_minor"] == first_budget
    assert second_started.json()["additional_approval_required_minor"] == second_budget
    assert len(gateway.created_customers) == 2

    second_refreshed = await client.post(
        f"/v1/tasks/{second_task_id}/payment-authorization/refresh",
        headers=client.auth_headers(creator_id),
    )
    assert second_refreshed.status_code == 200, second_refreshed.text
    assert second_refreshed.json()["status"] == "active"
    assert second_refreshed.json()["blocked_minor"] == second_budget
    assert second_refreshed.json()["other_tasks_blocked_minor"] == first_budget
    assert second_refreshed.json()["total_creator_blocked_minor"] == (
        first_budget + second_budget
    )
    assert second_refreshed.json()["additional_approval_required_minor"] == 0


async def test_oauth_mcp_uses_same_user_for_prava_task_and_completer_wallet(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "mcp-payment")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    monkeypatch.setattr(mcp_server, "payment_runtime_from_settings", lambda: runtime)
    monkeypatch.setattr(
        submission_service,
        "get_settings",
        lambda: SimpleNamespace(prava_payment_automation_enabled=True),
    )
    _, creator_principal = await issue_client(
        creator_id,
        scopes={PAYMENTS_READ_SCOPE, PAYMENTS_WRITE_SCOPE},
    )

    with use_oauth_principal(creator_principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            started = await mcp_client.call_tool(
                "start_task_payment_authorization",
                {"task_id": str(task_id)},
            )
            refreshed = await mcp_client.call_tool(
                "refresh_task_payment_authorization",
                {"task_id": str(task_id)},
            )

    assert not started.is_error
    assert started.structured_content["task_id"] == str(task_id)
    assert started.structured_content["status"] == "pending"
    assert started.structured_content["additional_approval_required_minor"] > 0
    assert refreshed.structured_content["status"] == "active"
    assert refreshed.structured_content["blocked_minor"] == (
        refreshed.structured_content["total_cap_minor"]
    )

    submitted = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert submitted.status_code == 201, submitted.text
    submission_id = UUID(submitted.json()["id"])
    approved = await verify(client, submission_id, creator_id, result="passed")
    assert approved.status_code == 200, approved.text
    pending = await client.get(
        f"/v1/submissions/{submission_id}/payment",
        headers=client.auth_headers(creator_id),
    )
    payment_id = UUID(pending.json()["id"])
    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True

    _, freelancer_principal = await issue_client(
        freelancer_id,
        scopes={PAYMENTS_READ_SCOPE},
    )
    with use_oauth_principal(creator_principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            payment = await mcp_client.call_tool(
                "get_payment_status",
                {"payment_id": str(payment_id)},
            )
            creator_wallet = await mcp_client.call_tool("get_wallet_balance", {})
    with use_oauth_principal(freelancer_principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            freelancer_wallet = await mcp_client.call_tool("get_wallet_balance", {})

    assert not payment.is_error
    assert payment.structured_content["status"] == "succeeded"
    assert creator_wallet.structured_content["balances"] == []
    assert freelancer_wallet.structured_content["balances"] == [
        {"currency": "USD", "balance_minor": 1_000}
    ]


async def test_prava_authorized_draft_cannot_change_or_delete_its_budget(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id = await create_user(
        client,
        email="authorized-draft-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    created = await client.post("/v1/tasks", json=task_payload(creator_id))
    assert created.status_code == 201, created.text
    task_id = UUID(created.json()["id"])
    gateway = FakePravaGateway()
    monkeypatch.setattr(
        payment_routes,
        "runtime_from_settings",
        lambda: payment_runtime(gateway),
    )
    authorized = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert authorized.status_code == 201, authorized.text

    async with AsyncSessionFactory() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(
                text(
                    "UPDATE tasks SET total_budget_minor = total_budget_minor + 1 "
                    "WHERE id = :task_id"
                ),
                {"task_id": task_id},
            )
            await session.commit()
        await session.rollback()
    assert getattr(caught.value.orig, "sqlstate", None) == "HCF01"

    replaced = await client.put(
        f"/v1/tasks/{task_id}",
        json=task_payload(creator_id, title="Changed after approval"),
        headers=client.auth_headers(creator_id),
    )
    deleted = await client.delete(
        f"/v1/tasks/{task_id}",
        headers=client.auth_headers(creator_id),
    )
    assert replaced.status_code == 409
    assert "Prava budget authorization" in replaced.json()["detail"]
    assert deleted.status_code == 409
