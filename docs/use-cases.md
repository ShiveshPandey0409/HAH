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
- authorize automatic Prava payouts;
- receive verification and payment results through a webhook.

### Freelancer

The human completing the work. A freelancer can:

- connect a Reddit account, a LinkedIn account, or both;
- provide the account handle used for enrichment;
- have followers, following, and Reddit karma retrieved for eligibility checks;
- see only bounties matching a verified social account and influencer range;
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

Prava provides the payment authorization and transaction references used for
automatic creator-to-freelancer payouts. The platform stores provider references
and redacted responses only. It does not store card number, CVV, or expiry.

## End-to-end use case

1. A creator signs up and connects a Prava account.
2. The creator creates a task with a total budget, currency, description, and deadline.
3. The creator adds one or more bounties.
4. Each bounty defines its platform, post/comment action, instructions, reward,
   number of slots, influencer range, proof requirements, and optional deadline.
5. The platform ensures the total value of all non-cancelled bounty slots does not
   exceed the task budget.
6. A freelancer signs up and connects Reddit and/or LinkedIn.
7. Social-account metrics are enriched and the account is verified.
8. The freelancer feed shows only matching open tasks with remaining slots.
9. The freelancer claims one slot. The database checks the platform, verified
   account, influencer range, deadline, and remaining capacity atomically.
10. The freelancer completes the work and submits the required URLs or files.
11. The submission is verified automatically, manually, or through MCP.
12. Verification cannot pass until every required proof type is present.
13. A passed submission becomes approved for payment.
14. The platform creates one idempotent Prava payout for the exact bounty reward.
15. Prava attempts can be retried without creating a second logical payout.
16. A successful payout marks the freelancer's claim as paid and updates the
    creator's task authorization usage.
17. Verification and payment results are delivered to the creator webhook.

## Eligibility rules

A freelancer can see and claim a bounty only when:

- the task and bounty are open;
- neither applicable deadline has passed;
- the freelancer has a verified account on the required platform;
- the selected account falls within the configured follower or karma range;
- a bounty slot remains available;
- the freelancer has not already claimed that bounty.

Following count is stored as an enrichment signal but is not currently a bounty
filter. LinkedIn uses follower ranges. Reddit can use follower or karma ranges.

## Proof and verification

A creator can require one or more of:

- `url`: link to the Reddit or LinkedIn post/comment;
- `screenshot`: screenshot showing the completed work;
- `image`: another supporting image.

A claim can have submission revisions when changes are requested. Each revision
keeps its own proof and verification result.

## Payment rules

- A task creator authorizes Prava with per-payment and total limits.
- The authorization must match the task creator and currency.
- The authorization cannot exceed the task budget.
- Only a passed submission with an approved claim can be paid.
- The payment amount must equal the bounty reward.
- The payer must be the task creator and the payee must be the assigned freelancer.
- A claim and submission can each have only one logical payout.
- Provider retries are recorded separately from the logical payment.

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
- Storing raw payment-card credentials
- Filtering bounties by criteria other than the configured influencer range

## Acceptance criteria

The current database foundation is complete when it can demonstrate this sequence
in one test: creator and freelancer creation, social enrichment, eligible-bounty
discovery, atomic claim, required proof, successful verification, authorized Prava
payout, paid claim, MCP request record, and webhook delivery record.

The executable check for this sequence is `database/smoke_test.sql`.
