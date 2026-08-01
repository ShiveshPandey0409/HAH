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

The backend currently supports user creation plus atomic task-and-bounty creation.
Tasks can be created through HTTP or the authenticated MCP `create_task` tool; both
surfaces use the same validation and transaction logic. Social profiles, claims,
submissions, and verification are the next milestones. Prava payments remain deferred.

```bash
cd backend
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d hire_human \
  < ../database/schema.sql
uv run alembic stamp 20260801_0001
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload
```

Create a user:

```bash
curl --request POST http://localhost:8000/v1/users \
  --header 'Content-Type: application/json' \
  --data '{
    "email": "worker@example.com",
    "display_name": "Marketing Freelancer",
    "can_create_tasks": false,
    "can_work_tasks": true,
    "bio": "Reddit and LinkedIn marketing"
  }'
```

Task endpoints:

- `POST /v1/tasks` creates one draft task with all bounties atomically.
- `GET /v1/tasks/{task_id}` reads the task and current slot counts.
- `POST /v1/tasks/{task_id}/open` opens a valid draft task and its draft bounties.

The MCP Streamable HTTP endpoint is `/mcp`. It requires a bearer token issued by the
API-client management service with the `tasks:create` scope. Only a SHA-256 hash of
the high-entropy secret is stored. Every successful or failed tool execution has a
redacted, idempotent `mcp_requests` audit record.

Run the PostgreSQL integration tests against the isolated test database:

```bash
docker compose up -d postgres_test
uv run pytest
```

API documentation is available at `http://localhost:8000/docs` while the server is running.

See [database/README.md](database/README.md) before initializing or adopting a database.

The separate backend implementation plans are indexed in [plans/README.md](plans/README.md).
