from __future__ import annotations

import json
from datetime import UTC, datetime
from random import Random
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.core.config import DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY, Settings
from app.schemas.webhook import (
    MCPRequestCompletedData,
    SubmissionCreatedData,
    VerificationCompletedData,
    WebhookEndpointPutResponse,
    WebhookEventType,
    WebhookPutRequest,
)
from app.services.webhooks import (
    ENCRYPTED_WEBHOOK_URL,
    WebhookDeliveryPolicy,
    WebhookDestinationResolutionError,
    WebhookDestinationValidationError,
    WebhookEventValidationError,
    WebhookSecretError,
    build_webhook_cipher,
    canonical_webhook_event,
    decrypt_signing_secret,
    decrypt_webhook_credentials,
    issue_signing_secret,
    normalize_webhook_destination,
    redact_webhook_response_body,
    resolve_webhook_destination,
    webhook_backoff_seconds,
    webhook_request_headers,
    webhook_signature,
)


class FakeResolver:
    def __init__(self, *addresses: str, error: Exception | None = None) -> None:
        self.addresses = addresses
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        if self.error is not None:
            raise self.error
        return self.addresses


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployments_reject_missing_or_public_webhook_encryption_key(
    app_env: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env=app_env,
            webhook_secret_encryption_keys=[],
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env=app_env,
            webhook_secret_encryption_keys=[DEVELOPMENT_WEBHOOK_ENCRYPTION_KEY],
        )


def test_unknown_environment_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="prod",
            webhook_secret_encryption_keys=[Fernet.generate_key().decode()],
        )


def submission_event_data(**overrides: object) -> SubmissionCreatedData:
    values: dict[str, object] = {
        "submission_id": uuid4(),
        "claim_id": uuid4(),
        "bounty_id": uuid4(),
        "task_id": uuid4(),
        "freelancer_id": uuid4(),
        "revision": 1,
        "submitted_at": datetime(2026, 8, 2, tzinfo=UTC),
        "proof_types": ["screenshot", "url"],
    }
    values.update(overrides)
    return SubmissionCreatedData.model_validate(values)


def test_webhook_request_validates_and_sorts_subscriptions() -> None:
    request = WebhookPutRequest(
        url="https://hooks.example.com/events",
        subscribed_events=[
            "verification.completed",
            "submission.created",
            "payment.failed",
        ],
    )

    assert [event.value for event in request.subscribed_events] == [
        "payment.failed",
        "submission.created",
        "verification.completed",
    ]
    with pytest.raises(ValidationError):
        WebhookPutRequest(
            url="https://hooks.example.com/events",
            subscribed_events=["submission.created", "submission.created"],
        )
    with pytest.raises(ValidationError):
        WebhookPutRequest(
            url="https://hooks.example.com/events",
            subscribed_events=["submission.approved"],
        )


def test_signing_secret_is_recoverable_but_not_stored_in_cleartext() -> None:
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_cipher = build_webhook_cipher([old_key])
    destination_url = "https://hooks.example.com/capability-token?key=query-secret"
    issued = issue_signing_secret(old_cipher, destination_url=destination_url)
    rotated_cipher = build_webhook_cipher([new_key, old_key])

    assert issued.secret.startswith("whsec_")
    assert issued.secret.encode() not in issued.ciphertext
    assert destination_url.encode() not in issued.ciphertext
    assert issued.secret not in repr(issued)
    assert issued.secret_hash.startswith("sha256:")
    credentials = decrypt_webhook_credentials(
        rotated_cipher,
        issued.ciphertext,
        expected_secret_hash=issued.secret_hash,
    )
    assert credentials.signing_secret == issued.secret
    assert credentials.destination_url == destination_url
    assert issued.secret not in repr(credentials)
    assert destination_url not in repr(credentials)
    assert decrypt_signing_secret(rotated_cipher, issued.ciphertext) == issued.secret


