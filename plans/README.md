# Hire a Human implementation plans

This folder splits the approved use case into independent implementation plans.
Each plan owns one backend capability, its API, database work, tests, and definition
of done.

## Product boundaries used by every plan

- Direct users only; no organizations, workspaces, or memberships.
- A user can create tasks, work as a freelancer, or do both.
- Reddit and LinkedIn are the only platforms.
- Post and comment are the only actions.
- Freelancers submit public account URLs only. The platform does not ask for a
  username, password, OAuth login, access token, or posting permission.
- Eligibility can use only the configured follower or Reddit karma range.
- The user profile has no trust score or general status field.
- Raw payment-card data is never accepted or stored by this backend.
- `users.id` is the canonical identity for both human HTTP sessions and MCP OAuth
  delegations. Email claims never auto-link an OAuth identity.
- Signup/login/password recovery are public; every business `/v1` route requires a
  revocable HAH session. MCP accepts only scoped OAuth access tokens.

## Plans and dependency order

| Order | Plan | Depends on |
|---|---|---|
| 1 | [User creation](01-user-creation.md) | PostgreSQL/FastAPI foundation |
| 2 | [Public social profile URLs and enrichment](02-social-profile-enrichment.md) | User creation |
| 3 | [Task and bounty creation](03-task-and-bounty-creation.md) | User creation |
| 4 | [Freelancer feed and claims](04-freelancer-feed-and-claims.md) | Plans 02 and 03 |
| 5 | [Proof submission and verification](05-submission-and-verification.md) | Plan 04 |
| 6 | [Prava authorization and payouts](06-prava-payments.md) | Plans 03 and 05 |
| 7 | [MCP agent operations](07-mcp-agent-operations.md) | Reuses plans 03 and 05 |
| 8 | [Webhook delivery](08-webhook-delivery.md) | Event producers from plans 05, 06, and 07 |

## Parallel delivery

After Plan 01 is stable, Plans 02, 03, the Prava contract adapter from Plan 06,
and webhook configuration from Plan 08 can be developed in parallel. Plan 04 joins
Plans 02 and 03. Plan 05 follows claims. Payment execution and event delivery are
connected only after verification is reliable.

All manual HTTP endpoints and MCP tools must call the same service layer. They must
not implement separate validation or payment logic.
