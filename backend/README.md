# HAH backend

## Setup

```bash
cd backend
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d hire_human \
  < ../database/schema.sql
uv run alembic stamp 20260801_0001
uv run alembic upgrade head
```

## Run

Start the API and webhook worker in separate terminals:

```bash
uv run uvicorn app.main:app --reload
```

```bash
uv run python -m app.workers.webhooks
```

`WEBHOOK_SECRET_ENCRYPTION_KEYS` must contain one or more private Fernet keys.
The first key encrypts new signing secrets and destination URLs as one versioned
credential envelope; the remaining keys support rotation.
The checked-in example key is for local development only and is rejected when
`APP_ENV=staging` or `APP_ENV=production`; unknown environment names are rejected.

## Test

```bash
docker compose up -d postgres_test
uv run pytest
```

`GET /health` checks the API process and `GET /ready` checks PostgreSQL. Implemented
HTTP endpoints cover users, tasks, public social profiles, the freelancer
marketplace, submissions, verification, and webhook configuration:

- `POST /v1/users`
- `POST /v1/tasks`, `GET /v1/tasks/{task_id}`, `POST /v1/tasks/{task_id}/open`
- `PUT /v1/users/{user_id}/social-profiles/{platform}`
- `GET /v1/users/{user_id}/social-profiles`
- `GET /v1/freelancers/{freelancer_id}/bounties`
- `POST /v1/bounties/{bounty_id}/claims`
- `POST /v1/claims/{claim_id}/submissions`
- `POST /v1/submissions/{submission_id}/verification`
- `PUT /v1/users/{creator_id}/webhook`
- `GET /v1/users/{creator_id}/webhook`

Social enrichment is behind a vendor-neutral adapter. Because no provider contract
or credentials are specified in this repository, the default adapter safely returns
`503` and leaves the normalized URL stored but unvalidated. Configure a provider
adapter to populate public metrics; no OAuth token or social credential is accepted
or stored.

The MCP Streamable HTTP endpoint is `/mcp`. It requires
`Authorization: Bearer <api-key>` and exposes `create_task` with the `tasks:create`
scope and `verify_submission` with the `submissions:verify` scope. HTTP and MCP
operations share the same services; MCP calls additionally provide an idempotency
key and write a bounded, redacted audit record.
API keys are returned once by `app.services.api_clients.issue_api_client`; only the
hash is stored. Credential provisioning is intentionally an internal management
operation rather than a public signup endpoint.

Webhook PUT returns a signing secret once and rotates it on replacement; GET never
returns the secret. The database stores an encrypted destination credential and a
non-routing sentinel rather than plaintext capability paths/query strings; PUT and GET
still return the normalized destination URL. `submission.created`, `verification.completed`, and
`mcp_request.completed` are enqueued in the business transaction, signed over the
stored canonical JSON bytes, and delivered by the worker with bounded retries.
Destinations must resolve only to public unicast addresses. Payment event names can
be reserved in subscriptions, but this milestone never enqueues payment events.

For a non-local deployment, set `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` to
JSON arrays containing the public host and browser origins. The local-only defaults
fail closed for other hosts and keep MCP DNS-rebinding protection enabled.

The current HTTP prototype still uses caller-supplied user IDs. Add authenticated
HTTP principals before public deployment, as tracked in `plans/TODO.md`.

Prava payment execution remains deliberately deferred.

The backend application lives only in `backend/app/`. Database migrations, tests,
dependency metadata, and local PostgreSQL services also live under `backend/`.
Database baseline and existing-database adoption instructions are in
[`database/README.md`](../database/README.md).
