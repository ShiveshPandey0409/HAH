# Plan 07: MCP agent operations

## Outcome

An authorized external agent can create the same task/bounty setup as the manual
API and can record a verification result. MCP does not have separate business rules;
it calls the same application services as the HTTP routes.

## MCP tools

### `create_task`

Input matches the validated task-and-bounty command from Plan 03 plus an
idempotency key. The service sets `creation_source = mcp` and records the agent's
API client ID.

### `verify_submission`

Input contains submission ID, result, checks, optional failure reason, and an
idempotency key. It calls the verification service from Plan 05 with method `mcp`.

Do not add tools for Reddit/LinkedIn posting or social-account login.

## Credentials and database

Use `api_clients` and `mcp_requests`:

- API keys are generated once and only a secure hash is stored;
- clients belong directly to a creator user;
- scopes are limited to the implemented tools;
- each request stores tool name, idempotency key, status, redacted input/output,
  related task/submission IDs, and timestamps;
- unique `(api_client_id, idempotency_key)` prevents duplicate execution.

Never store reusable credentials in `request_data` or `response_data`.

## Execution flow

1. Authenticate the API client from its presented key.
2. Check the tool-specific scope.
3. Insert the `mcp_requests` row with `started` status and idempotency key.
4. If that key already succeeded, return the stored safe response.
5. Call the shared task or verification service.
6. Store the related resource ID, final status, and redacted response.
7. Emit the result for Plan 08.

If execution failed before a business transaction committed, record the error and
allow a safe retry. If it committed but the response was lost, the idempotency row
must return the original result.

## Error behavior

- invalid key: unauthenticated MCP error;
- missing scope: authorization error;
- validation problem: structured field errors matching the HTTP API;
- idempotency key reused with different input: conflict;
- downstream failure: stable error with request ID, never a secret or raw provider
  response.

## Tests

- API key hashing and authentication;
- scope enforcement for both tools;
- manual and MCP task creation produce equivalent database rows;
- manual and MCP verification use identical proof/state rules;
- repeated identical request returns the original result;
- same key with different input is rejected;
- concurrent duplicate calls create one task or one verification result;
- request records and logs redact credentials and provider secrets;
- successful and failed operations emit webhook events.

## Definition of done

Both MCP tools work end to end, retrying a call cannot duplicate a task or
verification, and every MCP result is auditable and safe to deliver through the
configured webhook.
