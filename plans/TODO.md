# Backend delivery TODO

Deliver each milestone as a separate PR. A milestone is complete only when its
HTTP and MCP surfaces reuse the same service layer, migrations apply from a clean
PostgreSQL database, and the full test suite passes.

## Baseline — user creation (Plan 01)

- [x] `GET /health` and `GET /ready` are implemented.
- [x] `POST /v1/users` is implemented and the user-creation baseline is verified.
- [x] Validation, duplicate-email handling, rollback behavior, and safe responses
  are covered by tests.

## Milestone 1 — task creation and MCP `create_task` (Plans 03 and 07)

- [x] Map the baseline `tasks`, `bounties`, `api_clients`, and `mcp_requests`
  tables in SQLAlchemy; use new Alembic revisions only for post-baseline guards.
- [x] Implement one atomic task service for a draft task and all of its bounties,
  including creator capability, budget, deadline, influence, proof, and currency
  rules.
- [x] Add `POST /v1/tasks`, `POST /v1/tasks/{task_id}/open`, and
  `GET /v1/tasks/{task_id}`.
- [x] Add hashed API-key authentication, tool scopes, audit records, and
  idempotency handling for MCP requests.
- [x] Implement MCP `create_task` through the same task service with
  `creation_source = mcp`.
- [x] Test all supported platform/action combinations, transaction rollback,
  budget concurrency, task opening, MCP/HTTP parity, scope enforcement, redaction,
  and duplicate/concurrent idempotent calls.
- [x] Run migrations from an empty database, run the complete test suite, smoke
  test HTTP and MCP end to end, then open PR 1.
- [ ] Before public deployment, define manual HTTP session authentication and
  replace caller-supplied user IDs with authenticated principals. The current
  prototype intentionally authenticates MCP only.

## Milestone 2 — social enrichment, eligible feed, and claims (Plans 02 and 04)

- [ ] Map baseline `social_accounts` and `bounty_claims`; add new migrations only
  for post-baseline changes to eligibility and atomic-claim guarantees.
- [ ] Implement the public-profile URL validator and a vendor-neutral enrichment
  adapter; store no OAuth or social credentials.
- [ ] Add `PUT /v1/users/{user_id}/social-profiles/{platform}` and
  `GET /v1/users/{user_id}/social-profiles`.
- [ ] Add `GET /v1/freelancers/{freelancer_id}/bounties` and
  `POST /v1/bounties/{bounty_id}/claims`.
- [ ] Test URL normalization, provider failures, metric mapping, profile
  uniqueness, eligibility boundaries, expired/closed exclusions, and concurrent
  final-slot claims.
- [ ] Run migrations from an empty database, run the complete test suite, smoke
  test the user-to-profile-to-feed-to-claim flow, then open PR 2.

## Milestone 3 — submissions, verification, MCP, and webhooks (Plans 05, 07, 08)

- [ ] Map baseline `submissions`, `submission_proofs`, `webhook_endpoints`, and
  `webhook_deliveries`; add migrations only for required post-baseline changes.
- [ ] Implement atomic proof submission and revision handling; add
  `POST /v1/claims/{claim_id}/submissions`.
- [ ] Implement one verification service for automatic, manual, and MCP methods;
  add `POST /v1/submissions/{submission_id}/verification`.
- [ ] Implement MCP `verify_submission` through that service with scope checks,
  audit records, redaction, and idempotency.
- [ ] Add `PUT /v1/users/{creator_id}/webhook` and
  `GET /v1/users/{creator_id}/webhook`, transactional event creation, signing,
  retry/backoff, and concurrency-safe delivery.
- [ ] Emit `submission.created`, `verification.completed`, and
  `mcp_request.completed`; do not enqueue or emit payment work yet.
- [ ] Test proof requirements, revisions, claim transitions, final-result
  idempotency, HTTP/MCP parity, signatures, safe payloads, retry policy, and
  concurrent workers.
- [ ] Run migrations from an empty database, run the complete test suite, smoke
  test claim-to-submission-to-verification-to-webhook over HTTP and MCP, then open
  PR 3.

## Deferred — Prava payments (Plan 06)

- [ ] Implement only after the current three milestones are merged and the Prava
  sandbox contract is confirmed from current provider documentation.
- [ ] Keep payment authorization, payout execution, payment events, and payment
  tables out of the current delivery and PRs.
