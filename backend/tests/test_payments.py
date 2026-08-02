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
from app.services.api_clients import (
    PAYMENTS_READ_SCOPE,
    PAYMENTS_WRITE_SCOPE,
    SUBMISSIONS_APPROVE_SCOPE,
    SUBMISSIONS_READ_SCOPE,
    SUBMISSIONS_VERIFY_SCOPE,
    TASKS_CREATE_SCOPE,
)
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
from tests.test_mcp_create_task import issue_client, mcp_arguments
from tests.test_submissions import claimed_work, submit, url_proof, verify
from tests.test_tasks import task_payload


class FakePravaGateway:
    def __init__(self) -> None:
        self.created_customers: list[str] = []
        self.created_amounts: list[str] = []
        self.charge_amounts: list[str] = []
        self.charge_references: list[str] = []
        self.revoked_sessions: list[str] = []
        self.revoke_failures: list[PravaGatewayError] = []
        self.failures: list[PravaGatewayError] = []

    async def create_mandate_session(self, **kwargs) -> PravaMandateSession:
        self.created_customers.append(kwargs["customer_id"])
        self.created_amounts.append(kwargs["amount"])
        return PravaMandateSession(
            session_id=f"sess_{len(self.created_customers)}",
            approval_url="https://sandbox.collect.prava.space/approve-demo",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def list_mandates(self, *, customer_id: str) -> list[PravaMandate]:
        assert customer_id in self.created_customers
        return [
            PravaMandate(
                mandate_id=f"mdt_hah_test_{index + 1}",
                status="active",
                state="available",
                approved_amount=self.created_amounts[index],
                currency="USD",
                valid_until=datetime.now(UTC) + timedelta(days=30),
                updated_at=datetime.now(UTC) + timedelta(seconds=index),
            )
            for index, created_customer in enumerate(self.created_customers)
            if created_customer == customer_id
        ]

    async def revoke_session(self, *, session_id: str) -> None:
        self.revoked_sessions.append(session_id)
        if self.revoke_failures:
            raise self.revoke_failures.pop(0)

    async def execute_sandbox_payment(self, **kwargs) -> PravaPaymentResult:
        self.charge_amounts.append(kwargs["amount"])
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


async def test_http_prava_revoke_session_requires_success(monkeypatch) -> None:
    gateway = _http_prava_gateway()
    requests: list[tuple[str, str, dict]] = []

    async def successful_revoke(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {"success": True}

    monkeypatch.setattr(gateway, "_request", successful_revoke)
    await gateway.revoke_session(session_id="ses_live_contract")
    assert requests == [("POST", "/v1/sessions/ses_live_contract/revoke", {"json": {}})]

    async def malformed_revoke(*args, **kwargs):
        del args, kwargs
        return {"success": False}

    monkeypatch.setattr(gateway, "_request", malformed_revoke)
    with pytest.raises(PravaGatewayError) as caught:
        await gateway.revoke_session(session_id="ses_live_contract")
    assert caught.value.code == "PRAVA_INVALID_RESPONSE"


def payment_runtime(
    gateway: FakePravaGateway,
    *,
    global_allowance_minor: int = 5_000,
) -> PaymentRuntime:
    return PaymentRuntime(
        gateway=gateway,
        merchant_name="Hire a Human",
        merchant_url="https://hah-api-prava.onrender.com",
        merchant_country="IN",
        payer_user_id="universal-hackathon-payer",
        payer_email="payer@example.com",
        global_allowance_minor=global_allowance_minor,
        global_max_charges=30,
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
) -> dict:
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
    return refreshed.json()


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


@pytest.mark.parametrize("revoke_already_gone", [False, True])
async def test_pending_prava_session_can_be_restarted(
    client: AsyncClient,
    monkeypatch,
    revoke_already_gone: bool,
) -> None:
    creator_id, _, claim_id = await claimed_work(client, f"restart-{revoke_already_gone}")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    monkeypatch.setattr(payment_routes, "runtime_from_settings", lambda: runtime)

    started = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert started.status_code == 201, started.text
    if revoke_already_gone:
        gateway.revoke_failures.append(PravaGatewayError("NOT_FOUND", retryable=False))

    restarted = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization/restart",
        headers=client.auth_headers(creator_id),
    )
    assert restarted.status_code == 201, restarted.text
    assert restarted.json()["status"] == "pending"
    assert restarted.json()["approval_url"].startswith("https://sandbox.collect.prava.space/")
    assert gateway.revoked_sessions == ["sess_1"]
    assert len(gateway.created_customers) == 2


async def test_active_prava_authorization_cannot_be_restarted(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, _, claim_id = await claimed_work(client, "restart-active")
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

    restarted = await client.post(
        f"/v1/tasks/{task_id}/payment-authorization/restart",
        headers=client.auth_headers(creator_id),
    )
    assert restarted.status_code == 409
    assert gateway.revoked_sessions == []


async def test_verified_submission_auto_pays_once_and_credits_internal_wallet(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id, freelancer_id, claim_id = await claimed_work(client, "payment-success")
    task_id = await _task_id_for_claim(claim_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    authorization = await _authorize_task(
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
    assert gateway.charge_references == [
        f"hah-pool-funding:{authorization['pool_id']}"
    ]
    assert gateway.charge_amounts == ["50.00"]

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
    authorization = await _authorize_task(
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
    assert gateway.charge_references == [
        f"hah-pool-funding:{authorization['pool_id']}"
    ]
    assert gateway.charge_amounts == ["50.00"]

    funded_authorization = await client.get(
        f"/v1/tasks/{task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert funded_authorization.status_code == 200
    assert funded_authorization.json()["pool_funded_once"] is True
    assert funded_authorization.json()["funding_status"] == "succeeded"
    assert funded_authorization.json()["used_minor"] == 2_000
    assert funded_authorization.json()["payments_used"] == 2

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


async def test_tasks_reuse_one_global_approval_and_report_available_budget(
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
    assert first_authorization.json()["pool_cap_minor"] == 5_000
    assert first_authorization.json()["global_approved_minor"] == 5_000
    assert first_authorization.json()["global_allocated_minor"] == first_budget
    assert first_authorization.json()["global_available_minor"] == 5_000 - first_budget

    second_started = await client.post(
        f"/v1/tasks/{second_task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert second_started.status_code == 201, second_started.text
    second_budget = second_started.json()["total_cap_minor"]
    assert second_started.json()["status"] == "active"
    assert second_started.json()["approval_url"] is None
    assert second_started.json()["reused_global_approval"] is True
    assert second_started.json()["pool_id"] == first_authorization.json()["pool_id"]
    assert (
        second_started.json()["provider_authorization_ref"]
        == first_authorization.json()["provider_authorization_ref"]
    )
    assert second_started.json()["blocked_minor"] == second_budget
    assert second_started.json()["other_tasks_blocked_minor"] == first_budget
    assert second_started.json()["total_creator_blocked_minor"] == first_budget + second_budget
    assert second_started.json()["additional_approval_required_minor"] == 0
    assert second_started.json()["pool_allocated_minor"] == first_budget + second_budget
    assert second_started.json()["global_allocated_minor"] == first_budget + second_budget
    assert second_started.json()["global_available_minor"] == 5_000 - (first_budget + second_budget)
    assert gateway.created_customers == ["universal-hackathon-payer"]

    allowance = await client.get(
        "/v1/payments/global-allowance?currency=usd",
        headers=client.auth_headers(creator_id),
    )
    assert allowance.status_code == 200, allowance.text
    assert allowance.json() == {
        "currency": "USD",
        "approved_minor": 5_000,
        "allocated_minor": first_budget + second_budget,
        "available_minor": 5_000 - first_budget - second_budget,
        "pending_approval_minor": 0,
        "active_pool_count": 1,
        "pending_pool_count": 0,
    }


async def test_exhausted_global_allowance_requests_one_new_top_up(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id = await create_user(
        client,
        email="payment-top-up-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    first_task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Exhaust allowance")],
    )
    second_task = await create_open_task(
        client,
        creator_id,
        [bounty_payload("Needs top up")],
    )
    first_task_id = UUID(first_task["id"])
    second_task_id = UUID(second_task["id"])
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway, global_allowance_minor=1_000)
    monkeypatch.setattr(payment_routes, "runtime_from_settings", lambda: runtime)

    await _authorize_task(
        client,
        task_id=first_task_id,
        creator_id=creator_id,
        runtime=runtime,
        monkeypatch=monkeypatch,
    )
    second_started = await client.post(
        f"/v1/tasks/{second_task_id}/payment-authorization",
        headers=client.auth_headers(creator_id),
    )
    assert second_started.status_code == 201, second_started.text
    assert second_started.json()["status"] == "pending"
    assert second_started.json()["additional_approval_required_minor"] == 1_000
    assert second_started.json()["global_available_minor"] == 0
    assert second_started.json()["global_pending_approval_minor"] == 1_000
    assert gateway.created_customers == [
        "universal-hackathon-payer",
        "universal-hackathon-payer",
    ]

    second_refreshed = await client.post(
        f"/v1/tasks/{second_task_id}/payment-authorization/refresh",
        headers=client.auth_headers(creator_id),
    )
    assert second_refreshed.status_code == 200, second_refreshed.text
    assert second_refreshed.json()["status"] == "active"
    assert second_refreshed.json()["global_approved_minor"] == 2_000
    assert second_refreshed.json()["global_allocated_minor"] == 2_000
    assert second_refreshed.json()["global_available_minor"] == 0
    assert second_refreshed.json()["global_pending_approval_minor"] == 0


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
            restarted = await mcp_client.call_tool(
                "restart_task_payment_authorization",
                {"task_id": str(task_id)},
            )
            refreshed = await mcp_client.call_tool(
                "refresh_task_payment_authorization",
                {"task_id": str(task_id)},
            )
            allowance = await mcp_client.call_tool(
                "get_global_allowance",
                {"currency": "USD"},
            )

    assert not started.is_error
    assert started.structured_content["task_id"] == str(task_id)
    assert started.structured_content["status"] == "pending"
    assert started.structured_content["additional_approval_required_minor"] > 0
    assert not restarted.is_error
    assert restarted.structured_content["status"] == "pending"
    assert gateway.revoked_sessions == ["sess_1"]
    assert refreshed.structured_content["status"] == "active"
    assert not allowance.is_error
    assert allowance.structured_content["approved_minor"] == 5_000
    assert allowance.structured_content["allocated_minor"] == 1_000
    assert allowance.structured_content["available_minor"] == 4_000
    assert (
        refreshed.structured_content["blocked_minor"]
        == (refreshed.structured_content["total_cap_minor"])
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


async def test_mcp_agent_can_publish_monitor_pay_and_reuse_human_allowance(
    client: AsyncClient,
    monkeypatch,
) -> None:
    creator_id = await create_user(
        client,
        email="mcp-agent-creator@example.com",
        can_create_tasks=True,
        can_work_tasks=False,
    )
    freelancer_id = await create_user(
        client,
        email="mcp-agent-worker@example.com",
    )
    social_account_id = await add_social_account(freelancer_id)
    gateway = FakePravaGateway()
    runtime = payment_runtime(gateway)
    monkeypatch.setattr(mcp_server, "payment_runtime_from_settings", lambda: runtime)
    monkeypatch.setattr(
        submission_service,
        "get_settings",
        lambda: SimpleNamespace(prava_payment_automation_enabled=True),
    )
    _, principal = await issue_client(
        creator_id,
        scopes={
            TASKS_CREATE_SCOPE,
            SUBMISSIONS_READ_SCOPE,
            SUBMISSIONS_VERIFY_SCOPE,
            SUBMISSIONS_APPROVE_SCOPE,
            PAYMENTS_READ_SCOPE,
            PAYMENTS_WRITE_SCOPE,
        },
    )
    first_arguments = mcp_arguments(
        idempotency_key="agent-flow-first",
        title="First MCP funded task",
        total_budget_minor=1_000,
        bounties=[bounty_payload("First MCP bounty")],
    )

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            created = await mcp_client.call_tool("create_task", first_arguments)
            first_publish = await mcp_client.call_tool(
                "publish_task",
                {"task_id": created.structured_content["id"]},
            )
            approved_publish = await mcp_client.call_tool(
                "publish_task",
                {"task_id": created.structured_content["id"]},
            )
            repeated_publish = await mcp_client.call_tool(
                "publish_task",
                {"task_id": created.structured_content["id"]},
            )
            listed = await mcp_client.call_tool("list_tasks", {})

    assert not first_publish.is_error
    assert first_publish.structured_content["ready"] is False
    assert first_publish.structured_content["human_approval_required"] is True
    assert first_publish.structured_content["next_action"] == (
        "open_approval_url_then_call_publish_task_again"
    )
    assert first_publish.structured_content["payment_authorization"]["approval_url"] == (
        "https://sandbox.collect.prava.space/approve-demo"
    )
    assert approved_publish.structured_content["ready"] is True
    assert approved_publish.structured_content["task"]["status"] == "open"
    assert approved_publish.structured_content["next_action"] == "task_open"
    assert repeated_publish.structured_content == approved_publish.structured_content
    assert [task["id"] for task in listed.structured_content["result"]] == [
        created.structured_content["id"]
    ]

    task_id = UUID(created.structured_content["id"])
    bounty_id = UUID(created.structured_content["bounties"][0]["id"])
    claimed = await claim(client, bounty_id, freelancer_id, social_account_id)
    assert claimed.status_code == 201, claimed.text
    claim_id = UUID(claimed.json()["id"])
    submitted = await submit(client, claim_id, freelancer_id, [url_proof()])
    assert submitted.status_code == 201, submitted.text
    submission_id = submitted.json()["id"]

    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            before_verification = await mcp_client.call_tool(
                "list_task_submissions",
                {"task_id": str(task_id)},
            )
            verified = await mcp_client.call_tool(
                "verify_submission",
                {
                    "idempotency_key": "agent-flow-verify",
                    "submission_id": submission_id,
                    "result": "passed",
                    "checks": {"proof": "accepted"},
                },
            )

    assert before_verification.structured_content["result"][0]["id"] == submission_id
    assert before_verification.structured_content["result"][0]["verification_status"] == (
        "pending"
    )
    assert verified.structured_content["verification_status"] == "passed"
    assert await process_next_payment(AsyncSessionFactory, runtime=runtime) is True

    second_arguments = mcp_arguments(
        idempotency_key="agent-flow-second",
        title="Second MCP funded task",
        total_budget_minor=1_000,
        bounties=[bounty_payload("Second MCP bounty")],
    )
    with use_oauth_principal(principal):
        async with Client(app.state.mcp_server, raise_exceptions=True) as mcp_client:
            payments = await mcp_client.call_tool(
                "list_task_payment_statuses",
                {"task_id": str(task_id)},
            )
            authorization = await mcp_client.call_tool(
                "get_task_payment_authorization_status",
                {"task_id": str(task_id)},
            )
            second_created = await mcp_client.call_tool("create_task", second_arguments)
            second_publish = await mcp_client.call_tool(
                "publish_task",
                {"task_id": second_created.structured_content["id"]},
            )

    assert payments.structured_content["result"][0]["status"] == "succeeded"
    assert payments.structured_content["result"][0]["submission_id"] == submission_id
    assert authorization.structured_content["status"] == "active"
    assert second_publish.structured_content["ready"] is True
    assert second_publish.structured_content["task"]["status"] == "open"
    assert (
        second_publish.structured_content["payment_authorization"]["reused_global_approval"] is True
    )
    assert gateway.created_customers == ["universal-hackathon-payer"]


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
