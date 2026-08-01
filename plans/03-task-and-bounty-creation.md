# Plan 03: task and bounty creation

## Outcome

A creator can manually create a funded marketing task and its Reddit/LinkedIn post
or comment bounties. Each task owns the total budget and deadline. Each bounty owns
its reward, slot count, influence range, proof requirements, and optional deadline.

## API

### Create a draft task with bounties

```http
POST /v1/tasks
```

Request shape:

```json
{
  "creator_id": "uuid",
  "title": "Launch campaign",
  "description": "Campaign instructions",
  "total_budget_minor": 50000,
  "currency": "USD",
  "deadline_at": "2026-08-31T18:00:00Z",
  "bounties": [
    {
      "platform": "reddit",
      "action": "comment",
      "instructions": "Write an original relevant comment",
      "reward_minor": 1000,
      "slot_count": 10,
      "influence_metric": "karma",
      "min_influence": 500,
      "max_influence": 5000,
      "proof_requirements": ["url", "screenshot"],
      "deadline_at": "2026-08-30T18:00:00Z"
    }
  ]
}
```

The request creates the task and all bounties in one transaction. The task starts
as `draft`.

### Open a task

```http
POST /v1/tasks/{task_id}/open
```

Opening is allowed only when the task has at least one valid bounty. Payment
authorization is handled separately in Plan 06.

### Read a task

```http
GET /v1/tasks/{task_id}
```

Return the task, bounties, allocated budget, remaining budget, and claim counts.

## Database

Use `tasks` for the campaign and `bounties` for paid subtasks.

The service must rely on the existing database guarantees:

- creator has `can_create_tasks = true`;
- only Reddit/LinkedIn and post/comment combinations are accepted;
- monetary values are positive integer minor units with one task currency;
- non-cancelled bounty allocation is `reward_minor * slot_count`;
- total allocation cannot exceed `tasks.total_budget_minor`;
- bounty deadline cannot be after the task deadline;
- proof requirements contain only `url`, `screenshot`, and `image` with no duplicates;
- influence range is followers or karma, with karma limited to Reddit.

Do not create organization, proof-requirement, or pre-created slot tables.

## Service flow

1. Validate creator capability and request fields.
2. Normalize currency to its uppercase code.
3. Begin one transaction.
4. Insert the draft task with `creation_source = manual`.
5. Insert every bounty.
6. Let the database reject budget or cross-row rule violations.
7. Commit once and return the complete task.

The reusable service accepts a `creation_source` argument so Plan 07 can call the
same logic with `mcp`.

## Failure behavior

- `404`: creator does not exist;
- `409`: idempotency conflict or task cannot transition to open;
- `422`: invalid capability, budget, deadline, action, platform, influence range,
  proof requirement, or total bounty allocation;
- unexpected failures roll back the entire task and all bounties.

## Tests

- creator with one and several bounties;
- Reddit post/comment and LinkedIn post/comment;
- creator-only and dual-capability users can create;
- freelancer-only user is rejected;
- budget equality succeeds and budget overflow fails;
- concurrent bounty writes cannot exceed the task budget;
- invalid deadlines and influence ranges fail;
- invalid or duplicate proof requirements fail;
- a failed bounty rolls back the entire create request;
- a task without any bounty cannot open.

## Definition of done

Manual task creation is atomic, all four supported bounty types work, database
budget constraints hold under concurrency, and the same creation service is ready
for the MCP adapter.
