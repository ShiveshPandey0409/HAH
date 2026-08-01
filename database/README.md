# Database baseline and migrations

`database/schema.sql` is the single executable DDL source for revision
`20260801_0001`. The matching Alembic revision is an adoption marker and does not
create a second copy of the schema.

## Fresh database

From `backend/`:

```bash
docker compose up -d postgres
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d hire_human \
  < ../database/schema.sql
uv run alembic stamp 20260801_0001
uv run alembic upgrade head
```

Load `schema.sql` only into an empty database. Later schema changes must be added as
new Alembic revisions instead of editing the stamped baseline.

## Adopt an existing database

For a database already created from `database/schema.sql`:

1. Back up the database.
2. Run `database/smoke_test.sql` with `ON_ERROR_STOP=1` to validate the expected
   baseline tables, constraints, functions, and triggers.
3. Confirm the database has no `alembic_version` row.
4. Run `uv run alembic stamp 20260801_0001` from `backend/`.
5. Run `uv run alembic upgrade head` to apply revisions created after the baseline.

Never run the baseline SQL and then `alembic upgrade` without stamping it first.
Never stamp a database that did not pass the baseline validation.

### Revision 0005 security adoption

Baseline webhook rows do not contain a recoverable signing secret. Revision
`20260802_0005` therefore disables those endpoints, replaces their destination with a
non-routing sentinel, and replaces legacy delivery payload/error content with a small
redacted tombstone. Reconfigure the endpoint with a new PUT after upgrading.

The same revision replaces pre-0005 MCP request/response audit bodies with bounded
structural summaries. Their idempotency keys remain reserved, but those historical
requests are intentionally non-replayable. These redactions are irreversible; take the
backup in step 1 if the historical integration data must be retained offline.

## Tests

Integration tests accept only the database name `hire_human_test`. The fixture
resets that database, loads the SQL baseline, stamps revision `20260801_0001`, and
then runs pending Alembic migrations. Any other database name is rejected before a
connection or destructive statement is attempted.
