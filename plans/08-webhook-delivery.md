# Plan 08: webhook delivery

## Outcome

A creator or agent can register a webhook endpoint and receive signed, retryable
results for submission, verification, MCP, and payment events.

## API

### Configure an endpoint

```http
PUT /v1/users/{creator_id}/webhook
```

Request contains an HTTPS destination and the supported event subscriptions. Return
the generated signing secret once; store only its secure hash or encrypted secret
material required by the chosen signing design.

### Read configuration

```http
GET /v1/users/{creator_id}/webhook
```

Return destination, subscriptions, and delivery configuration without the signing
secret.

## Events

Initial event types:

- `submission.created`;
- `verification.completed`;
- `payment.succeeded`;
- `payment.failed`;
- `mcp_request.completed`.

Each event payload contains an event ID, type, creation time, related resource IDs,
and the safe result needed by the creator/agent. It does not contain credentials,
raw enrichment payloads, raw Prava responses, or card data.

## Database

Use `webhook_endpoints` and `webhook_deliveries`:

- one current endpoint per creator is enough for the present scope;
- subscriptions are validated against supported event names;
- each event creates a delivery row with a unique event ID;
- delivery row owns payload, attempt count, next attempt, response code, redacted
  response body, and final state;
- pending/retrying rows are indexed for worker polling.

## Delivery flow

1. Business transaction creates a delivery/outbox row in the same commit as its
   final state change.
2. Worker locks one due delivery using `FOR UPDATE SKIP LOCKED`.
3. Serialize the stored payload exactly once.
4. Sign event ID, timestamp, and raw body.
5. POST to the configured HTTPS endpoint with a bounded timeout.
6. Record the response code and a size-limited/redacted body.
7. Mark delivered on any configured success response.
8. Retry transient failures with capped exponential backoff and jitter.
9. Mark permanently failed after the maximum attempt count.

Consumers deduplicate by event ID. A retry reuses the same event ID and payload.

## Failure behavior

- invalid/private/non-HTTPS destination is rejected during configuration;
- redirects are not followed automatically;
- DNS/connection/timeout/429/5xx failures are retryable;
- most other 4xx responses are permanent;
- endpoint configuration changes affect new attempts without modifying the stored
  event payload;
- business operations never roll back because the remote endpoint is unavailable.

## Tests

- supported subscription validation;
- signing and signature verification fixture;
- safe payload for every event type;
- success marks delivered;
- timeout, 429, and 5xx retry with increasing next-attempt times;
- permanent 4xx failure stops retries;
- concurrent workers cannot deliver the same attempt simultaneously;
- retry keeps identical event ID/body;
- secrets and provider data are absent from stored payloads and logs.

## Definition of done

Every required event is saved transactionally, delivered with a verifiable
signature, retried safely, and readable by an agent without exposing sensitive
application or provider data.
