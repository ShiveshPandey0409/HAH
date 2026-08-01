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

```bash
uv run uvicorn app.main:create_app --factory --reload
```

## Test

```bash
docker compose up -d postgres_test
uv run pytest
```

`GET /health` checks the API process and `GET /ready` checks PostgreSQL. Implemented
HTTP endpoints are `POST /v1/users`, `POST /v1/tasks`, `GET /v1/tasks/{task_id}`,
and `POST /v1/tasks/{task_id}/open`.

The MCP Streamable HTTP endpoint is `/mcp`. It requires
`Authorization: Bearer <api-key>` and currently exposes `create_task` to API clients
with the `tasks:create` scope. HTTP and MCP task creation share the same service;
MCP calls additionally provide an idempotency key and write a redacted audit record.
API keys are returned once by `app.services.api_clients.issue_api_client`; only the
hash is stored. Credential provisioning is intentionally an internal management
operation rather than a public signup endpoint.

For a non-local deployment, set `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` to
JSON arrays containing the public host and browser origins. The local-only defaults
fail closed for other hosts and keep MCP DNS-rebinding protection enabled.

Prava payment behavior is not part of the current implementation milestone.

The backend application lives only in `backend/app/`. Database migrations, tests,
dependency metadata, and local PostgreSQL services also live under `backend/`.
Database baseline and existing-database adoption instructions are in
[`database/README.md`](../database/README.md).
