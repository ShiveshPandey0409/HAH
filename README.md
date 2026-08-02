## Entire User Journey

```text
Landing
├── Brand
│   ├── Sign up
│   ├── Create task
│   │   ├── Manually
│   │   └── Via agent using MCP
│   ├── Hire
│   └── Agent pays creator via Prava
└── Creator
    ├── Sign up
    ├── Submit public social account URL
    ├── Find task
    ├── Accept task
    ├── Finish task
    ├── Marks done
    └── Get paid
```

## Implemented backend

The backend supports users, atomic task-and-bounty creation, public social-profile
enrichment, the eligible freelancer feed and claims, proof submissions, manual/MCP
verification, and signed retryable webhooks. Tasks and verification can be performed
through authenticated MCP tools that reuse the HTTP business services. Prava payment
authorization, automatic task funding, and internal hackathon wallet credits are
implemented through the same services.

```bash
cd backend
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d hire_human \
  < ../database/schema.sql
uv run alembic stamp 20260801_0001
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Run `uv run python -m app.workers.webhooks` in a second terminal to deliver queued
events. The configured keys encrypt both webhook signing secrets and capability URLs.
Replace the development-only example before any deployment; staging and production
modes reject that public key.

Create an account and save the returned `access_token`:

```bash
curl --request POST http://localhost:8000/v1/auth/signup \
  --header 'Content-Type: application/json' \
  --data '{
    "email": "worker@example.com",
    "password": "replace-with-a-long-password",
    "display_name": "Marketing Freelancer",
    "can_create_tasks": false,
    "can_work_tasks": true,
    "bio": "Reddit and LinkedIn marketing"
  }'
