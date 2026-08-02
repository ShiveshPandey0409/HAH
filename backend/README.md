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
then supply `WEBHOOK_SECRET_ENCRYPTION_KEYS` as a JSON list of Fernet keys.
For payments, supply `PRAVA_SECRET_KEY` with the dashboard's `sk_test_*` key and
`PRAVA_PAYER_EMAIL` with the shared hackathon payer's email. Never configure the
test card number, CVV, expiry, OTP, or passkey as Render environment variables.
Production startup requires this deployment-safe encryption key. External MCP OAuth
and SMTP password-reset delivery are optional integrations: when OAuth introspection
is absent, the MCP endpoint rejects every token; when SMTP is absent,
`POST /v1/auth/forgot-password` returns `503` without issuing a reset token. If these
integrations are configured, their credentials must be complete and deployed URLs
must use HTTPS. `WEBHOOK_WORKER_ENABLED=true` runs delivery in the single web
instance without a separately billed worker.

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
- `POST /v1/claims/{claim_id}/proof-uploads`
- `POST /v1/claims/{claim_id}/submissions`
- `GET /v1/submissions/{submission_id}`
- `GET /v1/tasks/{task_id}/submissions`
- `GET /v1/submissions/{submission_id}/proofs/{proof_id}/content`
- `POST /v1/submissions/{submission_id}/verification`
- `POST /v1/tasks/{task_id}/payment-authorization`
- `POST /v1/tasks/{task_id}/payment-authorization/refresh`
- `GET /v1/tasks/{task_id}/payment-authorization`
- `GET /v1/tasks/{task_id}/payments`
- `GET /v1/submissions/{submission_id}/payment`
- `GET /v1/payments/{payment_id}`, `POST /v1/payments/{payment_id}/retry`
- `GET /v1/wallet`
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

Each bounty is a flat subtask and declares one or more proof requirements: `url`,
`screenshot`, or `image`. URL proofs are accepted as submitted HTTPS links; this
hackathon backend does not fetch or automatically validate their content.

For a screenshot or image, the authenticated claimant uploads the file first:

```bash
curl -X POST http://127.0.0.1:8000/v1/claims/CLAIM_ID/proof-uploads \
  -H "Authorization: Bearer WORKER_TOKEN" \
  -F "proof_type=screenshot" \
  -F "file=@proof.png"
```

The response's `upload_id` is then attached to the submission:

```json
{
  "proofs": [
    {"proof_type": "url", "url": "https://www.reddit.com/r/example/comments/abc"},
    {"proof_type": "screenshot", "upload_id": "UPLOAD_UUID"}
  ]
}
```

PNG, JPEG, GIF, and WebP files up to 5 MiB are supported. Images are temporarily
stored in PostgreSQL for the hackathon, so no S3 bucket or Render disk is required.
The API validates file signatures, hashes each upload, ties it to the claimant and
claim, and prevents reuse. The creator and submitting worker can read a submission
and fetch its authenticated `content_url`; only the creator can list all submissions
for a task. A `submission.created` webhook includes `submission_url`, which can be
retrieved with the creator's API session.

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
Starting or refreshing a Prava task authorization needs `payments:write`; reading a
payment or wallet needs `payments:read`.
The read-only `get_submission_proofs` tool needs `submissions:read` and returns URL
metadata plus uploaded screenshots/images as native MCP image content.
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
Destinations must resolve only to public unicast addresses. Successful and terminally
failed reward payments enqueue `payment.succeeded` and `payment.failed` respectively.

For a non-local deployment, set `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` to
JSON arrays containing the public host and browser origins. The local-only defaults
fail closed for other hosts and keep MCP DNS-rebinding protection enabled.

## Prava sandbox task funding

This integration uses Prava's current REST mandate APIs and hosted approval page; no
frontend SDK is required for the hackathon flow. The publishable `pk_test_*` key is
only needed later if the Prava iframe is embedded in a frontend. The backend needs:

