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
uv run uvicorn app.main:app --reload
```

## Test

```bash
docker compose up -d postgres_test
uv run pytest
```

`GET /health` checks the API process. `GET /ready` checks PostgreSQL. The first
implemented product endpoint is `POST /v1/users`.

The backend application lives only in `backend/app/`. Database migrations, tests,
dependency metadata, and local PostgreSQL services also live under `backend/`.
Database baseline and existing-database adoption instructions are in
[`database/README.md`](../database/README.md).
