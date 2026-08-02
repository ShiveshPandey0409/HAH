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
execution remains deferred.

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

No enrichment vendor is selected in this repository. The default adapter returns
`503` while preserving the submitted URL as unvalidated; deployments must supply a
vendor adapter. Provider-neutral fake adapters cover the complete flow in tests.

The canonical account record is `users.id`. Human sessions and MCP OAuth identities
both reference that row, so the same person owns HTTP and agent actions. They do not
share bearer tokens: `/v1` accepts only HAH login sessions, while `/mcp` accepts only
OAuth access tokens and maps the verified delegation back to the same user.

The MCP Streamable HTTP endpoint is `/mcp` and is an OAuth 2.1 protected resource.
An external OAuth/OIDC authorization server owns user login, consent, authorization
code + PKCE, and token issuance. HAH validates each bearer access token by
introspection and maps its exact issuer, subject, and agent `client_id` to a locally
approved user delegation. API keys are not accepted by `/mcp`.

HAH's RFC 7662 introspection profile additionally requires `authorization_id`: an
opaque, non-secret authorization-grant handle. The authorization server must return
the same handle for every access token minted from one authorization/refresh grant,
must issue a new never-reused handle after every new consent, and must revoke the old
grant. The same value is recorded by the trusted local post-consent provisioning
flow. It must never be an authorization code, access token, refresh token, ID token,
or other credential. A token without this extension fails closed; immutable grant
history prevents an old consent from being revived by reusing its handle.

Every token needs `mcp:access`; `create_task` additionally needs `tasks:create`, and
`verify_submission` needs `submissions:verify`. A `passed` verification also needs
the narrower `submissions:approve` consent because it can become money-moving in a
later milestone. Every successful or failed tool execution has a bounded, redacted,
delegation-scoped idempotency record. Access tokens are never persisted or sent to
Prava. Payment tools and Prava execution remain disabled.

Webhook delivery currently emits `submission.created`, `verification.completed`,
and `mcp_request.completed`. Canonical payload bytes are signed and delivered by a
concurrency-safe worker with DNS/SSRF checks and bounded retry/backoff. Payment
events and all Prava execution remain out of scope.

Run the PostgreSQL integration tests against the isolated test database:

```bash
docker compose up -d postgres_test
uv run pytest
```

API documentation is available at `http://localhost:8000/docs` while the server is running.

## Frontend

The React frontend covers creator and human signup, task creation and publishing,
social profiles, eligible work, claims, proof submission, manual verification,
webhooks, and account settings. Cloudflare Kumo provides the core components.

```bash
cd frontend
cp .env.example .env
bun install
bun run dev
```

`bun run dev` starts FastAPI on `8000` and Vite on `5173`; Vite proxies `/v1`
to the API. Set `VITE_API_BASE_URL` for a separately deployed API and
`VITE_MCP_URL` for the MCP configuration copied from the landing page.

See [database/README.md](database/README.md) before initializing or adopting a database.

The separate backend implementation plans are indexed in [plans/README.md](plans/README.md).
