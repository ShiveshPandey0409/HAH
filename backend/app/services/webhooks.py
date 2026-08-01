from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from random import Random, SystemRandom
from typing import Protocol, cast
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationStatus
from app.models.user import User
from app.models.webhook import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.schemas.webhook import (
    CURRENT_WEBHOOK_EVENT_TYPES,
    CurrentWebhookEventData,
    WebhookDeliveryConfiguration,
    WebhookEndpointPutResponse,
    WebhookEndpointResponse,
    WebhookEventEnvelope,
    WebhookEventType,
    WebhookPutRequest,
)

MAX_WEBHOOK_URL_LENGTH = 2048
MAX_DEDUPLICATION_KEY_LENGTH = 255
MAX_ENTITY_TYPE_LENGTH = 64
MAX_DNS_ANSWERS = 32
MAX_RESPONSE_HEADER_BYTES = 65_536
SIGNING_SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"
WEBHOOK_CREDENTIAL_ENVELOPE_VERSION = 1
MAX_WEBHOOK_CREDENTIAL_ENVELOPE_BYTES = 4_096
ENCRYPTED_WEBHOOK_URL = "https://encrypted.invalid/"


class WebhookCreatorNotFoundError(Exception):
    pass


class WebhookCreatorCannotCreateError(Exception):
    pass


class WebhookEndpointNotFoundError(Exception):
    pass


class WebhookDestinationValidationError(Exception):
    pass


class WebhookDestinationResolutionError(Exception):
    pass


class WebhookSecretError(Exception):
    pass


class WebhookEventValidationError(Exception):
    pass


