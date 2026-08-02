from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.claim import BountyClaim
from app.models.payment import (
    AuthorizationStatus,
    Payment,
    PaymentAttempt,
    PaymentAuthorization,
    PaymentStatus,
    WalletEntry,
)
from app.models.submission import Submission, VerificationStatus
from app.models.task import Bounty, Task, TaskStatus
from app.schemas.payment import (
    PaymentAuthorizationResponse,
    PaymentResponse,
    WalletBalanceResponse,
    WalletEntryResponse,
    WalletResponse,
)

_SAFE_PROVIDER_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_SUPPORTED_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "INR",
        "CAD",
        "AUD",
        "JPY",
        "SGD",
        "AED",
        "HKD",
        "MXN",
        "BRL",
        "CHF",
        "CNY",
        "NZD",
        "SEK",
        "NOK",
        "DKK",
        "ZAR",
        "THB",
        "KRW",
        "PLN",
        "TWD",
        "PHP",
        "IDR",
        "MYR",
        "CZK",
        "ILS",
        "CLP",
        "ARS",
        "COP",
        "PEN",
        "SAR",
        "QAR",
        "EGP",
        "NGN",
        "KES",
        "GHS",
        "TZS",
        "UGX",
        "PKR",
        "BDT",
        "LKR",
        "VND",
        "MMK",
        "NPR",
    }
)
_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW"})
_UNSUPPORTED_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "KWD", "OMR"})


class PaymentNotFoundError(Exception):
    pass


class PaymentAuthorizationRequiredError(Exception):
    pass


class PaymentConflictError(Exception):
    pass


class PaymentValidationError(Exception):
    pass


class PaymentProviderUnavailableError(Exception):
    pass


class PravaGatewayError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        transaction_id: str | None = None,
    ) -> None:
        super().__init__("Prava sandbox request failed")
        self.code = code if _SAFE_PROVIDER_CODE.fullmatch(code) else "PRAVA_ERROR"
        self.retryable = retryable
        self.transaction_id = transaction_id


@dataclass(frozen=True, slots=True)
class PravaMandateSession:
    session_id: str
    approval_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PravaMandate:
    mandate_id: str
    status: str
    state: str
    approved_amount: str
    currency: str
    valid_until: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PravaPaymentResult:
    transaction_id: str
    order_id: str | None
    status: str
    deduplicated: bool
    visa_confirmation: str | None


class PravaGateway(Protocol):
    async def create_mandate_session(
        self,
        *,
        customer_id: str,
        customer_email: str,
        amount: str,
        currency: str,
        merchant_name: str,
        merchant_url: str,
        merchant_country: str,
        task_title: str,
        max_charges: int,
    ) -> PravaMandateSession: ...

    async def list_mandates(self, *, customer_id: str) -> list[PravaMandate]: ...

    async def revoke_session(self, *, session_id: str) -> None: ...

    async def execute_sandbox_payment(
        self,
        *,
        mandate_id: str,
        amount: str,
        reference: str,
    ) -> PravaPaymentResult: ...


