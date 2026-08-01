# Hire-a-Human database design

This is a direct user-to-user model. There is no `organization`, `workspace`,
`membership`, or `organization_id` anywhere in the schema.

The schema supports exactly the requested core flow:

1. A creator creates a task manually or through MCP.
2. The task contains Reddit/LinkedIn post or comment bounties.
3. A freelancer connects Reddit and/or LinkedIn and receives enriched influence metrics.
4. The freelancer feed returns only bounties matching their verified account and influence range.
5. The freelancer claims one slot and submits URL/image/screenshot proof.
6. The submission is verified automatically, manually, or through MCP.
7. An approved submission creates one idempotent Prava payout.
8. Submission, verification, and payment results can be delivered by webhook.

## Why each table exists

| Table | Used by | Purpose | Why it is not merged further |
|---|---|---|---|
| `users` | Both | Login identity; creator/freelancer capability flags; optional bio; one Prava account reference. | Already merges roles, freelancer profile, and Prava account because each is 1:1 with a user. |
| `social_accounts` | Freelancer | Reddit/LinkedIn handle, verification, followers/following, Reddit karma, and latest enrichment payload. | One user can have two platform accounts, so these cannot be columns on `users` without duplication. |
| `tasks` | Creator | Top-level campaign, total budget, status, deadline, and manual/MCP source. | A task has many bounties; combining them would repeat campaign data. |
| `bounties` | Both | A Reddit/LinkedIn post/comment subtask, reward, slot count, influencer range, instructions, and proof rules. | It is the required task-to-subtask relationship. Proof rules are merged here as a small JSON array. |
| `bounty_claims` | Freelancer | Reserves one bounty slot and tracks it from claimed to submitted, approved, and paid. | A bounty can be claimed by many freelancers. Pre-created slot and assignment tables were merged into this one row. |
| `submissions` | Freelancer / verifier | Supports resubmission revisions and stores the current verification result/checks. | A claim exists before submission and may receive more than one revision. A separate verification table was merged here. |
| `submission_proofs` | Freelancer | Stores each submitted URL, screenshot, or image reference. | A submission can contain several proofs, so fixed columns or one JSON blob would weaken file validation and indexing. |
| `payment_authorizations` | Creator / Prava | Records a task-specific Prava automatic-payment approval and its caps/usage. | A user's Prava account is in `users`, but each task can have a different payment limit and validity window. |
| `payments` | Both / Prava | One logical, idempotent payout from creator to freelancer for one approved claim. | Kept separate from claims because payment has provider and retry state; the unique claim/submission keys prevent double pay. |
| `payment_attempts` | Prava worker | Records each provider attempt and response for safe retries and debugging. | One payment may need several attempts. Raw card number, CVV, or expiry must never be stored. |
| `api_clients` | Creator / MCP | Stores hashed MCP/API credentials and scopes. | One creator may rotate or operate multiple clients; request logs must not contain reusable secrets. |
| `mcp_requests` | MCP | Idempotency and audit record for task creation or verification calls. | One client makes many calls; merging would overwrite call history. |
| `webhook_endpoints` | Creator | Reusable destination, signing-secret hash, and subscriptions. | Configuration changes independently of deliveries. |
| `webhook_deliveries` | Agent/backend | One retryable event delivery with payload, attempts, and result. | Many events go to one endpoint. Event and delivery were merged because the current scope does not need fan-out to many endpoints. |

## Deliberate merges

The earlier design was over-normalized. These tables are intentionally **not** present:

- `organizations`, `organization_members`: not required; ownership points directly to `users`.
- `user_roles`, `freelancer_profiles`, `prava_accounts`: merged into `users` because they are 1:1 today.
- `social_enrichment_runs`: current metrics/provider payload merged into `social_accounts`; add history only when the product needs metric trends or provider debugging.
- `proof_requirements`: merged into `bounties.proof_requirements` as a constrained JSON array.
- `bounty_slots`: merged into `bounty_claims`; only claimed slots require rows.
- `verifications`: merged into `submissions`; the current requirement needs one final verification result per revision.
- `webhook_events`: merged into `webhook_deliveries`; split it later only if one event must fan out to several endpoints.

## Important database guarantees

- A user's account can be creator, freelancer, or both.
- Only `reddit` and `linkedin` platforms and `post` and `comment` actions are accepted.
- Bounty allocation cannot exceed the task's total budget, even during concurrent writes.
- `get_eligible_bounties(user_id)` implements the freelancer feed using verified social metrics.
- `claim_bounty(...)` atomically checks platform, influencer range, deadline, and remaining capacity.
- A successful verification moves the claim to `approved`.
- A payout must match the exact task, bounty reward, approved submission, creator, freelancer, and currency.
- Each claim can be paid only once; `idempotency_key` protects provider retries.
- Prava authorization caps and usage are enforced in the database.
- Payment success moves the freelancer claim to `paid` automatically.
- Raw card number, CVV, or expiry is never stored; only Prava references and redacted provider data are permitted.

## Main query surfaces

```sql
-- Freelancer feed
SELECT * FROM get_eligible_bounties(:freelancer_user_id);

-- Atomic freelancer claim
SELECT * FROM claim_bounty(
  :bounty_id,
  :freelancer_user_id,
  :verified_social_account_id
);
```

The executable PostgreSQL definition is in `database/schema.sql`, and the
transactional end-to-end check is in `database/smoke_test.sql`.