```

Human account endpoints are `POST /v1/auth/signup`, `POST /v1/auth/login`,
`GET /v1/auth/me`, `POST /v1/auth/logout`, `POST /v1/auth/change-password`,
`POST /v1/auth/forgot-password`, and `POST /v1/auth/reset-password`. Except for
signup, login, and password recovery, every `/v1` operation requires
`Authorization: Bearer <access_token>`. Password-reset email uses SMTP; staging
and production fail startup when SMTP or an HTTPS reset URL is missing.

Task endpoints:

- `POST /v1/tasks` creates one draft task with all bounties atomically.
- `GET /v1/tasks` lists the logged-in creator's tasks.
- `GET /v1/tasks/{task_id}` reads an owned task and current slot counts.
- `PUT /v1/tasks/{task_id}` atomically replaces an owned draft and its bounties.
- `DELETE /v1/tasks/{task_id}` deletes an owned draft.
- `POST /v1/tasks/{task_id}/open` opens a valid draft task and its draft bounties.

Replacing or deleting an opened task is rejected so claims, submissions, audits,
and later payment records cannot be rewritten. Actor IDs are derived from the
session and are not accepted in task, claim, submission, or verification bodies.

Marketplace endpoints:

- `PUT /v1/users/{user_id}/social-profiles/{platform}` normalizes a public Reddit
  or LinkedIn profile URL and enriches it through the configured provider adapter.
- `GET /v1/users/{user_id}/social-profiles` returns the logged-in user's safe metrics.
- `GET /v1/freelancers/{freelancer_id}/bounties` returns only eligible open work.
- `POST /v1/bounties/{bounty_id}/claims` atomically reserves one remaining slot and
  stores a fixed reward/currency snapshot.

Completion endpoints:

- `POST /v1/claims/{claim_id}/submissions` records one strict proof set and advances
  the claim to submitted.
- `POST /v1/submissions/{submission_id}/verification` records a manual verification
  result through the shared state machine.
- `PUT /v1/users/{creator_id}/webhook` creates or rotates the signed webhook endpoint;
  `GET` returns configuration without its secret.

Payment endpoints:

- `POST /v1/tasks/{task_id}/payment-authorization` creates a Prava-hosted task-budget
  approval URL; `POST .../refresh` activates it after passkey approval.
- `GET /v1/tasks/{task_id}/payment-authorization` returns the safe authorization and
  funding state.
- `GET /v1/submissions/{submission_id}/payment`, `GET /v1/payments/{payment_id}`, and
  `GET /v1/tasks/{task_id}/payments` expose authenticated status.
- `POST /v1/payments/{payment_id}/retry` retries a terminal funding failure.
- `GET /v1/wallet` returns the logged-in user's non-redeemable hackathon credits.

No enrichment vendor is selected in this repository. The default adapter returns
`503` while preserving the submitted URL as unvalidated; deployments must supply a
vendor adapter. Provider-neutral fake adapters cover the complete flow in tests.

The canonical account record is `users.id`. Human sessions and MCP OAuth identities
both reference that row, so the same person owns HTTP and agent actions. They do not
share bearer tokens: `/v1` accepts only HAH login sessions, while `/mcp` accepts only
OAuth access tokens and maps the verified delegation back to the same user.

The MCP Streamable HTTP endpoint is `/mcp` and is an OAuth 2.1 protected resource.
HAH now runs the matching first-party OAuth authorization server on the same backend:
`/register`, `/authorize`, `/oauth/consent`, `/token`, and `/revoke`. The consent page
authenticates the existing HAH account, so browser and MCP actions resolve to the same
`users.id`. Authorization uses S256 PKCE, hashed one-time codes and tokens, rotating
refresh tokens, and private `/oauth/introspect` validation. API keys and HAH browser
session tokens are not accepted by `/mcp`.

HAH's RFC 7662 introspection profile additionally requires `authorization_id`: an
opaque, non-secret authorization-grant handle. The authorization server returns
the same handle for every access token minted from one authorization/refresh grant,
issues a new never-reused handle after every new consent, and supersedes the old
grant. It is never an authorization code, access token, refresh token, ID token, or
other credential. A token without this extension fails closed; immutable grant
history prevents an old consent from being revived by reusing its handle.

Every token needs `mcp:access`; `create_task` additionally needs `tasks:create`, and
`verify_submission` needs `submissions:verify`. A `passed` verification also needs
the narrower `submissions:approve` consent because it can release a task reward.
Payment authorization tools require `payments:write`; status and
wallet tools require `payments:read`. Every successful or failed tool execution has a bounded, redacted,
delegation-scoped idempotency record. Access tokens are never persisted or sent to
Prava. The worker—not the MCP client—uses the server-side Prava sandbox secret.

Webhook delivery emits `submission.created`, `verification.completed`,
`payment.succeeded`, `payment.failed`, and `mcp_request.completed`. Canonical payload
bytes are signed and delivered by a
concurrency-safe worker with DNS/SSRF checks and bounded retry/backoff. Payment
credentials never enter webhook payloads.

For the hackathon, one shared Prava sandbox payer authorizes each task's whole budget.
This is a task reservation in the backend, not a HAH/creator wallet. The API and MCP
show what the current task blocks, what other tasks still block, and whether a new
Prava approval is required. On the first passed proof, the backend performs one
idempotent sandbox funding charge for that task; verified subtask rewards are appended
only to the relevant completer's internal wallet. This avoids Prava's
one-charge-per-recurring-cycle constraint while keeping every wallet credit backed by
the task funding record. No real money moves in sandbox and wallet redemption is
intentionally not implemented. See
[`backend/README.md`](backend/README.md#prava-sandbox-task-funding) for setup and the
exact API/MCP sequence. Card number, CVV, expiry, OTP, and passkey input belong only
on Prava's hosted page and must never be added to this repository or Render.

Run the PostgreSQL integration tests against the isolated test database:

```bash
docker compose up -d postgres_test
uv run pytest
```

API documentation is available at `http://localhost:8000/docs` while the server is running.

See [database/README.md](database/README.md) before initializing or adopting a database.

The separate backend implementation plans are indexed in [plans/README.md](plans/README.md).
