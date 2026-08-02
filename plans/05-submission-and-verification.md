# Plan 05: proof submission and verification

## Outcome

A freelancer can submit completed work with every proof required by the bounty.
The work can be verified automatically, manually, or through MCP. Only a complete,
passed submission becomes approved for payment.

## API

### Submit completed work

```http
POST /v1/claims/{claim_id}/submissions
```

The bearer session supplies the freelancer identity.

Request:

```json
{
  "proofs": [
    {"proof_type": "url", "url": "https://www.reddit.com/..."},
    {"proof_type": "screenshot", "storage_key": "proofs/uuid.png"}
  ]
}
```

The API creates one `submissions` revision and all `submission_proofs` in one
transaction. Files are uploaded to object storage through a separate signed-upload
flow; the database stores only the storage key and metadata.

### Verify a submission

```http
POST /v1/submissions/{submission_id}/verification
```

The bearer session supplies the manual verifier identity and must belong to the
task creator.

Request:

```json
{
  "result": "passed",
  "checks": {},
  "failure_reason": null
}
```

The manual endpoint and MCP tool use the same verification service. Automatic
verification also writes through that service.

## Database

Use `submissions` for each revision and `submission_proofs` for individual proof
items.

Rules:

- claim must belong to the submitting freelancer and be in a submittable state;
- revision numbers increase per claim;
- proof type is only `url`, `screenshot`, or `image`;
- URL proof requires an HTTPS URL;
- screenshot/image proof requires a storage key;
- verification cannot pass unless every bounty-required proof type is present;
- passed verification moves the claim to `approved`;
- failed verification moves it to `rejected`;
- review-required verification moves it to `reviewing`.

## Service flow

1. Load the claim and its bounty proof requirements.
2. Validate the proof array and file metadata.
3. Insert the next submission revision and proofs atomically.
4. Mark the claim submitted.
5. Route the submission to the selected verification method.
6. Persist exactly one final result for that revision.
7. Apply the claim-state transition in the same transaction.
8. Emit a verification event for Plan 08.
9. If passed, enqueue payment creation from Plan 06.

## Failure behavior

- `404`: claim or submission does not exist;
- `409`: claim state no longer allows submission or verification is already final;
- `422`: missing/duplicate proof type, invalid URL, missing file reference, invalid
  verification result, or incomplete proof set.

The payment step must never run for failed, pending, or review-required results.

## Tests

- each supported proof type;
- several required proofs in one submission;
- missing required proof prevents a passed result;
- invalid URL and missing object key fail;
- only the assigned freelancer can submit;
- revision number increments after requested changes;
- automatic/manual/MCP verification share state transitions;
- repeated final verification is idempotent;
- passed result approves the claim and emits one payment request;
- failed result never creates a payment.

## Definition of done

Every proof requirement is enforced, each revision is auditable, verification state
transitions are consistent across all three methods, and exactly one downstream
payment request is emitted for an approved claim.