def test_webhook_credential_envelope_rejects_bad_hash_and_legacy_plaintext() -> None:
    cipher = build_webhook_cipher([Fernet.generate_key()])
    issued = issue_signing_secret(
        cipher,
        destination_url="https://hooks.example.com/events",
    )

    with pytest.raises(WebhookSecretError):
        decrypt_webhook_credentials(
            cipher,
            issued.ciphertext,
            expected_secret_hash=f"sha256:{'0' * 64}",
        )
    with pytest.raises(WebhookSecretError):
        decrypt_webhook_credentials(
            cipher,
            cipher.encrypt(b"whsec_legacy-plaintext-secret"),
        )

    invalid_envelopes = (
        {
            "destination_url": "https://hooks.example.com/events",
            "signing_secret": issued.secret,
            "version": 2,
        },
        {
            "destination_url": "http://127.0.0.1/private",
            "signing_secret": issued.secret,
            "version": 1,
        },
        {
            "destination_url": ENCRYPTED_WEBHOOK_URL,
            "signing_secret": issued.secret,
            "version": 1,
        },
        {
            "destination_url": "https://hooks.example.com/events",
            "signing_secret": "whsec_invalid",
            "version": 1,
        },
    )
    for envelope in invalid_envelopes:
        ciphertext = cipher.encrypt(
            json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
        )
        with pytest.raises(WebhookSecretError):
            decrypt_webhook_credentials(cipher, ciphertext)


def test_encrypted_webhook_url_sentinel_contains_no_capability() -> None:
    assert ENCRYPTED_WEBHOOK_URL == "https://encrypted.invalid/"


def test_one_time_secret_serializes_but_is_hidden_from_repr() -> None:
    secret = "whsec_do-not-log-this"
    response = WebhookEndpointPutResponse(
        id=uuid4(),
        creator_id=uuid4(),
        url="https://hooks.example.com/events",
        subscribed_events=[WebhookEventType.SUBMISSION_CREATED],
        status="active",
        delivery={
            "success_statuses": "200-299",
            "max_attempts": 6,
            "timeout_seconds": 10,
        },
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
        signing_secret=SecretStr(secret),
    )

    assert response.model_dump(mode="json")["signing_secret"] == secret
    assert secret not in repr(response)


def test_canonical_submission_event_is_stable_and_allowlisted() -> None:
    event_id = UUID("12345678-1234-5678-1234-567812345678")
    created_at = datetime(2026, 8, 2, tzinfo=UTC)
    data = submission_event_data()

    first_payload, first_body = canonical_webhook_event(
        event_id=event_id,
        event_type=WebhookEventType.SUBMISSION_CREATED,
        created_at=created_at,
        data=data,
    )
    second_payload, second_body = canonical_webhook_event(
        event_id=event_id,
        event_type=WebhookEventType.SUBMISSION_CREATED,
        created_at=created_at,
        data=data,
    )

    assert first_payload == second_payload
    assert first_body == second_body
    assert json.loads(first_body) == first_payload
    assert set(first_payload) == {"id", "type", "created_at", "data"}
    assert set(first_payload["data"]) == {
        "submission_id",
        "claim_id",
        "bounty_id",
        "task_id",
        "freelancer_id",
        "revision",
        "submitted_at",
        "proof_types",
    }
    rendered = first_body.decode()
    for forbidden in (
        "external_url",
        "storage_key",
        "metadata",
        "access_token",
        "client_secret",
        "idempotency_key",
        "response_data",
    ):
        assert forbidden not in rendered


def test_event_type_requires_its_exact_safe_data_model() -> None:
    with pytest.raises(WebhookEventValidationError):
        canonical_webhook_event(
            event_id=uuid4(),
            event_type=WebhookEventType.VERIFICATION_COMPLETED,
            created_at=datetime.now(UTC),
            data=submission_event_data(),
        )
    with pytest.raises(WebhookEventValidationError):
        canonical_webhook_event(
            event_id=uuid4(),
            event_type=WebhookEventType.PAYMENT_SUCCEEDED,
            created_at=datetime.now(UTC),
            data=submission_event_data(),
        )


@pytest.mark.parametrize(
    "address",
    ["224.0.0.1", "239.255.255.250", "ff05::1", "fec0::1"],
)
async def test_webhook_destination_rejects_non_unicast_addresses(address: str) -> None:
    with pytest.raises(WebhookDestinationValidationError):
        await resolve_webhook_destination(
            "https://hooks.example.com/events",
            FakeResolver(address),
        )


