# Governance and safety

This page describes what the code does today. The detailed catalogue (every PreNode, invariant, test and denial reason) is
[governance-catalogue.md](governance-catalogue.md); the requirement gaps are in
[governance-audit-and-gap-analysis.md](governance-audit-and-gap-analysis.md). Nothing here describes planned behaviour as
implemented.

## Compiled VSL gates vs ordinary application controls

Only three constructs are compiled VSL gates (`app/governance/policy.py`, compiled in `gates.py` through `vsl_langgraph`):

| Construct | Name | What it enforces |
|---|---|---|
| Invariant | `action_within_allowlist` | The action type is in the allow-list |
| Invariant | `no_execution_while_suspended` | No action runs while automation is suspended (suspension is read from the ledger, not from the caller) |
| PreNode | `verified_report_and_approval` | Report status `issue_identified`; supported root cause citing known evidence; evidence policy; an approval context valid for exactly this action. The monitor is Python (`failed_preconditions`) encoded as binary PASS/FAIL gamma, not a calibrated confidence |

Everything else is **ordinary Python**, labelled `code` in the catalogue and not a compiled VSL gate: the target guard,
idempotency claims, lifecycle state, verdict derivation, expiry, evidence digest, and the dashboard views. A test asserts that
exactly three gates are compiled and that every catalogue `vsl_gate` entry names an `app.governance.*` enforcement point. No new
compiled gate was added, and none is justified by a current architectural requirement.

## Separation of duties

| Step | Who / what |
|---|---|
| Investigation | Agent with read-only MCP tools (`app/agent/`) |
| Recommendation | Proposal from the verified report (`proposal_from_report`, `app/services/recoveries.py`); model confidence is never an input |
| Authorisation | A person with the per-action permission, or (opt-in, default off) `policy:risk-policy-v1` for one bounded low-risk retry |
| Execution | `RecoveryRunner` only (`app/recovery/runner.py`) |
| Recording | `GovernanceRecorder` to the VerbaLedger JSONL ledger |

Approval is **not** execution. A proposed, unauthorised, unattempted or merely approved action is never displayed as executed.

## Recovery pipeline (order enforced by `RecoveryRunner`)

attempt record, then suspension and ledger check, then precondition (target allow-list, `_test` rule, `DB_NAME()`), then
idempotency claim (`claim_execution`; ledger `idempotency_claim_acquired`), then single-use `consume`, then execute, then
independent verification, then result record. Only two actions exist: `rerun_pipeline` (medium risk) and
`remove_injected_condition` (high risk). Any gate error, unreadable or non-verifying ledger, unknown risk or target mismatch
stops execution and leaves the approval unspent (fail closed). A failed or unknown outcome yields an escalation and a compensation
**plan** that is only proposed (`executable=False`); nothing rolls back.

## Approval lifecycle and expiry

An approval is bound to the exact action, parameters, target and evidence digest, HMAC-sealed, single-use (atomic consumption)
and expires (default 30 minutes, maximum 24 hours). Semantics, implemented once in `app/recovery/expiry.py` and used by both the
server and the presentation:

| Case | Result |
|---|---|
| Stored status `pending` and `expires_at > now` | unexpired, actionable |
| Stored status `pending` and `expires_at <= now` | **expired** (the boundary instant counts as expired) |
| `expires_at` ending `Z` or naive | read as UTC |
| `expires_at` missing or unparseable | not reinterpreted: not shown as expired, keeps its stored status (the presentation fix does not change server-side handling) |
| Stored status other than `pending` | not re-derived (an approved or decided request is not shown as expired) |

The stored status is not rewritten by reading. It flips to `expired` lazily only when someone attempts a decision, and the server
decision path (`ApprovalService._decide`) and `check` enforce expiry regardless of what any UI showed. Presentation: workflow
status `approval_expired`; `ApprovalOut.effective_status`; derived lifecycle state `Expired`; governance `request.expired`;
dashboard headline **EXPIRED** with no approve/reject control. Covered by `tests/test_approval_expiry_presentation.py` (25 tests:
expired, unexpired, invalid and missing expiry, timezone forms, non-pending statuses, API, dashboard).

## Evidence provenance and integrity

Policy `evidence-policy-v1` ([governance-evidence-policy.md](governance-evidence-policy.md)): cited evidence needs metadata
(tool, status, `collected_at`, investigation id), a known read-only tool, a status that supports repair, the same investigation,
a valid timestamp (not more than 60 s in the future, not older than 60 minutes). Checked at proposal and again at execution. At
request time a SHA-256 `evidence_digest` is written into the ledger and recomputed before execution; a mismatch denies with
`evidence_integrity_failed`. Requests without a recorded digest (legacy) fail closed. Limits: `collected_at` is locally stamped and
unsigned; the digest does not detect a forged value written before the request.

Fact vs hypothesis (INV-14, **partial**): a finding's kind is set by which report list it sits in; a finding must cite evidence
present in the report; a deterministic `review_reasons()` flags certain-sounding hypotheses, hedged "facts" and facts citing only
error evidence (`review_required`). It never promotes or clears a finding, and hypotheses never supply evidence to a proposal.

## Idempotency

Key: `idem-` + SHA-256[:40] over incident, action, parameters, target. At most one open claim per key; claim states `claimed`,
`succeeded`, `failed`, `uncertain`, `released`, `reconciled`. An `uncertain` claim blocks the same operation until a person
reconciles it (`reconcile_uncertain` is a service method only; no HTTP route or CLI). **Limitation:** this controls this
application's own execution path. It does not guarantee that an external action happens exactly once, and an ambiguous outcome is
reported as `uncertain` rather than retried.

## Audit ledger and dashboard verdict semantics

VerbaLedger (`data/governance_ledger.jsonl`) is a hash-chained JSON Lines file: tamper-**evident** within the file, not immutable,
not externally anchored; an old consistent chain or a truncated tail is undetectable without an external checkpoint.

The governance panel (`GET /api/v1/recoveries/{id}/governance`, built by `app/recovery/governance_view.py` from stored rows and
ledger outcomes only) shows per-PreNode verdicts from `app/recovery/prenode_verdicts.py`:

| Verdict | Meaning |
|---|---|
| `passed` / `failed` | Recorded outcome says so (basis `recorded`), or the fixed stage order implies it (basis `implied_by_later_step`, for example a recorded effect implies the earlier target check passed) |
| `unknown` | The outcome could not be established (for example an uncertain execution) |
| `not_recorded` | Nothing recorded; shown as `NOT RECORDED (this is not a pass)`; basis `none` |

Implied is never presented as recorded. An empty or partial governance response never renders a success.

## Database safety

See [installation-and-configuration.md](installation-and-configuration.md#database-setup-and-separation). Summary: no SQL recovery
target unless `_test`, allow-listed and `DB_NAME()`-verified; `dataops_sim` is never a target; the integration-test fixture wipes
its target and therefore needs `SQLSERVER_TESTS_CONFIRM_WIPE=<exact name>` and refuses `dataops_demo_test`.
