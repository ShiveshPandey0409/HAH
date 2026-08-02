# Plan 04: freelancer feed and claims

## Outcome

A freelancer sees only open bounties that match a provider-validated public profile
and the configured influence range. The freelancer can claim one available slot
without oversubscribing the bounty during concurrent requests.

## API

### Eligible feed

```http
GET /v1/freelancers/{freelancer_id}/bounties
```

Return only currently claimable bounties. The response includes task/bounty
instructions, platform, action, reward, currency, effective deadline, required
proof types, and remaining slots. It does not expose other freelancers or raw
enrichment payloads.

### Claim a bounty

```http
POST /v1/bounties/{bounty_id}/claims
```

Request:

```json
{
  "freelancer_id": "uuid",
  "social_account_id": "uuid"
}
```

Return `201 Created` with the claim and its fixed reward snapshot.

## Database

Use:

- `get_eligible_bounties(freelancer_id)` for the feed;
- `claim_bounty(bounty_id, freelancer_id, social_account_id)` for the atomic claim;
- `bounty_claims` for one reservation and its lifecycle.

The claim transaction verifies:

- user has `can_work_tasks = true`;
- profile belongs to that user and matches the bounty platform;
- provider validated the public profile;
- current follower/karma metric is inside the configured range;
- task and bounty are open and deadlines have not passed;
- a slot remains;
- freelancer has not already claimed that bounty.

Do not perform a separate read-then-insert capacity check in application code; the
database function owns the concurrency-sensitive decision.

## Service flow

1. Feed service calls the database function with the freelancer ID.
2. Claim service calls the atomic claim function inside one transaction.
3. Translate known database rule failures to stable API errors.
4. Commit once and return the created claim.

## Failure behavior

- `404`: freelancer, bounty, or submitted profile does not exist;
- `409`: already claimed, bounty full, closed, or deadline passed;
- `422`: wrong platform, unvalidated profile, or influence range mismatch.

A rejected claim must not consume a slot.

Claim TTL expiration is lazy: once `claim_expires_at` passes, the reservation releases
its capacity without rewriting the row's status. The lifetime one-claim-per-freelancer
rule still applies, so that freelancer cannot reclaim the same bounty after timeout.

## Tests

- feed contains matching Reddit and LinkedIn bounties only;
- follower and karma range boundaries are inclusive;
- following count is not used as a filter;
- unvalidated and wrong-platform profiles are excluded;
- expired/closed tasks and bounties are excluded;
- existing freelancer claim is excluded;
- exact final slot can be claimed once under concurrent requests;
- one freelancer cannot claim the same bounty twice;
- a user can claim different bounties with the appropriate profiles.

## Definition of done

The eligible feed and claim endpoint return the same decision for the same database
state, and concurrency tests prove that no bounty can exceed its slot count.
