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

## User creation API

The implemented backend scope is user creation only. A user can be a task creator, a
freelancer, or both. Social account URLs, tasks, verification, and payments come later.

```bash
cd backend
cp .env.example .env
uv sync --all-groups
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
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

Run the PostgreSQL integration tests against the isolated test database:

```bash
docker compose up -d postgres_test
uv run pytest
```

API documentation is available at `http://localhost:8000/docs` while the server is running.

The separate backend implementation plans are indexed in [plans/README.md](plans/README.md).
