# Evidence policy (evidence-policy-v1)

Enforced by `app.governance.policy.evidence_failures`, inside the monitor of the compiled PreNode `verified_report_and_approval`. It runs when a recovery is proposed and again immediately before execution, using the metadata stored with the approval request (not what the caller says).

Each cited evidence item needs stored metadata: `evidence_id`, `tool`, `status`, `collected_at` (UTC, timezone-aware), `investigation_id`.

| Rule | Denial reason |
|---|---|
| Metadata exists for every cited id | `evidence_metadata_missing` |
| `tool` is a known read-only investigation or operations tool | `evidence_source_unverified` |
| `status` is `issue_detected` or `warning` (a healthy or errored result does not justify a repair) | `evidence_does_not_support_action` |
| Same investigation as the proposal | `evidence_from_other_investigation` |
| Timestamp parses and has a timezone | `evidence_timestamp_invalid` |
| Not more than 60 seconds ahead of the clock | `evidence_timestamp_in_future` |
| Not older than 60 minutes | `evidence_stale` |
| A trustworthy clock reading is available | `evidence_check_time_unavailable` |

Any failure denies; nothing is assumed valid. A denied proposal gets no approval request. Stale evidence at execution leaves the approval unspent.

Limits: `collected_at` is stamped by this code when the tool result is received and is not cryptographically signed; the stored metadata table is not tamper-sealed; investigations saved before this change lack `collected_at` and are rejected (re-run the investigation).

## Observed vs hypothesis (INV-14)
`Finding.kind` is set by code from the report list a finding sits in. A finding must cite evidence present in the report; a supported root cause must cite known evidence. Model wording cannot change a decision, and hedging quality is not verified.

## Integrity of the stored metadata (trust boundary)
- Collection time is stamped by this application when it receives a tool result. It is a locally recorded time, **not** signed and not independently attested. Anyone who can run the application can influence it; the policy limits damage (age and clock-skew limits, known tools only) but does not make the timestamp cryptographically verified.
- When a request is created, `evidence_digest` (sha256 over the sorted metadata rows) is written into the hash-chained ledger entry `approval_requested`. Before execution the digest is recomputed from the stored rows and compared. A changed, deleted or never-recorded digest denies execution with `evidence_integrity_failed`, records that outcome, and leaves the approval unspent.
- This detects edits to the stored rows after the request was made. It does not detect a forged value written before the request, and the ledger itself is tamper-evident, not immutable (someone who can rewrite the whole chain can rewrite both).
- No database migration is needed: the digest lives in the ledger and the existing `approval_evidence` table is unchanged. Requests created before this change have no recorded digest and fail closed (re-run the investigation and request again).
