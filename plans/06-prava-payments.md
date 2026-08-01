# Plan 06: Prava authorization and automatic payouts

## Outcome

A creator authorizes task-level spending through Prava. After a freelancer's work
passes verification, the backend automatically creates and executes one payout for
the exact bounty reward.

The backend stores only Prava/provider references and redacted response data. It
must never receive or persist card number, CVV, or expiry.

## Required Prava contract check

Before implementing the adapter, confirm from the current Prava sandbox docs/SDK:

- how a creator account and freelancer payee are represented;
- how task-level or capped authorization is created;
- payout request and response fields;
- idempotency support;
- authentication and environment URLs;
- webhook signature and payment-status events;
- retry-safe status lookup.

Record the confirmed contract in adapter tests. Do not invent provider fields or
place sandbox card credentials in source code, logs, fixtures, or the database.

## API and internal flow

### Create task authorization

```http
POST /v1/tasks/{task_id}/payment-authorization
```

Request contains only provider-safe authorization references and limits. The
provider-hosted Prava flow handles sensitive payment details.

### Automatic payout

There is no public "pay arbitrary amount" endpoint. A passed verification enqueues
an internal command containing the approved claim ID. The payment service derives
the payer, payee, amount, currency, task, bounty, submission, and authorization from
the database.

## Database

Use:

- `users.prava_account_ref` for the provider account reference;
- `payment_authorizations` for task-specific caps and usage;
- `payments` for one logical payout per approved claim/submission;
- `payment_attempts` for provider retries.

Rules:

- authorization belongs to the task creator and task currency;
- total/per-payment limits cannot exceed the task budget;
- claim is approved and submission verification passed;
- payout amount equals the bounty reward;
- payer is the task creator and payee is the assigned freelancer;
- one claim and submission have at most one logical payment;
- provider calls use a stable idempotency key;
- successful payment increments authorization usage and marks the claim paid once.

## Service flow

1. Receive the approved claim ID from the verification transaction/outbox.
2. Create or load the logical payment using its idempotency key.
3. Validate the active authorization and remaining limits.
4. Create a payment-attempt row.
5. Call the Prava sandbox adapter.
6. Store provider reference and a redacted response.
7. Mark the payment succeeded, failed, or still processing.
8. On success, atomically mark the claim paid and update authorization usage.
9. Emit the payment result for Plan 08.

Retry only transport errors and explicitly retryable provider results. Reuse the
same logical payment and provider idempotency key for every retry.

## Failure behavior

- missing/inactive authorization: do not call Prava; keep payment failed for review;
- cap exceeded or mismatched amount/currency: reject before provider call;
- timeout/unknown result: query by idempotency key before retrying;
- permanent provider failure: store redacted failure data and emit a failed event;
- duplicate verification event: return the existing logical payment.

## Tests

- task authorization validation and cap enforcement;
- exact reward and currency derivation;
- only passed/approved work can pay;
- duplicate events produce one payment;
- timeout then status lookup does not double-pay;
- retry attempts are recorded separately;
- success updates authorization usage and claim state once;
- failure does not mark the claim paid;
- provider payloads/logs contain no raw card data;
- sandbox contract test with non-secret test configuration.

## Definition of done

An approved sandbox submission produces one idempotent Prava payout, retries cannot
duplicate it, database caps remain correct under concurrency, and no sensitive card
credential enters the application.