class WebhookTransportError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WebhookDeliveryPolicy:
    max_attempts: int = 6
    timeout_seconds: float = 10.0
    lease_seconds: float = 30.0
    backoff_base_seconds: float = 30.0
    backoff_cap_seconds: float = 3_600.0
    poll_interval_seconds: float = 1.0
    dns_timeout_seconds: float = 3.0
    response_body_limit_bytes: int = 4_096

    def __post_init__(self) -> None:
        positive_values = (
            self.max_attempts,
            self.timeout_seconds,
            self.lease_seconds,
            self.backoff_base_seconds,
            self.backoff_cap_seconds,
            self.poll_interval_seconds,
            self.dns_timeout_seconds,
            self.response_body_limit_bytes,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("webhook delivery policy values must be positive")
        if self.lease_seconds <= self.dns_timeout_seconds + self.timeout_seconds:
            raise ValueError("webhook lease must exceed DNS and request timeouts")
        if self.backoff_base_seconds > self.backoff_cap_seconds:
            raise ValueError("webhook backoff base cannot exceed its cap")

    def as_response(self) -> WebhookDeliveryConfiguration:
        return WebhookDeliveryConfiguration(
            max_attempts=self.max_attempts,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class NormalizedWebhookDestination:
    url: str
    hostname: str
    port: int
    request_target: str
    host_header: str


@dataclass(frozen=True, slots=True)
class ResolvedWebhookDestination(NormalizedWebhookDestination):
    addresses: tuple[str, ...]


class WebhookResolver(Protocol):
    async def resolve(self, hostname: str, port: int) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class WebhookHTTPResponse:
    status_code: int
    body: bytes = b""

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("webhook response status is invalid")


class WebhookTransport(Protocol):
    async def post(
        self,
        destination: ResolvedWebhookDestination,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        response_body_limit_bytes: int,
    ) -> WebhookHTTPResponse: ...


class WebhookClock(Protocol):
    def now(self) -> datetime: ...


class SystemWebhookClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemWebhookResolver:
    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        try:
            answers = await loop.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, UnicodeError) as error:
            raise WebhookDestinationResolutionError("destination could not be resolved") from error
        return tuple(dict.fromkeys(answer[4][0] for answer in answers))


class PinnedHTTPSWebhookTransport:
    """Minimal HTTP/1.1 transport that never performs a second DNS lookup."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    async def post(
        self,
        destination: ResolvedWebhookDestination,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        response_body_limit_bytes: int,
    ) -> WebhookHTTPResponse:
        if timeout_seconds <= 0 or response_body_limit_bytes <= 0:
            raise ValueError("webhook transport bounds must be positive")
        safe_headers = _validate_outbound_headers(headers)
        request = _build_http_request(destination, safe_headers, body)

        try:
            async with asyncio.timeout(timeout_seconds):
                last_error: Exception | None = None
                for address in destination.addresses:
                    try:
                        return await self._post_to_address(
                            address,
                            destination,
                            request,
                            response_body_limit_bytes=response_body_limit_bytes,
                        )
                    except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as error:
                        last_error = error
                if last_error is None:
                    raise WebhookTransportError("destination has no vetted address")
                raise WebhookTransportError("webhook connection failed") from last_error
        except TimeoutError as error:
            raise WebhookTransportError("webhook request timed out") from error
        except (asyncio.LimitOverrunError, ValueError) as error:
            raise WebhookTransportError("webhook response was invalid") from error

    async def _post_to_address(
        self,
        address: str,
        destination: ResolvedWebhookDestination,
        request: bytes,
        *,
        response_body_limit_bytes: int,
    ) -> WebhookHTTPResponse:
        reader, writer = await asyncio.open_connection(
            host=address,
            port=destination.port,
            ssl=self._ssl_context,
            server_hostname=destination.hostname,
            limit=MAX_RESPONSE_HEADER_BYTES,
        )
        try:
            writer.write(request)
            await writer.drain()
            header_block = await reader.readuntil(b"\r\n\r\n")
            if len(header_block) > MAX_RESPONSE_HEADER_BYTES:
                raise ValueError("webhook response headers are too large")
            status_code, response_headers = _parse_http_response_headers(header_block)
            response_body = await _read_bounded_response_body(
                reader,
                status_code=status_code,
                response_headers=response_headers,
                limit=response_body_limit_bytes,
            )
            return WebhookHTTPResponse(status_code=status_code, body=response_body)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


@dataclass(frozen=True, slots=True)
class WebhookRuntime:
    cipher: MultiFernet = field(repr=False)
    resolver: WebhookResolver
    transport: WebhookTransport | None = None
    policy: WebhookDeliveryPolicy = field(default_factory=WebhookDeliveryPolicy)


@dataclass(frozen=True, slots=True)
class IssuedSigningSecret:
    secret: str = field(repr=False)
    secret_hash: str
    ciphertext: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class WebhookCredentials:
    signing_secret: str = field(repr=False)
    destination_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EnqueuedWebhookEvent:
    delivery_id: UUID
    event_id: UUID
    created_at: datetime
    created: bool


@dataclass(frozen=True, slots=True)
class LeasedWebhookDelivery:
    delivery_id: UUID
    endpoint_id: UUID
    event_id: UUID
    event_type: str
    payload_body: bytes = field(repr=False)
    attempt_number: int
    lease_token: UUID = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExhaustedWebhookDelivery:
    delivery_id: UUID


type WebhookLeaseResult = LeasedWebhookDelivery | ExhaustedWebhookDelivery | None


@dataclass(frozen=True, slots=True)
class WebhookAttemptOutcome:
    delivered: bool
    retryable: bool
    response_code: int | None = None
    response_body: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.delivered and self.retryable:
            raise ValueError("a delivered webhook cannot be retryable")
        if self.delivered and self.error_code is not None:
            raise ValueError("a delivered webhook cannot have an error code")
        if self.response_code is not None and not 100 <= self.response_code <= 599:
            raise ValueError("webhook response status is invalid")
        if self.error_code is not None and len(self.error_code) > 100:
            raise ValueError("webhook error code is too long")
        if self.response_body is not None and len(self.response_body) > 1024:
            raise ValueError("webhook response marker is too long")


class WebhookSessionFactory(Protocol):
    def __call__(self) -> contextlib.AbstractAsyncContextManager[AsyncSession]: ...


def build_webhook_cipher(keys: Sequence[str | bytes | SecretStr]) -> MultiFernet:
    fernets: list[Fernet] = []
    for value in keys:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        encoded = value.encode("ascii") if isinstance(value, str) else value
        try:
            fernets.append(Fernet(encoded))
        except (TypeError, ValueError) as error:
            raise WebhookSecretError("webhook encryption key is invalid") from error
    if not fernets:
        raise WebhookSecretError("at least one webhook encryption key is required")
    return MultiFernet(fernets)


def issue_signing_secret(
    cipher: MultiFernet,
    *,
    destination_url: str,
) -> IssuedSigningSecret:
    normalized_url = normalize_webhook_destination(destination_url).url
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    secret = f"{SIGNING_SECRET_PREFIX}{token}"
    secret_bytes = secret.encode("utf-8")
    envelope = json.dumps(
        {
            "destination_url": normalized_url,
            "signing_secret": secret,
            "version": WEBHOOK_CREDENTIAL_ENVELOPE_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return IssuedSigningSecret(
        secret=secret,
        secret_hash=f"sha256:{hashlib.sha256(secret_bytes).hexdigest()}",
        ciphertext=cipher.encrypt(envelope),
    )


def decrypt_webhook_credentials(
    cipher: MultiFernet,
    ciphertext: bytes,
    *,
    expected_secret_hash: str | None = None,
) -> WebhookCredentials:
    try:
        plaintext = cipher.decrypt(ciphertext)
        if len(plaintext) > MAX_WEBHOOK_CREDENTIAL_ENVELOPE_BYTES:
            raise ValueError
        envelope = json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise WebhookSecretError("webhook signing secret is unavailable") from error

    if not isinstance(envelope, dict) or set(envelope) != {
        "destination_url",
        "signing_secret",
        "version",
    }:
        raise WebhookSecretError("webhook signing secret is unavailable")
    if type(envelope["version"]) is not int or (
        envelope["version"] != WEBHOOK_CREDENTIAL_ENVELOPE_VERSION
    ):
        raise WebhookSecretError("webhook signing secret is unavailable")

    secret = envelope["signing_secret"]
    destination_url = envelope["destination_url"]
    if not isinstance(secret, str) or re.fullmatch(r"whsec_[A-Za-z0-9_-]{43}", secret) is None:
        raise WebhookSecretError("webhook signing secret is unavailable")
    if not isinstance(destination_url, str):
        raise WebhookSecretError("webhook signing secret is unavailable")

    actual_hash = f"sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()}"
    if expected_secret_hash is not None and not hmac.compare_digest(
        actual_hash,
        expected_secret_hash,
    ):
        raise WebhookSecretError("webhook signing secret is unavailable")
    try:
        normalized_url = normalize_webhook_destination(destination_url).url
    except WebhookDestinationValidationError as error:
        raise WebhookSecretError("webhook signing secret is unavailable") from error
    if normalized_url != destination_url or destination_url == ENCRYPTED_WEBHOOK_URL:
        raise WebhookSecretError("webhook signing secret is unavailable")
    return WebhookCredentials(
        signing_secret=secret,
        destination_url=destination_url,
    )


def decrypt_signing_secret(cipher: MultiFernet, ciphertext: bytes) -> str:
    return decrypt_webhook_credentials(cipher, ciphertext).signing_secret


def normalize_webhook_destination(url: str) -> NormalizedWebhookDestination:
    if (
        not url
        or len(url) > MAX_WEBHOOK_URL_LENGTH
        or "\\" in url
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
        )
    ):
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    if re.search(r"%(?![0-9A-Fa-f]{2})", url):
        raise WebhookDestinationValidationError("webhook destination is not allowed")

    try:
        parsed = urlsplit(url)
        raw_hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise WebhookDestinationValidationError("webhook destination is not allowed") from error

    if (
        parsed.scheme.lower() != "https"
        or raw_hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    if "%" in raw_hostname:
        raise WebhookDestinationValidationError("webhook destination is not allowed")

    hostname = _normalize_hostname(raw_hostname)
    port = 443 if port is None else port
    if not 1 <= port <= 65_535:
        raise WebhookDestinationValidationError("webhook destination is not allowed")

    quoted_path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    quoted_query = quote(parsed.query, safe="=&;%:+,/?@!$'()*-._~")
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = authority_host if port == 443 else f"{authority_host}:{port}"
    normalized_url = urlunsplit(("https", authority, quoted_path, quoted_query, ""))
    if len(normalized_url) > MAX_WEBHOOK_URL_LENGTH or normalized_url == ENCRYPTED_WEBHOOK_URL:
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    request_target = quoted_path + (f"?{quoted_query}" if quoted_query else "")
    return NormalizedWebhookDestination(
        url=normalized_url,
        hostname=hostname,
        port=port,
        request_target=request_target,
        host_header=authority,
    )


async def resolve_webhook_destination(
    url: str,
    resolver: WebhookResolver,
    *,
    timeout_seconds: float = 3.0,
) -> ResolvedWebhookDestination:
    normalized = normalize_webhook_destination(url)
    try:
        literal_address = ipaddress.ip_address(normalized.hostname)
    except ValueError:
        literal_address = None

    if literal_address is not None:
        addresses = (str(literal_address),)
    else:
        try:
            async with asyncio.timeout(timeout_seconds):
                resolved = await resolver.resolve(normalized.hostname, normalized.port)
        except WebhookDestinationResolutionError:
            raise
        except (OSError, TimeoutError) as error:
            raise WebhookDestinationResolutionError("destination could not be resolved") from error
        addresses = tuple(dict.fromkeys(str(address) for address in resolved))

    if not addresses or len(addresses) > MAX_DNS_ANSWERS:
        raise WebhookDestinationResolutionError("destination could not be resolved")
    canonical_addresses: list[str] = []
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise WebhookDestinationResolutionError(
                "destination returned an invalid address"
            ) from error
        if not _is_globally_routable(parsed_address):
            raise WebhookDestinationValidationError("webhook destination is not allowed")
        canonical_addresses.append(str(parsed_address))

    return ResolvedWebhookDestination(
        url=normalized.url,
        hostname=normalized.hostname,
        port=normalized.port,
        request_target=normalized.request_target,
        host_header=normalized.host_header,
        addresses=tuple(canonical_addresses),
    )


async def configure_webhook_endpoint(
    session: AsyncSession,
    *,
    creator_id: UUID,
    data: WebhookPutRequest,
    cipher: MultiFernet,
    resolver: WebhookResolver,
    policy: WebhookDeliveryPolicy,
) -> WebhookEndpointPutResponse:
    destination = await resolve_webhook_destination(
        data.url,
        resolver,
        timeout_seconds=policy.dns_timeout_seconds,
    )
    issued = issue_signing_secret(cipher, destination_url=destination.url)

    try:
        creator = await session.scalar(select(User).where(User.id == creator_id).with_for_update())
        if creator is None:
            raise WebhookCreatorNotFoundError
        if not creator.can_create_tasks:
            raise WebhookCreatorCannotCreateError

        endpoint = await session.scalar(
            select(WebhookEndpoint)
            .where(
                WebhookEndpoint.creator_id == creator_id,
                WebhookEndpoint.status == IntegrationStatus.ACTIVE,
            )
            .with_for_update()
        )
        if endpoint is None:
            endpoint = WebhookEndpoint(
                creator_id=creator_id,
                url=ENCRYPTED_WEBHOOK_URL,
                secret_hash=issued.secret_hash,
                secret_ciphertext=issued.ciphertext,
                subscribed_events=[event.value for event in data.subscribed_events],
                status=IntegrationStatus.ACTIVE,
            )
            session.add(endpoint)
        else:
            endpoint.url = ENCRYPTED_WEBHOOK_URL
            endpoint.secret_hash = issued.secret_hash
            endpoint.secret_ciphertext = issued.ciphertext
            endpoint.subscribed_events = [event.value for event in data.subscribed_events]
            endpoint.status = IntegrationStatus.ACTIVE

        await session.flush()
        await session.commit()
    except (WebhookCreatorNotFoundError, WebhookCreatorCannotCreateError):
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise

    await session.refresh(endpoint)
    return _endpoint_put_response(
        endpoint,
        destination_url=destination.url,
        signing_secret=issued.secret,
        policy=policy,
    )


async def get_webhook_endpoint(
    session: AsyncSession,
    *,
    creator_id: UUID,
    cipher: MultiFernet,
    policy: WebhookDeliveryPolicy,
) -> WebhookEndpointResponse:
    creator = await session.get(User, creator_id)
    if creator is None:
        raise WebhookCreatorNotFoundError
    if not creator.can_create_tasks:
        raise WebhookCreatorCannotCreateError
    endpoint = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.creator_id == creator_id,
            WebhookEndpoint.status == IntegrationStatus.ACTIVE,
        )
    )
    if endpoint is None:
        raise WebhookEndpointNotFoundError
    if endpoint.secret_ciphertext is None:
        raise WebhookSecretError("webhook signing secret is unavailable")
    credentials = decrypt_webhook_credentials(
        cipher,
        endpoint.secret_ciphertext,
        expected_secret_hash=endpoint.secret_hash,
    )
    return _endpoint_response(
        endpoint,
        destination_url=credentials.destination_url,
        policy=policy,
    )


def canonical_webhook_event(
    *,
    event_id: UUID,
    event_type: WebhookEventType,
    created_at: datetime,
    data: CurrentWebhookEventData,
    payload_limit_bytes: int = 65_536,
) -> tuple[dict[str, object], bytes]:
    if event_type not in CURRENT_WEBHOOK_EVENT_TYPES:
        raise WebhookEventValidationError("payment webhook emission is not enabled")
    try:
        envelope = WebhookEventEnvelope(
            id=event_id,
            type=event_type,
            created_at=_aware_utc(created_at),
            data=data,
        )
        payload = cast(dict[str, object], envelope.model_dump(mode="json"))
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WebhookEventValidationError("webhook event is invalid") from error
    if not body or len(body) > payload_limit_bytes:
        raise WebhookEventValidationError("webhook event exceeds the payload limit")
    return payload, body


async def enqueue_webhook_event(
    session: AsyncSession,
    *,
    creator_id: UUID,
    event_type: WebhookEventType,
    entity_type: str,
    entity_id: UUID,
    data: CurrentWebhookEventData,
    deduplication_key: str,
    created_at: datetime | None = None,
    event_id: UUID | None = None,
    payload_limit_bytes: int = 65_536,
) -> EnqueuedWebhookEvent | None:
    entity_type = entity_type.strip()
    deduplication_key = deduplication_key.strip()
    if (
        not entity_type
        or len(entity_type) > MAX_ENTITY_TYPE_LENGTH
        or not re.fullmatch(r"[a-z][a-z0-9_]*", entity_type)
        or not deduplication_key
        or len(deduplication_key) > MAX_DEDUPLICATION_KEY_LENGTH
    ):
        raise WebhookEventValidationError("webhook event identity is invalid")
    if event_type not in CURRENT_WEBHOOK_EVENT_TYPES:
        raise WebhookEventValidationError("payment webhook emission is not enabled")

    endpoint = await session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.creator_id == creator_id,
            WebhookEndpoint.status == IntegrationStatus.ACTIVE,
        )
    )
    if endpoint is None or event_type.value not in endpoint.subscribed_events:
        return None

    event_id = event_id or uuid4()
    created_at = _aware_utc(created_at or datetime.now(UTC))
    payload, payload_body = canonical_webhook_event(
        event_id=event_id,
        event_type=event_type,
        created_at=created_at,
        data=data,
        payload_limit_bytes=payload_limit_bytes,
    )
    statement = (
        pg_insert(WebhookDelivery)
        .values(
            endpoint_id=endpoint.id,
            event_id=event_id,
            event_type=event_type.value,
            entity_type=entity_type,
            entity_id=entity_id,
            deduplication_key=deduplication_key,
            payload=payload,
            payload_body=payload_body,
            status=DeliveryStatus.PENDING,
            attempt_count=0,
            next_attempt_at=created_at,
            created_at=created_at,
        )
        .on_conflict_do_nothing(
            index_elements=[WebhookDelivery.endpoint_id, WebhookDelivery.deduplication_key]
        )
        .returning(WebhookDelivery.id)
    )
    delivery_id = await session.scalar(statement)
    if delivery_id is not None:
        return EnqueuedWebhookEvent(
            delivery_id=delivery_id,
            event_id=event_id,
            created_at=created_at,
            created=True,
        )

    existing = (
        await session.execute(
            select(
                WebhookDelivery.id,
                WebhookDelivery.event_id,
                WebhookDelivery.created_at,
            ).where(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.deduplication_key == deduplication_key,
            )
        )
    ).one()
    return EnqueuedWebhookEvent(
        delivery_id=existing.id,
        event_id=existing.event_id,
        created_at=existing.created_at,
        created=False,
    )


def webhook_signature(
    *,
    signing_secret: str,
    event_id: UUID,
    timestamp: int,
    body: bytes,
) -> str:
    if timestamp < 0:
        raise ValueError("webhook signature timestamp cannot be negative")
    signed = f"{str(event_id).lower()}.{timestamp}.".encode("ascii") + body
    digest = hmac.new(signing_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def webhook_request_headers(
    *,
    signing_secret: str,
    event_id: UUID,
    event_type: str,
    attempt_number: int,
    timestamp: int,
    body: bytes,
) -> dict[str, str]:
    if attempt_number <= 0:
        raise ValueError("webhook attempt number must be positive")
    return {
        "Content-Type": "application/json",
        "User-Agent": "hah-webhooks/1",
        "X-HAH-Event-Id": str(event_id).lower(),
        "X-HAH-Event-Type": event_type,
        "X-HAH-Webhook-Timestamp": str(timestamp),
        "X-HAH-Delivery-Attempt": str(attempt_number),
        "X-HAH-Webhook-Signature": webhook_signature(
            signing_secret=signing_secret,
            event_id=event_id,
            timestamp=timestamp,
            body=body,
        ),
    }


async def lease_due_webhook_delivery(
    session: AsyncSession,
    *,
    now: datetime,
    policy: WebhookDeliveryPolicy,
    lease_token: UUID | None = None,
) -> WebhookLeaseResult:
    now = _aware_utc(now)
    try:
        delivery = await session.scalar(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRYING]),
                WebhookDelivery.next_attempt_at <= now,
                or_(
                    WebhookDelivery.lease_expires_at.is_(None),
                    WebhookDelivery.lease_expires_at <= now,
                ),
            )
            .order_by(
                WebhookDelivery.next_attempt_at,
                WebhookDelivery.created_at,
                WebhookDelivery.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if delivery is None:
            await session.rollback()
            return None
        if delivery.attempt_count >= policy.max_attempts:
            delivery.status = DeliveryStatus.FAILED
            delivery.failed_at = now
            delivery.next_attempt_at = None
            delivery.last_error = delivery.last_error or "attempts_exhausted"
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery_id = delivery.id
            await session.commit()
            return ExhaustedWebhookDelivery(delivery_id=delivery_id)

        token = lease_token or uuid4()
        delivery.attempt_count += 1
        delivery.last_attempt_at = now
        delivery.lease_token = token
        delivery.lease_expires_at = now + timedelta(seconds=policy.lease_seconds)
        leased = LeasedWebhookDelivery(
            delivery_id=delivery.id,
            endpoint_id=delivery.endpoint_id,
            event_id=delivery.event_id,
            event_type=delivery.event_type,
            payload_body=bytes(delivery.payload_body),
            attempt_number=delivery.attempt_count,
            lease_token=token,
        )
        await session.commit()
        return leased
    except Exception:
        await session.rollback()
        raise


async def attempt_webhook_delivery(
    session: AsyncSession,
    *,
    leased: LeasedWebhookDelivery,
    runtime: WebhookRuntime,
    now: datetime,
) -> WebhookAttemptOutcome:
    endpoint = await session.get(WebhookEndpoint, leased.endpoint_id)
    if endpoint is None:
        return _permanent_outcome("endpoint_missing")
    if endpoint.status != IntegrationStatus.ACTIVE:
        return _permanent_outcome("endpoint_disabled")
    if endpoint.secret_ciphertext is None:
        return _permanent_outcome("secret_unavailable")
    if runtime.transport is None:
        return WebhookAttemptOutcome(
            delivered=False,
            retryable=True,
            error_code="transport_unavailable",
        )

    try:
        credentials = decrypt_webhook_credentials(
            runtime.cipher,
            endpoint.secret_ciphertext,
            expected_secret_hash=endpoint.secret_hash,
        )
    except WebhookSecretError:
        return _permanent_outcome("secret_unavailable")
    try:
        destination = await resolve_webhook_destination(
            credentials.destination_url,
            runtime.resolver,
            timeout_seconds=runtime.policy.dns_timeout_seconds,
        )
    except WebhookDestinationValidationError:
        return _permanent_outcome("destination_not_allowed")
    except WebhookDestinationResolutionError:
        return WebhookAttemptOutcome(
            delivered=False,
            retryable=True,
            error_code="dns_failure",
        )

    timestamp = int(_aware_utc(now).timestamp())
    headers = webhook_request_headers(
        signing_secret=credentials.signing_secret,
        event_id=leased.event_id,
        event_type=leased.event_type,
        attempt_number=leased.attempt_number,
        timestamp=timestamp,
        body=leased.payload_body,
    )
    try:
        response = await runtime.transport.post(
            destination,
            headers=headers,
            body=leased.payload_body,
            timeout_seconds=runtime.policy.timeout_seconds,
            response_body_limit_bytes=runtime.policy.response_body_limit_bytes,
        )
    except (WebhookTransportError, TimeoutError, OSError):
        return WebhookAttemptOutcome(
            delivered=False,
            retryable=True,
            error_code="transport_failure",
        )

    response_body = redact_webhook_response_body(response.body)
    if 200 <= response.status_code <= 299:
        return WebhookAttemptOutcome(
            delivered=True,
            retryable=False,
            response_code=response.status_code,
            response_body=response_body,
        )
    if response.status_code == 429 or 500 <= response.status_code <= 599:
        return WebhookAttemptOutcome(
            delivered=False,
            retryable=True,
            response_code=response.status_code,
            response_body=response_body,
            error_code=f"http_{response.status_code}",
        )
    return WebhookAttemptOutcome(
        delivered=False,
        retryable=False,
        response_code=response.status_code,
        response_body=response_body,
        error_code=f"http_{response.status_code}",
    )


async def finalize_webhook_delivery(
    session: AsyncSession,
    *,
    leased: LeasedWebhookDelivery,
    outcome: WebhookAttemptOutcome,
    now: datetime,
    policy: WebhookDeliveryPolicy,
    random_source: Random,
) -> bool:
    now = _aware_utc(now)
    try:
        delivery = await session.scalar(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.id == leased.delivery_id,
                WebhookDelivery.lease_token == leased.lease_token,
            )
            .with_for_update()
        )
        if delivery is None:
            await session.rollback()
            return False

        delivery.last_response_code = outcome.response_code
        delivery.last_response_body = outcome.response_body
        delivery.last_error = outcome.error_code
        delivery.lease_token = None
        delivery.lease_expires_at = None
        if outcome.delivered:
            delivery.status = DeliveryStatus.DELIVERED
            delivery.delivered_at = now
            delivery.failed_at = None
            delivery.next_attempt_at = None
        elif outcome.retryable and delivery.attempt_count < policy.max_attempts:
            delivery.status = DeliveryStatus.RETRYING
            delivery.delivered_at = None
            delivery.failed_at = None
            delivery.next_attempt_at = now + timedelta(
                seconds=webhook_backoff_seconds(
                    delivery.attempt_count,
                    policy=policy,
                    random_source=random_source,
                )
            )
        else:
            delivery.status = DeliveryStatus.FAILED
            delivery.delivered_at = None
            delivery.failed_at = now
            delivery.next_attempt_at = None
        await session.commit()
        return True
    except Exception:
        await session.rollback()
        raise


async def process_next_webhook_delivery(
    session_factory: WebhookSessionFactory,
    *,
    runtime: WebhookRuntime,
    clock: WebhookClock | None = None,
    random_source: Random | None = None,
) -> bool:
    clock = clock or SystemWebhookClock()
    random_source = random_source or SystemRandom()
    async with session_factory() as session:
        lease = await lease_due_webhook_delivery(
            session,
            now=clock.now(),
            policy=runtime.policy,
        )
    if lease is None:
        return False
    if isinstance(lease, ExhaustedWebhookDelivery):
        return True

    try:
        async with session_factory() as session:
            outcome = await attempt_webhook_delivery(
                session,
                leased=lease,
                runtime=runtime,
                now=clock.now(),
            )
    except Exception:
        outcome = WebhookAttemptOutcome(
            delivered=False,
            retryable=True,
            error_code="internal_failure",
        )
    async with session_factory() as session:
        await finalize_webhook_delivery(
            session,
            leased=lease,
            outcome=outcome,
            now=clock.now(),
            policy=runtime.policy,
            random_source=random_source,
        )
    return True


def webhook_backoff_seconds(
    attempt_count: int,
    *,
    policy: WebhookDeliveryPolicy,
    random_source: Random,
) -> float:
    if attempt_count <= 0:
        raise ValueError("webhook attempt count must be positive")
    window = min(
        policy.backoff_cap_seconds,
        policy.backoff_base_seconds * (2 ** (attempt_count - 1)),
    )
    return (window / 2) + random_source.uniform(0, window / 2)


def redact_webhook_response_body(body: bytes) -> str | None:
    return None if not body else "[redacted]"


def _endpoint_response(
    endpoint: WebhookEndpoint,
    *,
    destination_url: str,
    policy: WebhookDeliveryPolicy,
) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        id=endpoint.id,
        creator_id=endpoint.creator_id,
        url=destination_url,
        subscribed_events=endpoint.subscribed_events,
        status=endpoint.status,
        delivery=policy.as_response(),
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _endpoint_put_response(
    endpoint: WebhookEndpoint,
    *,
    destination_url: str,
    signing_secret: str,
    policy: WebhookDeliveryPolicy,
) -> WebhookEndpointPutResponse:
    base = _endpoint_response(
        endpoint,
        destination_url=destination_url,
        policy=policy,
    )
    return WebhookEndpointPutResponse(
        **base.model_dump(),
        signing_secret=SecretStr(signing_secret),
    )


def _normalize_hostname(hostname: str) -> str:
    hostname = hostname.rstrip(".")
    if not hostname:
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal)
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise WebhookDestinationValidationError("webhook destination is not allowed") from error
    if len(ascii_hostname) > 253:
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    label_pattern = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if any(not label_pattern.fullmatch(label) for label in ascii_hostname.split(".")):
        raise WebhookDestinationValidationError("webhook destination is not allowed")
    return ascii_hostname


def _is_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not any(
        (
            address.is_multicast,
            address.is_unspecified,
            address.is_reserved,
            address.is_loopback,
            address.is_link_local,
            getattr(address, "is_site_local", False),
            address.is_private,
        )
    )


def _validate_outbound_headers(headers: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in headers.items():
        if (
            not re.fullmatch(r"[A-Za-z0-9-]+", name)
            or "\r" in value
            or "\n" in value
            or any(ord(character) < 32 and character != "\t" for character in value)
        ):
            raise ValueError("webhook request header is invalid")
        if name.lower() in {"host", "content-length", "connection"}:
            raise ValueError("webhook request contains a reserved header")
        safe[name] = value
    return safe


def _build_http_request(
    destination: ResolvedWebhookDestination,
    headers: Mapping[str, str],
    body: bytes,
) -> bytes:
    lines = [
        f"POST {destination.request_target} HTTP/1.1",
        f"Host: {destination.host_header}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{name}: {value}" for name, value in headers.items()),
        "",
        "",
    ]
    try:
        head = "\r\n".join(lines).encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("webhook request headers must be ASCII") from error
    return head + body


def _parse_http_response_headers(header_block: bytes) -> tuple[int, dict[str, str]]:
    try:
        lines = header_block.decode("iso-8859-1").split("\r\n")
        version, status, *_ = lines[0].split(" ")
        status_code = int(status)
    except (UnicodeError, ValueError) as error:
        raise ValueError("webhook response status is invalid") from error
    if version not in {"HTTP/1.0", "HTTP/1.1"} or not 100 <= status_code <= 599:
        raise ValueError("webhook response status is invalid")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t" or ":" not in line:
            raise ValueError("webhook response headers are invalid")
        name, value = line.split(":", 1)
        normalized_name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+", normalized_name):
            raise ValueError("webhook response headers are invalid")
        headers[normalized_name] = value.strip()
    return status_code, headers


async def _read_bounded_response_body(
    reader: asyncio.StreamReader,
    *,
    status_code: int,
    response_headers: Mapping[str, str],
    limit: int,
) -> bytes:
    if status_code in {204, 304} or 100 <= status_code <= 199:
        return b""
    content_length = response_headers.get("content-length")
    if content_length is not None:
        try:
            expected = int(content_length)
        except ValueError as error:
            raise ValueError("webhook response content length is invalid") from error
        if expected < 0:
            raise ValueError("webhook response content length is invalid")
        if expected == 0:
            return b""
        wanted = min(expected, limit + 1)
        try:
            return await reader.readexactly(wanted)
        except asyncio.IncompleteReadError as error:
            return error.partial
    return await reader.read(limit + 1)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("webhook timestamp must include a timezone")
    return value.astimezone(UTC)


def _permanent_outcome(error_code: str) -> WebhookAttemptOutcome:
    return WebhookAttemptOutcome(
        delivered=False,
        retryable=False,
        error_code=error_code,
    )
