# Plan 06: Prava authorization and automatic payouts

## Outcome

A creator authorizes one task budget through Prava. This is recorded as task-reserved
capacity, not a creator/HAH wallet. After a freelancer's work passes verification,
the backend funds that task budget once when necessary and appends one internal wallet
credit for the exact bounty reward to the completer only.

The backend stores only Prava/provider references and redacted response data. It
must never receive or persist card number, CVV, or expiry.

OAuth user delegation on `/mcp` is not sufficient authorization to fund a task. The
agent can request a task-bound Prava approval URL, but the shared sandbox payer must
approve the exact merchant, task-budget cap, and currency on Prava's hosted passkey
surface. There is no public or MCP endpoint for an arbitrary amount or payee.
Each new task requires its own task-bound approval; the response exposes the current
task reservation and the aggregate still reserved by the creator's other tasks.

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

### Automatic task funding and reward credit

There is no public "pay arbitrary amount" endpoint. A passed verification enqueues
an internal command containing the approved claim ID. If the task is not funded yet,
the worker makes one idempotent Prava sandbox charge for the full authorized task
budget. It then credits the exact bounty reward to the assigned user's append-only
internal wallet. Later verified rewards reuse that task funding and do not create
additional external charges.

## Database

Use:

- `users.prava_account_ref` for the provider account reference;
- `payment_authorizations` for task-specific caps and usage;
- `payments` for one logical internal reward per approved claim/submission;
- `payment_attempts` for provider retries.
- `wallet_entries` for append-only, non-redeemable reward credits.

Rules:

- authorization belongs to the task creator and task currency;
- total/per-payment limits cannot exceed the task budget;
- claim is approved and submission verification passed;
- payout amount equals the bounty reward;
- payer is the task creator and payee is the assigned freelancer;
- one claim and submission have at most one logical payment;
- the single task-funding provider call uses a stable task idempotency key;
- successful payment increments authorization usage and marks the claim paid once.

## Service flow

1. Receive the approved claim ID inside the verification transaction.
2. Create or load the logical reward using its submission idempotency key.
3. Validate the active task authorization and remaining internal limits.
4. If task funding is absent, charge the full task budget once through Prava REST
   using a task-scoped idempotency reference.
5. Immediately discard the sandbox credential and store only safe references.
6. In `prava_sandbox`, report the official sandbox outcome; production must first execute
   a real merchant processor checkout.
7. Atomically mark the reward succeeded, mark the claim paid, append one wallet
   credit, and update allocated authorization usage.
8. Later rewards skip the Prava call and allocate from the already funded task.
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

An approved sandbox submission produces one idempotent internal wallet reward, one
task creates at most one Prava funding charge, retries cannot duplicate either, and
no sensitive card credential is stored, logged, returned, or delivered by webhook.
