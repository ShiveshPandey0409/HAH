# HAH backend

## Setup

```bash
cd backend
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
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