def test_verification_and_mcp_payload_models_reject_unsafe_extra_fields() -> None:
    with pytest.raises(ValidationError):
        VerificationCompletedData(
            submission_id=uuid4(),
            claim_id=uuid4(),
            bounty_id=uuid4(),
            task_id=uuid4(),
            revision=1,
            method="automatic",
            status="failed",
            verified_at=datetime.now(UTC),
            raw_provider_response={"token": "provider-secret"},
        )
    with pytest.raises(ValidationError):
        MCPRequestCompletedData(
            request_id=uuid4(),
            method="create_task",
            status="failed",
            completed_at=datetime.now(UTC),
            request_data={"idempotency_key": "secret"},
        )


def test_signature_known_answer_and_headers() -> None:
    event_id = UUID("12345678-1234-5678-1234-567812345678")
    timestamp = 1_722_500_000
    body = (
        b'{"created_at":"2026-08-02T00:00:00Z","data":{},'
        b'"id":"12345678-1234-5678-1234-567812345678",'
        b'"type":"submission.created"}'
    )
    signature = webhook_signature(
        signing_secret="whsec_test-secret",
        event_id=event_id,
        timestamp=timestamp,
        body=body,
    )

    assert signature == ("v1=8193ca5192864008df905cba0993a3a8e62af31cec72a083834d614523f1d9de")
    headers = webhook_request_headers(
        signing_secret="whsec_test-secret",
        event_id=event_id,
        event_type="submission.created",
        attempt_number=2,
        timestamp=timestamp,
        body=body,
    )
    assert headers["X-HAH-Webhook-Signature"] == signature
    assert headers["X-HAH-Delivery-Attempt"] == "2"
    assert "whsec_test-secret" not in json.dumps(headers)
    assert (
        webhook_signature(
            signing_secret="whsec_test-secret",
            event_id=event_id,
            timestamp=timestamp,
            body=body + b" ",
        )
        != signature
    )


@pytest.mark.parametrize(
    "url",
    [
        ENCRYPTED_WEBHOOK_URL,
        "http://hooks.example.com/events",
        "https://user:password@hooks.example.com/events",
        "https://hooks.example.com/events#fragment",
        "https://hooks.example.com/event\nInjected: yes",
        "https://hooks.example.com\\@127.0.0.1/events",
        "https://hooks.example.com/%zz",
    ],
)
def test_structurally_unsafe_destination_is_rejected(url: str) -> None:
    with pytest.raises(WebhookDestinationValidationError):
        normalize_webhook_destination(url)


async def test_destination_is_normalized_and_all_addresses_are_vetted() -> None:
    resolver = FakeResolver("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    destination = await resolve_webhook_destination(
        "HTTPS://Hooks.Example.COM:443/a%20path?state=a%20b",
        resolver,
    )

    assert destination.url == "https://hooks.example.com/a%20path?state=a%20b"
    assert destination.request_target == "/a%20path?state=a%20b"
    assert destination.host_header == "hooks.example.com"
    assert destination.addresses == (
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    )
    assert resolver.calls == [("hooks.example.com", 443)]


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.1",),
        ("169.254.169.254",),
        ("::1",),
        ("::ffff:127.0.0.1",),
        ("93.184.216.34", "10.0.0.1"),
    ],
)
async def test_private_or_mixed_dns_answers_are_rejected(addresses: tuple[str, ...]) -> None:
    with pytest.raises(WebhookDestinationValidationError):
        await resolve_webhook_destination(
            "https://hooks.example.com/events",
            FakeResolver(*addresses),
        )


async def test_dns_failure_is_distinct_from_private_destination() -> None:
    with pytest.raises(WebhookDestinationResolutionError):
        await resolve_webhook_destination(
            "https://hooks.example.com/events",
            FakeResolver(error=OSError("resolver unavailable")),
        )


def test_backoff_uses_capped_equal_jitter_windows() -> None:
    policy = WebhookDeliveryPolicy()
    first = webhook_backoff_seconds(1, policy=policy, random_source=Random(10))
    second = webhook_backoff_seconds(2, policy=policy, random_source=Random(10))
    capped = webhook_backoff_seconds(20, policy=policy, random_source=Random(10))

    assert 15 <= first <= 30
    assert 30 <= second <= 60
    assert second >= first
    assert 1_800 <= capped <= 3_600


def test_response_body_is_fully_redacted() -> None:
    assert redact_webhook_response_body(b"") is None
    assert redact_webhook_response_body(b'{"access_token":"receiver-secret"}') == "[redacted]"