```dotenv
PRAVA_PAYMENT_AUTOMATION_ENABLED=true
PRAVA_PAYMENT_WORKER_ENABLED=true
PRAVA_BASE_URL=https://sandbox.api.prava.space
PRAVA_SECRET_KEY=sk_test_replace_me
PRAVA_PAYER_USER_ID=hah-universal-hackathon-payer
PRAVA_PAYER_EMAIL=shared-payer@example.com
PRAVA_MERCHANT_NAME=Hire a Human
PRAVA_MERCHANT_URL=https://hah-api-prava.onrender.com
PRAVA_MERCHANT_COUNTRY=IN
PRAVA_SETTLEMENT_MODE=prava_sandbox
```

Keep `PRAVA_SECRET_KEY` on the server. The shared sandbox card is entered only on the
returned Prava URL. Do not store it in `.env`, Render, Swagger requests, MCP inputs,
the database, logs, tests, or screenshots committed to Git.

The API sequence for a logged-in task creator is:

```bash
# 1. Create a task, then start its task-budget authorization.
curl -X POST "$API/v1/tasks/$TASK_ID/payment-authorization" \
  -H "Authorization: Bearer $CREATOR_TOKEN"

# 2. Open approval_url from the response and approve on Prava's hosted page.
# 3. Refresh until status is active.
curl -X POST "$API/v1/tasks/$TASK_ID/payment-authorization/refresh" \
  -H "Authorization: Bearer $CREATOR_TOKEN"

# 4. After an approved proof, inspect the logical reward and worker result.
curl "$API/v1/submissions/$SUBMISSION_ID/payment" \
  -H "Authorization: Bearer $CREATOR_TOKEN"

# 5. The freelancer reads the resulting internal credit.
curl "$API/v1/wallet" -H "Authorization: Bearer $FREELANCER_TOKEN"
```

`POST .../payment-authorization` asks Prava to block the task's exact total budget. It
does not create a creator/HAH wallet and does not charge yet. Its response shows the
current task reservation, remaining budget, amounts blocked by the creator's other
tasks, and whether this task still needs approval. The first passed submission
schedules one idempotent Prava sandbox charge for that task budget. After it succeeds,
the first and all later verified rewards are append-only credits in the correct
freelancer wallet. This is intentional:
Prava recurring mandates allow one external charge per cycle, while a task may have
many bounties and slots. It also matches the hackathon model where HAH receives the
test funding and only task completers receive internal wallet balances.

`prava_sandbox` calls the real Prava sandbox session, mandate, charge, and report APIs.
It validates that Prava minted the single-use test credential, immediately discards
it, and stores only safe provider references. Prava's sandbox does not move real money,
and `/v1/wallet` explicitly returns `redeemable: false`. A production version must
send that credential through the merchant's real processor before reporting
`APPROVED`, plus add regulated custody/payout and redemption controls.

The HAH MCP server exposes `start_task_payment_authorization`,
`refresh_task_payment_authorization`, `get_payment_status`, and `get_wallet_balance`.
MCP agents use the same HAH user as browser login through the OAuth delegation mapping,
but they never receive the Prava secret or payment credentials. A human still opens
the Prava approval URL once. Later `verify_submission` can schedule the reward; the
server-side worker performs the Prava REST charge because Prava deliberately does not
expose mandate charging through its own MCP tools.

Official contract references: [create a mandate session](https://docs.prava.space/api-reference/create-session),
[mandate rules](https://docs.prava.space/concepts/mandates),
[charge a mandate](https://docs.prava.space/api-reference/mandate-charge),
[report a mandate charge](https://docs.prava.space/api-reference/mandate-report),
[sandbox behavior](https://docs.prava.space/api-reference/testing), and
[Prava MCP tools](https://docs.prava.space/mcp/tools).

The backend application lives only in `backend/app/`. Database migrations, tests,
dependency metadata, and local PostgreSQL services also live under `backend/`.
Database baseline and existing-database adoption instructions are in
[`database/README.md`](../database/README.md).
