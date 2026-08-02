# Plan 02: public social profile URLs and enrichment

## Outcome

A freelancer can submit one public Reddit URL and one public LinkedIn URL. The
backend validates the URL, asks the configured enrichment provider for public
metrics, and stores the latest result for later eligibility checks.

This is not a Reddit or LinkedIn account connection. There is no social login,
OAuth, token storage, or automatic posting.

## API

### Submit or replace a profile URL

```http
PUT /v1/users/{user_id}/social-profiles/{platform}
```

The bearer session is required and `user_id` must equal the logged-in user.

Request:

```json
{
  "profile_url": "https://www.reddit.com/user/example/"
}
```

Rules:

- `platform` is only `reddit` or `linkedin`;
- the URL must use HTTPS and belong to the selected platform;
- the target user must have `can_work_tasks = true`;
- the API normalizes the URL before storing it;
- one user has at most one URL per platform;
- the request never accepts a handle, account ID, password, cookie, or token.

Return the saved URL and enrichment state. Do not return the provider's raw payload.

### Read submitted profiles

```http
GET /v1/users/{user_id}/social-profiles
```

The bearer session is required and `user_id` must equal the logged-in user.

Return platform, normalized URL, public metrics, last enrichment time, and whether
the provider validated the profile.

## Database

Use `social_accounts`:

- `user_id`, `platform`, and `profile_url` identify the profile;
- follower/following and Reddit karma fields store the latest metrics;
- `is_verified` means the enrichment provider validated that the public profile
  exists; it does not mean OAuth ownership verification;
- `enrichment_provider`, `enrichment_data`, and `enriched_at` record the latest
  provider result;
- unique `(user_id, platform)` makes the `PUT` operation an upsert;
- unique `(platform, profile_url)` prevents the same public profile being assigned
  to multiple users.

Do not add an enrichment-history table until history is an actual requirement.

## Service flow

1. Load the freelancer user.
2. Validate and normalize the public URL.
3. Upsert the pending `social_accounts` row.
4. Call a provider adapter with platform and normalized URL.
5. Validate the provider response.
6. Store the current metrics, redacted provider payload, provider name, validation
   result, and timestamp.
7. Return the safe profile response.

Keep the provider behind an interface so the API and database do not depend on one
vendor-specific response shape.

## Failure behavior

- `404`: user does not exist;
- `409`: URL already belongs to another user;
- `422`: unsupported platform, malformed URL, platform/URL mismatch, or user cannot
  work tasks;
- `502`: provider rejected the request or returned an invalid response;
- `503`: provider was unavailable. Keep the URL but do not mark it validated.

Provider retries must be bounded and must not create duplicate profile rows.

## Tests

- Reddit and LinkedIn URL normalization;
- platform/host mismatch rejection;
- non-HTTPS and non-public URL rejection;
- one profile per platform per freelancer;
- duplicate URL across users returns conflict;
- creator-only user cannot add a freelancer profile;
- successful Reddit metric mapping, including karma;
- successful LinkedIn follower/following mapping;
- failed enrichment never marks the profile validated;
- response does not expose raw provider secrets or social credentials.

## Definition of done

The two endpoints work against PostgreSQL, the provider adapter is tested with a
fake implementation, and a validated profile contains the metrics required by the
freelancer feed without storing any social-login credential.
