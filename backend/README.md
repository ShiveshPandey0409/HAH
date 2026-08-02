# HAH backend

## Quick start

Prerequisites: Docker and [`uv`](https://docs.astral.sh/uv/).

```bash
cd backend
cp .env.example .env
uv sync --all-groups

docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d hire_human \
  < ../database/schema.sql
uv run alembic stamp 20260801_0001
uv run alembic upgrade head

uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The API is now available at `http://127.0.0.1:8000`.

## API documentation

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>

FastAPI serves these automatically while the API process is running.

## Optional webhook worker

Run this in a second terminal only when testing webhook delivery:

```bash
cd backend
uv run python -m app.workers.webhooks
```

`WEBHOOK_SECRET_ENCRYPTION_KEYS` must contain one or more private Fernet keys.
The first key encrypts new signing secrets and destination URLs as one versioned
credential envelope; the remaining keys support rotation.
The checked-in example key is for local development only and is rejected when
`APP_ENV=staging` or `APP_ENV=production`; unknown environment names are rejected.

## Render deployment

The repository root contains `render.yaml` for a free web service and free
PostgreSQL database in Singapore. It deploys from `shivesh`, runs the database
baseline and Alembic migrations before Uvicorn, serves `/docs`, and checks `/ready`.

```bash
render whoami -o json
render blueprints validate render.yaml
```

Connect the private `ShiveshPandey0409/HAH` repository to the Render workspace,
then supply the OAuth issuer/introspection values, SMTP/reset-link settings, and
`WEBHOOK_SECRET_ENCRYPTION_KEYS` as a JSON list of Fernet keys. Production startup
fails closed when OAuth, SMTP, the HTTPS password-reset URL, or webhook encryption
is missing. `WEBHOOK_WORKER_ENABLED=true` runs delivery in the single web instance
without a separately billed worker.

## Test

```bash
docker compose up -d postgres_test
uv run pytest
```

`GET /health` checks the API process and `GET /ready` checks PostgreSQL. Implemented
HTTP endpoints cover accounts, tasks, social profiles, the freelancer
marketplace, submissions, verification, and webhook configuration:

- `POST /v1/auth/signup`, `POST /v1/auth/login`, `GET /v1/auth/me`
- `POST /v1/auth/logout`, `POST /v1/auth/change-password`
- `POST /v1/auth/forgot-password`, `POST /v1/auth/reset-password`
- `GET /v1/tasks`, `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`, `PUT /v1/tasks/{task_id}`, `DELETE /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/open`
- `PUT /v1/users/{user_id}/social-profiles/{platform}`
- `GET /v1/users/{user_id}/social-profiles`
- `GET /v1/freelancers/{freelancer_id}/bounties`
- `POST /v1/bounties/{bounty_id}/claims`
- `POST /v1/claims/{claim_id}/submissions`
- `POST /v1/submissions/{submission_id}/verification`
- `PUT /v1/users/{creator_id}/webhook`
- `GET /v1/users/{creator_id}/webhook`

Signup, login, and password recovery are public. Every other `/v1` business route
requires the opaque bearer token returned by signup/login. Passwords use salted
scrypt hashes; only SHA-256 hashes of login/reset tokens are stored. Logout,
password change, and password reset revoke the appropriate sessions. Forgot-password
responses do not disclose whether an email exists. SMTP receives the single-use
reset URL; raw reset tokens are never logged or stored.

Task CRUD operates on the whole draft aggregate. `PUT` replaces the task and its
bounties atomically; `DELETE` removes only a draft. Opened tasks reject replacement
and deletion to preserve claims, submissions, audits, and future payment history.
Creator/freelancer/verifier IDs come from the authenticated session, not request bodies.

Social enrichment is behind a vendor-neutral adapter. Because no provider contract
or credentials are specified in this repository, the default adapter safely returns
`503` and leaves the normalized URL stored but unvalidated. Configure a provider
adapter to populate public metrics; no OAuth token or social credential is accepted
or stored.

The MCP Streamable HTTP endpoint is `/mcp`. It requires an OAuth bearer access token
on every request and publishes RFC 9728 protected-resource metadata at
`/.well-known/oauth-protected-resource/mcp`. This backend is only the resource
server: a separate OAuth/OIDC authorization server owns login, consent,
authorization code + PKCE, token issuance, refresh, and revocation.

Configure `MCP_PUBLIC_URL`, `MCP_OAUTH_ISSUER_URL`, and the three
`MCP_OAUTH_INTROSPECTION_*` values. The resource URL must be the exact public
`https://.../mcp` audience. Staging and production reject missing credentials or
non-HTTPS OAuth URLs. Development without introspection credentials remains
protected and rejects every token; it never falls back to the legacy `hah.*` API
key format.

The introspection response must follow RFC 7662 and return `active: true`, bearer
`token_type`, `sub`, `client_id`, `exp`, `iat`, and exact `aud` or `resource`, plus HAH's
required `authorization_id` extension. HAH treats these fields as mandatory even
where RFC 7662 marks them optional. `authorization_id` is an opaque, non-secret
handle for the external authorization grant, not a credential. It must be stable
across access-token refresh within one grant, globally unique and never reused for
later consent, and rotated when new consent is collected. Never put an authorization
code, access token, refresh token, ID token, session ID, or reusable secret in this
field. If the authorization server uses another stable grant identifier, its adapter
must expose it as `authorization_id`; otherwise HAH rejects the token.

`users.id` is the single account source of truth. Browser sessions point directly
to it. Before an MCP token can be used, the trusted post-consent flow maps its exact
`(issuer, subject)` to that same HAH user and approves a delegation for its OAuth
`client_id`. Email claims are never used for account linking. Every token needs
`mcp:access`; `create_task` additionally needs `tasks:create`; verification needs
`submissions:verify`, plus `submissions:approve` when the result is `passed`.
HTTP and MCP operations share the same services. MCP idempotency is isolated per
delegation, audits snapshot the actor and granted scopes, and no access token or raw
claim set is persisted.
`app.services.oauth_delegations.grant_oauth_delegation` and
`revoke_oauth_delegation` are the trusted post-consent management operations; they
are deliberately not exposed as anonymous HTTP endpoints. Provisioning must pass
the exact `authorization_id` returned by introspection. The database retains every
handle ever used by that delegation and rejects reuse, while a disabled external
identity is terminal and cannot silently reactivate its child grants.

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

Prava payment execution remains deliberately deferred.

The backend application lives only in `backend/app/`. Database migrations, tests,
dependency metadata, and local PostgreSQL services also live under `backend/`.
Database baseline and existing-database adoption instructions are in
[`database/README.md`](../database/README.md).
