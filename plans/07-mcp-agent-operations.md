# Plan 07: MCP agent operations

## Outcome

An authorized external agent can create the same task/bounty setup as the manual
API and can record a verification result. MCP does not have separate business rules;
it calls the same application services as the HTTP routes.

## MCP tools

### `create_task`

Input matches the validated task-and-bounty command from Plan 03 plus an
idempotency key. The service sets `creation_source = mcp` and records the agent's
OAuth delegation and user actor.

### `verify_submission`

Input contains submission ID, result, checks, optional failure reason, and an
idempotency key. It calls the verification service from Plan 05 with method `mcp`.

Do not add tools for Reddit/LinkedIn posting or social-account login.

## OAuth delegation and database

Use an external OAuth/OIDC authorization server and make HAH an OAuth 2.1 resource
server. The external server owns login, consent, authorization code + PKCE, tokens,
refresh, and revocation. HAH publishes protected-resource metadata, validates the
exact resource audience through authenticated token introspection, and accepts no
API-key fallback on `/mcp`.

The same person who signs into the HTTP API uses the same canonical `users.id` for
MCP. During external OAuth consent, the trusted callback binds the verified
`(issuer, subject)` and agent `client_id` delegation to that logged-in user. Browser
session tokens are never accepted by `/mcp`, and MCP tokens are never accepted by
the `/v1` session dependency; the shared user ID and service layer provide the
single source of truth.

Use `oauth_identities`, `oauth_delegations`, `oauth_authorization_grants`, and
`mcp_requests`:

- an exact `(issuer, subject)` identity belongs to one local user; email claims are
  never an account-linking key;
- each external agent `client_id` has a separate, revocable local delegation and
  approved scope set;
- token scopes are intersected with the locally approved scopes;
- RFC 7662 introspection must return a required `authorization_id` extension: a
  non-secret opaque grant handle that is stable across refresh, rotates for every
  new consent, and is never reused; the trusted provisioning flow records the same
  value and immutable history prevents A-to-B-to-A grant revival;
- disabling an external identity is terminal so old child delegations cannot be
  revived by toggling the identity back to active;
- each request stores tool name, idempotency key, status, redacted input/output,
  actor user, delegation, scope/consent snapshot, related resource IDs, and
  timestamps;
- unique `(oauth_delegation_id, idempotency_key)` prevents duplicate execution
  across access-token refreshes while isolating agents.

Legacy API-client rows remain only for historical audit compatibility. Never store
an access token, refresh token, authorization code, ID token, raw claim set, or
reusable provider credential.

## Execution flow

1. Introspect and validate the bearer access token, exact issuer, subject, client,
   expiry, MCP audience, and non-secret authorization-grant handle.
2. Resolve the active identity/delegation and check locally approved tool scopes.
3. Insert the `mcp_requests` row with `started` status and idempotency key.
4. If that key already succeeded, return the stored safe response.
5. Call the shared task or verification service.
6. Store the related resource ID, final status, and redacted response.
7. Emit the result for Plan 08.

If execution failed before a business transaction committed, record the error and
allow a safe retry. If it committed but the response was lost, the idempotency row
must return the original result.

## Error behavior

- missing, invalid, expired, revoked, or wrong-audience token: unauthenticated MCP
  error with protected-resource discovery;
- missing scope: authorization error;
- validation problem: structured field errors matching the HTTP API;
- idempotency key reused with different input: conflict;
- downstream failure: stable error with request ID, never a secret or raw provider
  response.

## Tests

- OAuth discovery, bearer authentication, exact audience/issuer/subject/client
  validation, and fail-closed introspection;
- exact identity mapping, local revocation, and email-claim rejection;
- grant-handle rotation, historical handle reuse rejection, and terminal identity
  disablement;
- scope enforcement for both tools;
- successful verification additionally requires `submissions:approve`;
- manual and MCP task creation produce equivalent database rows;
- manual and MCP verification use identical proof/state rules;
- repeated identical request returns the original result;
- same key with different input is rejected;
- concurrent duplicate calls create one task or one verification result;
- request records and logs redact credentials and provider secrets;
- token refresh preserves idempotency and different delegations remain isolated;
- successful and failed operations emit webhook events.

## Definition of done

Both MCP tools work end to end, retrying a call cannot duplicate a task or
verification, and every MCP result is auditable and safe to deliver through the
configured webhook.