class HTTPPravaGateway:
    """Prava sandbox REST client that never returns or persists card credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        secret_key: SecretStr,
        timeout_seconds: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key
        self._timeout_seconds = timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._secret_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    json=json,
                    params=params,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise PravaGatewayError("PRAVA_UNAVAILABLE", retryable=True) from error
        if len(response.content) > 262_144:
            raise PravaGatewayError("PRAVA_RESPONSE_TOO_LARGE", retryable=False)
        try:
            body = response.json()
        except ValueError as error:
            raise PravaGatewayError(
                "PRAVA_INVALID_RESPONSE",
                retryable=response.status_code >= 500,
            ) from error
        if not isinstance(body, dict):
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False)
        if response.is_success:
            return body
        provider_code = "PRAVA_REQUEST_FAILED"
        error_value = body.get("error")
        if isinstance(error_value, dict) and isinstance(error_value.get("code"), str):
            candidate = error_value["code"].upper()
            if _SAFE_PROVIDER_CODE.fullmatch(candidate):
                provider_code = candidate
        raise PravaGatewayError(
            provider_code,
            retryable=response.status_code == 429 or response.status_code >= 500,
        )

    async def create_mandate_session(
        self,
        *,
        customer_id: str,
        customer_email: str,
        amount: str,
        currency: str,
        merchant_name: str,
        merchant_url: str,
        merchant_country: str,
        task_title: str,
        max_charges: int,
    ) -> PravaMandateSession:
        body = await self._request(
            "POST",
            "/v1/sessions",
            json={
                "user_id": customer_id,
                "user_email": customer_email,
                "total_amount": amount,
                "currency": currency,
                "purchase_context": [
                    {
                        "merchant_details": {
                            "name": merchant_name,
                            "url": merchant_url,
                            "country_code_iso2": merchant_country,
                        },
                        "product_details": [
                            {
                                "description": f"Task reward cap: {task_title[:120]}",
                                "unit_price": amount,
                                "quantity": 1,
                            }
                        ],
                    }
                ],
                "mandate_setup": {
                    "intent": "mandate_setup",
                    "recurring_frequency": "monthly",
                    "merchant_scope": "listed",
                    "max_charges": max_charges,
                },
            },
        )
        try:
            expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
            session_id = str(body["session_id"])
            approval_url = str(body["iframe_url"])
            # Prava's sandbox currently omits ``authorizeOnly`` from some
            # successful mandate-session responses even though the API docs
            # show it as ``true``. The request itself included mandate_setup,
            # so an omitted flag is safe to accept; an explicit false still
            # indicates the wrong checkout flow and must be rejected.
            authorize_only = body.get("authorizeOnly")
        except (KeyError, TypeError, ValueError) as error:
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False) from error
        if (
            authorize_only not in {None, True}
            or not session_id.startswith(("sess_", "ses_"))
            or not approval_url.startswith("https://")
            or expires_at.tzinfo is None
        ):
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False)
        return PravaMandateSession(
            session_id=session_id,
            approval_url=approval_url,
            expires_at=expires_at,
        )

    async def list_mandates(self, *, customer_id: str) -> list[PravaMandate]:
        body = await self._request(
            "GET",
            "/v1/mandates",
            params={"customer_id": customer_id, "standing_only": "true"},
        )
        raw_mandates = body.get("mandates")
        if not isinstance(raw_mandates, list):
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False)
        mandates: list[PravaMandate] = []
        try:
            for raw in raw_mandates:
                if not isinstance(raw, dict):
                    raise ValueError
                valid_until_raw = raw.get("validUntil")
                valid_until = (
                    datetime.fromisoformat(str(valid_until_raw).replace("Z", "+00:00"))
                    if valid_until_raw is not None
                    else None
                )
                updated_at = datetime.fromisoformat(str(raw["updatedAt"]).replace("Z", "+00:00"))
                mandates.append(
                    PravaMandate(
                        mandate_id=str(raw["id"]),
                        status=str(raw["status"]),
                        state=str(raw["state"]),
                        approved_amount=str(raw["approvedAmount"]),
                        currency=str(raw["currency"]),
                        valid_until=valid_until,
                        updated_at=updated_at,
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False) from error
        return mandates

    async def revoke_session(self, *, session_id: str) -> None:
        # Prava's Fastify sandbox rejects a bodyless JSON POST with
        # FST_ERR_CTP_EMPTY_JSON_BODY, so send the explicit empty object.
        body = await self._request(
            "POST",
            f"/v1/sessions/{session_id}/revoke",
            json={},
        )
        if body.get("success") is not True:
            raise PravaGatewayError("PRAVA_INVALID_RESPONSE", retryable=False)

    async def execute_sandbox_payment(
        self,
        *,
        mandate_id: str,
        amount: str,
        reference: str,
    ) -> PravaPaymentResult:
        # This is Prava's official sandbox charge/report completion loop. Prava
        # returns one-time card credentials, which are validated and discarded
        # immediately. They are never returned, persisted, or logged by HAH.
        charge = await self._request(
            "POST",
            f"/v1/mandates/{mandate_id}/charge",
            json={
                "amount": amount,
                "reference": reference,
            },
        )
        transaction_id = str(charge.get("transactionId", ""))
        credentials = charge.get("credentials")
        has_ephemeral_credentials = isinstance(credentials, dict) and all(
            isinstance(credentials.get(field), str) and credentials[field]
            for field in ("token", "dynamicCvv", "expiryMonth", "expiryYear")
        )
        if (
            charge.get("status") != "awaiting_result"
            or charge.get("fetchStatus") != "SUCCESS"
            or not transaction_id
            or not has_ephemeral_credentials
        ):
            code = str(charge.get("errorCode", "PRAVA_CHARGE_FAILED")).upper()
            raise PravaGatewayError(code, retryable=False, transaction_id=transaction_id or None)

        order_id = str(charge["orderId"]) if charge.get("orderId") is not None else None
        deduplicated = charge.get("deduplicated") is True
        credentials = None
        charge = {}

        try:
            report = await self._request(
                "POST",
                f"/v1/mandates/{mandate_id}/charges/{transaction_id}/report",
                json={
                    "txn_status": "APPROVED",
                    "txn_type": "PURCHASE",
                    "amount_paid": amount,
                },
            )
        except PravaGatewayError as error:
            raise PravaGatewayError(
                error.code,
                retryable=error.retryable,
                transaction_id=transaction_id,
            ) from error
        if report.get("status") != "completed" or report.get("visaConfirmation") != "SUCCESS":
            raise PravaGatewayError(
                "PRAVA_REPORT_FAILED",
                retryable=False,
                transaction_id=transaction_id,
            )
        return PravaPaymentResult(
            transaction_id=transaction_id,
            order_id=order_id,
            status="completed",
            deduplicated=deduplicated,
            visa_confirmation="SUCCESS",
        )


@dataclass(frozen=True, slots=True)
class PaymentWorkerPolicy:
    max_attempts: int = 4
    lease_seconds: int = 45
    retry_base_seconds: int = 15
    retry_cap_seconds: int = 300
    poll_interval_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class PaymentRuntime:
    gateway: PravaGateway
    merchant_name: str
    merchant_url: str
    merchant_country: str
    payer_user_id: str
    payer_email: str
    policy: PaymentWorkerPolicy = field(default_factory=PaymentWorkerPolicy)


@dataclass(frozen=True, slots=True)
class LeasedPayment:
    payment_id: UUID
    authorization_id: UUID
    mandate_id: str
    funding_amount_minor: int
    currency: str
    funding_idempotency_key: str
    attempt_number: int
    requires_prava_funding: bool


def runtime_from_settings(settings: Settings | None = None) -> PaymentRuntime:
    settings = settings or get_settings()
    if not settings.prava_configured:
        raise PaymentProviderUnavailableError("Prava sandbox credentials are not configured")
    assert settings.prava_secret_key is not None
    assert settings.prava_payer_email is not None
    return PaymentRuntime(
        gateway=HTTPPravaGateway(
            base_url=str(settings.prava_base_url),
            secret_key=settings.prava_secret_key,
            timeout_seconds=settings.prava_request_timeout_seconds,
        ),
        merchant_name=settings.prava_merchant_name.strip(),
        merchant_url=str(settings.prava_merchant_url),
        merchant_country=settings.prava_merchant_country,
        payer_user_id=settings.prava_payer_user_id.strip(),
        payer_email=str(settings.prava_payer_email),
        policy=PaymentWorkerPolicy(
            max_attempts=settings.prava_payment_max_attempts,
            lease_seconds=settings.prava_payment_lease_seconds,
            retry_base_seconds=settings.prava_payment_retry_base_seconds,
            retry_cap_seconds=settings.prava_payment_retry_cap_seconds,
            poll_interval_seconds=settings.prava_payment_poll_interval_seconds,
        ),
    )


def _minor_to_decimal(amount_minor: int, currency: str) -> str:
    currency = currency.strip().upper()
    if currency not in _SUPPORTED_CURRENCIES or currency in _UNSUPPORTED_THREE_DECIMAL_CURRENCIES:
        raise PaymentValidationError("Task currency is not supported by this Prava integration")
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return str(amount_minor)
    return f"{Decimal(amount_minor) / Decimal(100):.2f}"


async def _authorization_response(
    session: AsyncSession,
    authorization: PaymentAuthorization,
    *,
    approval_url: str | None = None,
) -> PaymentAuthorizationResponse:
    remaining_minor = max(authorization.total_cap_minor - authorization.used_minor, 0)
    reserves_budget = authorization.status in {
        AuthorizationStatus.ACTIVE,
        AuthorizationStatus.PAUSED,
    }
    blocked_minor = remaining_minor if reserves_budget else 0
    other_tasks_blocked_minor = int(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        PaymentAuthorization.total_cap_minor - PaymentAuthorization.used_minor
                    ),
                    0,
                )
            ).where(
                PaymentAuthorization.creator_id == authorization.creator_id,
                PaymentAuthorization.id != authorization.id,
                PaymentAuthorization.currency == authorization.currency,
                PaymentAuthorization.status.in_(
                    [AuthorizationStatus.ACTIVE, AuthorizationStatus.PAUSED]
                ),
                PaymentAuthorization.total_cap_minor > PaymentAuthorization.used_minor,
            )
        )
        or 0
    )
    additional_required = (
        remaining_minor if authorization.status == AuthorizationStatus.PENDING else 0
    )
    return PaymentAuthorizationResponse(
        id=authorization.id,
        task_id=authorization.task_id,
        provider=authorization.provider,
        status=authorization.status,
        per_payment_cap_minor=authorization.per_payment_cap_minor,
        total_cap_minor=authorization.total_cap_minor,
        used_minor=authorization.used_minor,
        max_payments=authorization.max_payments,
        payments_used=authorization.payments_used,
        currency=authorization.currency.strip(),
        provider_authorization_ref=authorization.provider_authorization_ref,
        funding_status=authorization.funding_status,
        provider_funding_transaction_ref=authorization.provider_funding_transaction_ref,
        funding_failure_code=authorization.funding_failure_code,
        funding_failure_message=authorization.funding_failure_message,
        funded_at=authorization.funded_at,
        blocked_minor=blocked_minor,
        remaining_minor=remaining_minor,
        other_tasks_blocked_minor=other_tasks_blocked_minor,
        total_creator_blocked_minor=blocked_minor + other_tasks_blocked_minor,
        additional_approval_required_minor=additional_required,
        approval_url=approval_url,
        approval_expires_at=(
            authorization.provider_session_expires_at if approval_url is not None else None
        ),
        valid_until=authorization.valid_until,
        created_at=authorization.created_at,
        updated_at=authorization.updated_at,
    )


async def start_task_payment_authorization(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None or task.creator_id != creator_id:
        raise PaymentNotFoundError("Task not found")
    if task.status not in {TaskStatus.DRAFT, TaskStatus.OPEN}:
        raise PaymentConflictError("Payment authorization cannot be set up for this task state")

    bounties = list(
        (
            await session.scalars(
                select(Bounty).where(Bounty.task_id == task.id).order_by(Bounty.id)
            )
        ).all()
    )
    if not bounties:
        raise PaymentValidationError("Task has no bounties")
    per_payment_cap_minor = max(bounty.reward_minor for bounty in bounties)
    max_payments = sum(bounty.slots_total for bounty in bounties)
    currency = task.currency.strip()

    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.task_id == task.id)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if authorization is not None:
        if authorization.status == AuthorizationStatus.ACTIVE:
            return await _authorization_response(session, authorization)
        if (
            authorization.status == AuthorizationStatus.PENDING
            and authorization.funding_status != PaymentStatus.FAILED
            and authorization.provider_session_expires_at is not None
            and authorization.provider_session_expires_at > now
        ):
            raise PaymentConflictError(
                "A Prava approval session is already pending; refresh it after approval "
                "or restart it if the hosted session was consumed"
            )
        if authorization.payments_used or authorization.used_minor:
            raise PaymentConflictError("Used payment authorization cannot be replaced")

    common_values = {
        "per_payment_cap_minor": per_payment_cap_minor,
        "total_cap_minor": task.total_budget_minor,
        "max_payments": max_payments,
        "currency": task.currency,
    }
    customer_ref = f"{runtime.payer_user_id}:task:{task.id}"
    if len(customer_ref) > 255:
        customer_ref = f"hah-task:{task.id}"
    provider_session = await runtime.gateway.create_mandate_session(
        customer_id=customer_ref,
        customer_email=runtime.payer_email,
        amount=_minor_to_decimal(task.total_budget_minor, task.currency),
        currency=currency,
        merchant_name=runtime.merchant_name,
        merchant_url=runtime.merchant_url,
        merchant_country=runtime.merchant_country,
        task_title=task.title,
        max_charges=1,
    )

    if authorization is None:
        authorization = PaymentAuthorization(
            task_id=task.id,
            creator_id=creator_id,
            provider="prava",
            provider_customer_ref=customer_ref,
            provider_session_ref=provider_session.session_id,
            provider_session_expires_at=provider_session.expires_at,
            funding_status=PaymentStatus.CREATED,
            funding_idempotency_key=f"hah-task-funding:{task.id}",
            status=AuthorizationStatus.PENDING,
            **common_values,
        )
        session.add(authorization)
    else:
        authorization.provider_customer_ref = customer_ref
        authorization.provider_session_ref = provider_session.session_id
        authorization.provider_session_expires_at = provider_session.expires_at
        authorization.provider_authorization_ref = None
        authorization.funding_status = PaymentStatus.CREATED
        authorization.funding_idempotency_key = f"hah-task-funding:{task.id}"
        authorization.provider_funding_transaction_ref = None
        authorization.funding_failure_code = None
        authorization.funding_failure_message = None
        authorization.funded_at = None
        authorization.status = AuthorizationStatus.PENDING
        for field_name, value in common_values.items():
            setattr(authorization, field_name, value)
        authorization.valid_until = None
    await session.flush()
    await session.refresh(authorization)
    return await _authorization_response(
        session,
        authorization,
        approval_url=provider_session.approval_url,
    )


async def start_task_payment_authorization_and_commit(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    try:
        response = await start_task_payment_authorization(
            session,
            task_id,
            creator_id=creator_id,
            runtime=runtime,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def restart_task_payment_authorization(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None or task.creator_id != creator_id:
        raise PaymentNotFoundError("Task not found")
    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.task_id == task.id)
        .with_for_update()
    )
    if authorization is None:
        raise PaymentNotFoundError("Payment authorization not found")
    if authorization.status == AuthorizationStatus.ACTIVE:
        raise PaymentConflictError("Active payment authorization cannot be restarted")
    if authorization.payments_used or authorization.used_minor:
        raise PaymentConflictError("Used payment authorization cannot be restarted")
    if authorization.status not in {AuthorizationStatus.PENDING, AuthorizationStatus.EXPIRED}:
        raise PaymentConflictError("Payment authorization cannot be restarted in this state")

    if authorization.provider_session_ref is not None:
        try:
            await runtime.gateway.revoke_session(
                session_id=authorization.provider_session_ref,
            )
        except PravaGatewayError as error:
            # Prava returns NOT_FOUND for a session that was already consumed,
            # expired, or does not belong to the merchant. Ownership was
            # established from our task-bound database row, so only that
            # terminal provider response is safe to tolerate here.
            if error.code != "NOT_FOUND":
                raise

    authorization.provider_session_expires_at = datetime.now(UTC)
    await session.flush()
    return await start_task_payment_authorization(
        session,
        task_id,
        creator_id=creator_id,
        runtime=runtime,
    )


async def restart_task_payment_authorization_and_commit(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    try:
        response = await restart_task_payment_authorization(
            session,
            task_id,
            creator_id=creator_id,
            runtime=runtime,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def refresh_task_payment_authorization(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    task = await session.get(Task, task_id)
    if task is None or task.creator_id != creator_id:
        raise PaymentNotFoundError("Task not found")
    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.task_id == task.id)
        .with_for_update()
    )
    if authorization is None:
        raise PaymentNotFoundError("Payment authorization not found")
    if authorization.status in {AuthorizationStatus.ACTIVE, AuthorizationStatus.CANCELLED}:
        return await _authorization_response(session, authorization)
    if authorization.provider_customer_ref is None:
        raise PaymentConflictError("Payment authorization is missing its Prava customer")

    expected_amount = _minor_to_decimal(
        authorization.total_cap_minor,
        authorization.currency,
    )
    mandates = await runtime.gateway.list_mandates(customer_id=authorization.provider_customer_ref)
    matching = sorted(
        (
            mandate
            for mandate in mandates
            if mandate.currency == authorization.currency.strip()
            and mandate.approved_amount == expected_amount
        ),
        key=lambda mandate: mandate.updated_at,
        reverse=True,
    )
    if matching:
        mandate = matching[0]
        authorization.provider_authorization_ref = mandate.mandate_id
        authorization.valid_until = mandate.valid_until
        authorization.status = {
            "active": AuthorizationStatus.ACTIVE,
            "paused": AuthorizationStatus.PAUSED,
            "expired": AuthorizationStatus.EXPIRED,
            "cancelled": AuthorizationStatus.CANCELLED,
            "consumed": AuthorizationStatus.EXPIRED,
            "pending": AuthorizationStatus.PENDING,
        }.get(mandate.status, AuthorizationStatus.PENDING)
        if mandate.status == "active":
            authorization.funding_status = PaymentStatus.CREATED
    elif (
        authorization.provider_session_expires_at is not None
        and authorization.provider_session_expires_at <= datetime.now(UTC)
    ):
        authorization.status = AuthorizationStatus.EXPIRED
    await session.flush()
    await session.refresh(authorization)
    return await _authorization_response(session, authorization)


async def refresh_task_payment_authorization_and_commit(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
    runtime: PaymentRuntime,
) -> PaymentAuthorizationResponse:
    try:
        response = await refresh_task_payment_authorization(
            session,
            task_id,
            creator_id=creator_id,
            runtime=runtime,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def get_task_payment_authorization(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
) -> PaymentAuthorizationResponse:
    task = await session.get(Task, task_id)
    if task is None or task.creator_id != creator_id:
        raise PaymentNotFoundError("Task not found")
    authorization = await session.scalar(
        select(PaymentAuthorization).where(PaymentAuthorization.task_id == task.id)
    )
    if authorization is None:
        raise PaymentNotFoundError("Payment authorization not found")
    return await _authorization_response(session, authorization)


async def ensure_payment_for_verified_submission(
    session: AsyncSession,
    *,
    submission: Submission,
    claim: BountyClaim,
    bounty: Bounty,
    creator_id: UUID,
) -> Payment:
    existing = await session.scalar(select(Payment).where(Payment.submission_id == submission.id))
    if existing is not None:
        return existing
    if submission.verification_status != VerificationStatus.PASSED:
        raise PaymentValidationError("Only a passed submission can be paid")

    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.task_id == bounty.task_id)
        .with_for_update()
    )
    if authorization is None or authorization.status != AuthorizationStatus.ACTIVE:
        raise PaymentAuthorizationRequiredError("Task has no active Prava payment authorization")
    if authorization.provider_authorization_ref is None:
        raise PaymentAuthorizationRequiredError("Task has no approved Prava mandate")

    payment = Payment(
        authorization_id=authorization.id,
        task_id=bounty.task_id,
        bounty_id=bounty.id,
        claim_id=claim.id,
        submission_id=submission.id,
        payer_user_id=creator_id,
        payee_user_id=claim.freelancer_id,
        provider="prava",
        amount_minor=bounty.reward_minor,
        currency=authorization.currency,
        status=PaymentStatus.CREATED,
        idempotency_key=f"hah-payment:{submission.id}",
        next_attempt_at=datetime.now(UTC),
    )
    session.add(payment)
    try:
        await session.flush()
    except DBAPIError:
        existing = await session.scalar(
            select(Payment).where(Payment.submission_id == submission.id)
        )
        if existing is not None:
            return existing
        raise
    return payment


async def _payment_response(session: AsyncSession, payment: Payment) -> PaymentResponse:
    attempt_count = await session.scalar(
        select(func.count(PaymentAttempt.id)).where(PaymentAttempt.payment_id == payment.id)
    )
    return PaymentResponse(
        id=payment.id,
        task_id=payment.task_id,
        bounty_id=payment.bounty_id,
        claim_id=payment.claim_id,
        submission_id=payment.submission_id,
        payer_user_id=payment.payer_user_id,
        payee_user_id=payment.payee_user_id,
        provider=payment.provider,
        amount_minor=payment.amount_minor,
        currency=payment.currency.strip(),
        status=payment.status,
        provider_transaction_ref=payment.provider_transaction_ref,
        failure_code=payment.failure_code,
        failure_message=payment.failure_message,
        next_attempt_at=payment.next_attempt_at,
        attempt_count=attempt_count or 0,
        created_at=payment.created_at,
        updated_at=payment.updated_at,
        completed_at=payment.completed_at,
    )


async def get_payment(
    session: AsyncSession,
    payment_id: UUID,
    *,
    authorized_user_id: UUID,
) -> PaymentResponse:
    payment = await session.get(Payment, payment_id)
    if payment is None or authorized_user_id not in {
        payment.payer_user_id,
        payment.payee_user_id,
    }:
        raise PaymentNotFoundError("Payment not found")
    return await _payment_response(session, payment)


async def get_submission_payment(
    session: AsyncSession,
    submission_id: UUID,
    *,
    authorized_user_id: UUID,
) -> PaymentResponse:
    payment = await session.scalar(select(Payment).where(Payment.submission_id == submission_id))
    if payment is None or authorized_user_id not in {
        payment.payer_user_id,
        payment.payee_user_id,
    }:
        raise PaymentNotFoundError("Payment not found")
    return await _payment_response(session, payment)


async def list_task_payments(
    session: AsyncSession,
    task_id: UUID,
    *,
    creator_id: UUID,
) -> list[PaymentResponse]:
    task = await session.get(Task, task_id)
    if task is None or task.creator_id != creator_id:
        raise PaymentNotFoundError("Task not found")
    payments = list(
        (
            await session.scalars(
                select(Payment)
                .where(Payment.task_id == task_id)
                .order_by(Payment.created_at.desc(), Payment.id.desc())
            )
        ).all()
    )
    return [await _payment_response(session, payment) for payment in payments]


async def get_wallet(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> WalletResponse:
    entries = list(
        (
            await session.scalars(
                select(WalletEntry)
                .where(WalletEntry.user_id == user_id)
                .order_by(WalletEntry.created_at.desc(), WalletEntry.id.desc())
            )
        ).all()
    )
    balances: dict[str, int] = {}
    for entry in entries:
        currency = entry.currency.strip()
        balances[currency] = balances.get(currency, 0) + entry.amount_minor
    return WalletResponse(
        user_id=user_id,
        redeemable=False,
        balances=[
            WalletBalanceResponse(currency=currency, balance_minor=amount)
            for currency, amount in sorted(balances.items())
        ],
        entries=[
            WalletEntryResponse(
                id=entry.id,
                payment_id=entry.payment_id,
                amount_minor=entry.amount_minor,
                currency=entry.currency.strip(),
                entry_type=entry.entry_type,
                created_at=entry.created_at,
            )
            for entry in entries
        ],
    )


async def retry_payment(
    session: AsyncSession,
    payment_id: UUID,
    *,
    creator_id: UUID,
) -> PaymentResponse:
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.payer_user_id != creator_id:
        raise PaymentNotFoundError("Payment not found")
    if payment.status != PaymentStatus.FAILED:
        raise PaymentConflictError("Only a failed payment can be retried")
    authorization = await session.get(PaymentAuthorization, payment.authorization_id)
    if authorization is None or authorization.status != AuthorizationStatus.ACTIVE:
        raise PaymentAuthorizationRequiredError("Prava payment authorization is not active")
    if authorization.provider_authorization_ref is None:
        raise PaymentAuthorizationRequiredError("Task has no approved Prava mandate")
    if authorization.funding_status == PaymentStatus.FAILED:
        authorization.funding_status = PaymentStatus.CREATED
        authorization.funding_failure_code = None
        authorization.funding_failure_message = None
    payment.status = PaymentStatus.CREATED
    payment.failure_code = None
    payment.failure_message = None
    payment.completed_at = None
    payment.next_attempt_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(payment)
    return await _payment_response(session, payment)


async def retry_payment_and_commit(
    session: AsyncSession,
    payment_id: UUID,
    *,
    creator_id: UUID,
) -> PaymentResponse:
    try:
        response = await retry_payment(session, payment_id, creator_id=creator_id)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def _lease_next_payment(
    session: AsyncSession,
    *,
    policy: PaymentWorkerPolicy,
) -> LeasedPayment | None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=policy.lease_seconds)
    payment = await session.scalar(
        select(Payment)
        .where(
            or_(
                (
                    (Payment.status == PaymentStatus.CREATED)
                    & (Payment.next_attempt_at.is_not(None))
                    & (Payment.next_attempt_at <= now)
                ),
                (
                    (Payment.status == PaymentStatus.PROCESSING)
                    & (Payment.updated_at <= stale_before)
                ),
            )
        )
        .order_by(Payment.created_at, Payment.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if payment is None:
        return None
    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.id == payment.authorization_id)
        .with_for_update()
    )
    if (
        authorization is None
        or authorization.status != AuthorizationStatus.ACTIVE
        or authorization.provider_authorization_ref is None
    ):
        payment.status = PaymentStatus.FAILED
        payment.failure_code = "payment_configuration_invalid"
        payment.failure_message = "Payment configuration is invalid"
        payment.next_attempt_at = None
        payment.completed_at = now
        await session.commit()
        return None
    if authorization.funding_status == PaymentStatus.FAILED:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = authorization.funding_failure_code or "task_funding_failed"
        payment.failure_message = "Prava task-budget funding failed"
        payment.next_attempt_at = None
        payment.completed_at = now
        await _enqueue_payment_webhook(session, payment, event_type="payment.failed")
        await session.commit()
        return None
    if (
        authorization.funding_status == PaymentStatus.PROCESSING
        and payment.status == PaymentStatus.CREATED
    ):
        await session.rollback()
        return None

    requires_prava_funding = authorization.funding_status != PaymentStatus.SUCCEEDED

    attempt_number = (
        await session.scalar(
            select(func.coalesce(func.max(PaymentAttempt.attempt_number), 0)).where(
                PaymentAttempt.payment_id == payment.id
            )
        )
    ) + 1
    attempt = PaymentAttempt(
        payment_id=payment.id,
        attempt_number=attempt_number,
        provider_session_ref=authorization.provider_session_ref,
        status=PaymentStatus.PROCESSING,
        request_data=(
            {
                "operation": "fund_task_budget_and_credit_worker",
                "task_budget_minor": authorization.total_cap_minor,
                "reward_minor": payment.amount_minor,
                "currency": payment.currency.strip(),
                "reference": authorization.funding_idempotency_key,
            }
            if requires_prava_funding
            else {
                "operation": "credit_prava_funded_wallet",
                "reward_minor": payment.amount_minor,
                "currency": payment.currency.strip(),
            }
        ),
        response_data={},
    )
    session.add(attempt)
    if requires_prava_funding:
        authorization.funding_status = PaymentStatus.PROCESSING
        authorization.funding_failure_code = None
        authorization.funding_failure_message = None
    payment.status = PaymentStatus.PROCESSING
    payment.next_attempt_at = None
    payment.failure_code = None
    payment.failure_message = None
    await session.commit()
    return LeasedPayment(
        payment_id=payment.id,
        authorization_id=authorization.id,
        mandate_id=authorization.provider_authorization_ref,
        funding_amount_minor=authorization.total_cap_minor,
        currency=authorization.currency.strip(),
        funding_idempotency_key=authorization.funding_idempotency_key,
        attempt_number=attempt_number,
        requires_prava_funding=requires_prava_funding,
    )


async def _enqueue_payment_webhook(
    session: AsyncSession,
    payment: Payment,
    *,
    event_type: str,
) -> None:
    from app.schemas.webhook import PaymentCompletedData, WebhookEventType
    from app.services.webhooks import enqueue_webhook_event

    if payment.completed_at is None:
        raise ValueError("completed payment event requires a completion time")
    await enqueue_webhook_event(
        session,
        creator_id=payment.payer_user_id,
        event_type=WebhookEventType(event_type),
        entity_type="payment",
        entity_id=payment.id,
        data=PaymentCompletedData(
            payment_id=payment.id,
            submission_id=payment.submission_id,
            claim_id=payment.claim_id,
            bounty_id=payment.bounty_id,
            task_id=payment.task_id,
            payee_user_id=payment.payee_user_id,
            status=payment.status.value,
            amount_minor=payment.amount_minor,
            currency=payment.currency.strip(),
            completed_at=payment.completed_at,
            failure_code=payment.failure_code,
        ),
        deduplication_key=f"{event_type}:{payment.id}",
    )


async def _finalize_payment_success(
    session: AsyncSession,
    lease: LeasedPayment,
    result: PravaPaymentResult | None,
) -> None:
    payment = await session.scalar(
        select(Payment).where(Payment.id == lease.payment_id).with_for_update()
    )
    attempt = await session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.payment_id == lease.payment_id,
            PaymentAttempt.attempt_number == lease.attempt_number,
        )
    )
    if payment is None or attempt is None or payment.status != PaymentStatus.PROCESSING:
        await session.rollback()
        return
    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.id == lease.authorization_id)
        .with_for_update()
    )
    if authorization is None:
        await session.rollback()
        return
    now = datetime.now(UTC)
    if result is None and authorization.funding_status != PaymentStatus.SUCCEEDED:
        await session.rollback()
        return
    if result is not None:
        authorization.funding_status = PaymentStatus.SUCCEEDED
        authorization.provider_funding_transaction_ref = result.transaction_id
        authorization.funding_failure_code = None
        authorization.funding_failure_message = None
        authorization.funded_at = now
        # The database trigger validates funding before accepting payment success.
        # Flush the task funding state first instead of relying on ORM update order.
        await session.flush()
    payment.status = PaymentStatus.SUCCEEDED
    payment.provider_transaction_ref = result.transaction_id if result is not None else None
    payment.failure_code = None
    payment.failure_message = None
    payment.next_attempt_at = None
    payment.completed_at = now
    attempt.status = PaymentStatus.SUCCEEDED
    attempt.provider_transaction_ref = result.transaction_id if result is not None else None
    attempt.response_data = (
        {
            "status": result.status,
            "operation": "fund_task_budget_and_credit_worker",
            "order_id": result.order_id,
            "deduplicated": result.deduplicated,
            "visa_confirmation": result.visa_confirmation,
        }
        if result is not None
        else {
            "status": "succeeded",
            "operation": "credit_prava_funded_wallet",
        }
    )
    attempt.completed_at = now
    await session.flush()
    await _enqueue_payment_webhook(session, payment, event_type="payment.succeeded")
    await session.commit()


async def _finalize_payment_failure(
    session: AsyncSession,
    lease: LeasedPayment,
    error: PravaGatewayError,
    *,
    policy: PaymentWorkerPolicy,
) -> None:
    payment = await session.scalar(
        select(Payment).where(Payment.id == lease.payment_id).with_for_update()
    )
    attempt = await session.scalar(
        select(PaymentAttempt).where(
            PaymentAttempt.payment_id == lease.payment_id,
            PaymentAttempt.attempt_number == lease.attempt_number,
        )
    )
    authorization = await session.scalar(
        select(PaymentAuthorization)
        .where(PaymentAuthorization.id == lease.authorization_id)
        .with_for_update()
    )
    if (
        payment is None
        or attempt is None
        or authorization is None
        or payment.status != PaymentStatus.PROCESSING
    ):
        await session.rollback()
        return

    now = datetime.now(UTC)
    error_code = error.code.lower()
    if error.transaction_id is not None:
        authorization.provider_funding_transaction_ref = error.transaction_id
    attempt.status = PaymentStatus.FAILED
    attempt.response_data = {
        "status": "failed",
        "error_code": error.code,
        "provider_transaction_ref": error.transaction_id,
    }
    attempt.error_message = "Prava sandbox task-budget charge failed"
    attempt.completed_at = now

    if error.retryable and lease.attempt_number < policy.max_attempts:
        delay = min(
            policy.retry_base_seconds * (2 ** (lease.attempt_number - 1)),
            policy.retry_cap_seconds,
        )
        authorization.funding_status = PaymentStatus.CREATED
        authorization.funding_failure_code = error_code
        authorization.funding_failure_message = "Prava sandbox charge will be retried"
        payment.status = PaymentStatus.CREATED
        payment.failure_code = error_code
        payment.failure_message = "Prava sandbox charge will be retried"
        payment.next_attempt_at = now + timedelta(seconds=delay)
        payment.completed_at = None
    else:
        authorization.funding_status = PaymentStatus.FAILED
        authorization.funding_failure_code = error_code
        authorization.funding_failure_message = "Prava sandbox task-budget charge failed"
        payment.status = PaymentStatus.FAILED
        payment.failure_code = error_code
        payment.failure_message = "Prava sandbox task-budget charge failed"
        payment.next_attempt_at = None
        payment.completed_at = now
        await _enqueue_payment_webhook(session, payment, event_type="payment.failed")
    await session.commit()


class PaymentSessionFactory(Protocol):
    def __call__(self) -> contextlib.AbstractAsyncContextManager[AsyncSession]: ...


async def process_next_payment(
    session_factory: PaymentSessionFactory,
    *,
    runtime: PaymentRuntime,
) -> bool:
    async with session_factory() as session:
        lease = await _lease_next_payment(session, policy=runtime.policy)
    if lease is None:
        return False
    if not lease.requires_prava_funding:
        async with session_factory() as session:
            await _finalize_payment_success(session, lease, None)
        return True
    try:
        result = await runtime.gateway.execute_sandbox_payment(
            mandate_id=lease.mandate_id,
            amount=_minor_to_decimal(lease.funding_amount_minor, lease.currency),
            reference=lease.funding_idempotency_key,
        )
    except PravaGatewayError as error:
        async with session_factory() as session:
            await _finalize_payment_failure(
                session,
                lease,
                error,
                policy=runtime.policy,
            )
        return True
    async with session_factory() as session:
        await _finalize_payment_success(session, lease, result)
    return True
