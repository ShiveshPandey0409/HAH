# Hire a Human: use cases

## Product purpose

Hire a Human lets a brand or task creator pay real people to complete marketing
work from their own Reddit or LinkedIn accounts. Tasks can be created manually or
by an agent through MCP. Approved work is paid through Prava.

## Current scope

Supported platforms:

- Reddit
- LinkedIn

Supported work types:

- Create a post
- Add a comment

This produces four possible bounty types: Reddit post, Reddit comment, LinkedIn
post, and LinkedIn comment.

## Actors

### Task creator

The person or brand funding the marketing work. A creator can:

- create a task manually or through an MCP-enabled agent;
- set the task's total budget and deadline;
- break the task into one or more bounties;
- set the reward and available freelancer slots for each bounty;
- restrict a bounty using a follower or Reddit karma range;
- require URL, screenshot, and/or image proof;
- review or automatically verify submitted work;
- request a task-bound Prava budget reservation for the shared hackathon payer;
- see the amount blocked for this task and the amount still blocked by other tasks;
- receive verification and payment results through a webhook.

### Freelancer

The human completing the work. A freelancer can:

- submit a public Reddit account URL, a public LinkedIn account URL, or both;
- have followers, following, and Reddit karma retrieved for eligibility checks;
- see only bounties matching a validated public profile and influencer range;
- claim an available bounty slot;
- complete the post or comment personally;
- submit the requested proof;
- receive the fixed bounty reward after approval.

### Agent

An external agent can use MCP credentials to:

- create the same task and bounty setup available through the manual dashboard;
- request or record verification;
- use an idempotency key so a retried request does not create duplicate work;
- receive the result through the configured webhook.

### Platform backend

The platform enforces eligibility, claim capacity, proof requirements, verification,
payment authorization, idempotency, and webhook retry state.

### Prava

Prava provides the task-budget authorization and transaction reference used to fund
HAH. Verified freelancers receive internal hackathon wallet credits rather than a
direct Prava payout. The platform stores safe provider references and redacted
responses only. It does not store card number, CVV, expiry, OTP, or passkeys.
The shared payer and task creator do not receive a HAH wallet: their view is the
task's approved/blocked, used, and remaining budget.

## End-to-end use case

1. A creator signs up; the hackathon operator supplies one shared Prava sandbox payer.
2. The creator creates a task with a total budget, currency, description, and deadline.
3. The creator adds one or more bounties.
4. Each bounty defines its platform, post/comment action, instructions, reward,
   number of slots, influencer range, proof requirements, and optional deadline.
5. The platform ensures the total value of all non-cancelled bounty slots does not
   exceed the task budget.
6. The payer approves that exact task budget on Prava's hosted page. A new task gets
   its own approval; existing task reservations are shown but are not silently reused.
7. A freelancer signs up and submits a public Reddit and/or LinkedIn account URL.
8. The platform validates and normalizes the URL.
9. The enrichment provider validates the public profile and returns its metrics.
10. The freelancer feed shows only matching open tasks with remaining slots.
11. The freelancer claims one slot. The database checks the platform, validated
   account, influencer range, deadline, and remaining capacity atomically.
12. The freelancer completes the work and submits the required URLs or files.
13. The submission is verified automatically, manually, or through MCP.
14. Verification cannot pass until every required proof type is present.
15. A passed submission becomes approved for payment.
16. If necessary, the platform makes one idempotent Prava sandbox charge to fund the
    HAH task budget; a task is externally funded at most once.
17. The platform appends one internal wallet credit for the exact bounty reward.
18. Funding and reward retries cannot create a second task charge or wallet credit.
19. A successful credit marks the freelancer's claim as paid and updates the
    creator's allocated task authorization usage.
20. Verification and payment results are delivered to the creator webhook.

## Eligibility rules

A freelancer can see and claim a bounty only when:

- the task and bounty are open;
- neither applicable deadline has passed;
- the freelancer has a provider-validated public profile on the required platform;
- the selected account falls within the configured follower or karma range;
- a bounty slot remains available;
- the freelancer has not already claimed that bounty.

Following count is stored as an enrichment signal but is not currently a bounty
filter. LinkedIn uses follower ranges. Reddit can use follower or karma ranges.

The freelancer is asked only for `platform` and the public account URL. The platform
does not separately ask for a username, handle, account ID, password, or social token.

## Proof and verification

A creator can require one or more of:

- `url`: link to the Reddit or LinkedIn post/comment;
- `screenshot`: screenshot showing the completed work;
- `image`: another supporting image.

A claim can have submission revisions when changes are requested. Each revision
keeps its own proof and verification result.

## Payment rules

- A task creator authorizes Prava for the task's total budget; the task's maximum
  bounty reward remains the local per-reward limit.
- The authorization must match the task creator and currency.
- The authorization cannot exceed the task budget.
- Only a passed submission with an approved claim can be paid.
- The payment amount must equal the bounty reward.
- The payer must be the task creator and the payee must be the assigned freelancer.
- A claim and submission can each have only one logical wallet reward.
- One task has at most one provider funding transaction; later rewards allocate from
  the funded HAH task budget.
- Provider retries are recorded separately from the logical reward.
- Hackathon wallet credits are non-redeemable; real custody, withdrawals, and creator
  payouts are deliberately outside this milestone.

## Manual, MCP, and webhook parity

Manual and MCP task creation use the same task and bounty records and therefore
receive the same validation. MCP requests and payment calls use idempotency keys.
Webhook deliveries are retryable and can report submission, verification, and
payment outcomes to the creator or their agent.

## Not in the current scope

- Organizations, workspaces, or memberships
- User trust scores or general user-status fields
- Social platforms other than Reddit and LinkedIn
- Actions other than posting and commenting
- Automatically posting from the freelancer's social account
- Reddit or LinkedIn OAuth, login, account connection, or posting permission
- Storing Reddit or LinkedIn access tokens, refresh tokens, passwords, or cookies
- Storing raw payment-card credentials
- Filtering bounties by criteria other than the configured influencer range

## Acceptance criteria

The current database foundation is complete when it can demonstrate this sequence
in one test: creator and freelancer creation, social enrichment, eligible-bounty
discovery, atomic claim, required proof, successful verification, authorized Prava
payout, paid claim, MCP request record, and webhook delivery record.

The executable check for this sequence is `database/smoke_test.sql`.

The implementation order and separate setup plans are indexed in
[`plans/README.md`](../plans/README.md).
