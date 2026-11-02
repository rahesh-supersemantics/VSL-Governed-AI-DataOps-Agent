# VSL governance architecture (what exists, in order)

A recovery request passes these stages. Stage names are the PreNodes in `docs/governance-catalogue.md`; only the evidence
and approval rule and two invariants are compiled VSL gates (`app/governance/policy.py`, `gates.py`). The rest is
application code placed beside them, and is labelled as code in the catalogue.

```
investigation (offline or live) ──> proposal ──> PN-03 action valid? ──> PN-02 evidence cited & known? ──> approval request (PendingReview)
   person approves (PN-01/PN-05)  or  policy authorises (low risk, opt-in, bounded)        -- approval is NOT execution
resume ──> VSL gates ──> ledger verifies ──> PN-04 target allowlisted + DB_NAME() matches ──> PN-06 fresh preconditions
       ──> PN-07 atomic single-use claim ──> executor (only the runner calls it) ──> PN-08 independent verification
       ──> executed | verification_failed | uncertain ──> (failure/unknown) compensation PLAN proposed + escalation, no retry
```

- **Authority.** Model output cannot authorise anything. Authorisation is a person's approval or `policy:risk-policy-v1`; the
  approval service refuses that principal from any caller.
- **Fail closed.** A ledger that does not verify, a gate error, an unreadable ledger, an unknown risk, or a target mismatch
  stops execution and leaves the approval unspent.
- **Compensation is design-only.** `plan_for` returns a plan with `executable=False`. Nothing rolls back or writes.
- **Audit.** A hash-chained JSONL ledger (tamper-evident, not immutable). The dashboard governance panel reads
  `GET /recoveries/{id}/governance`, which is built from stored rows and ledger outcomes only.
- **Scenarios.** A to D of the brief are `tests/test_governance_demo.py` (offline). See `docs/governance-demo.md`.

# Setup and configuration (safe defaults)

| Setting | Purpose | Default |
|---|---|---|
| `RECOVERY_TARGET_ALLOWLIST` | comma list of `db` or `server/db`; every entry must still end in `_test` | empty: no SQL recovery target is allowed |
| `RECOVERY_AUTO_AUTHORIZE_LOW_RISK` | opt-in policy authorisation of the bounded retry | `false` |
| `APPROVAL_AUTH_MODE` | `disabled` or `dev_static` (local only; refused in production) | `disabled`: nobody can approve |
| `APPROVAL_DEV_CREDENTIALS` | JSON list of subject, token digest, roles. Digests only | unset |
| `APPROVAL_SIGNING_KEY` | seals approvals | unset: approvals unavailable |
| `INVESTIGATION_MODE` | `offline` (deterministic, no model) or `live` (needs `OPENAI_API_KEY`) | `offline` |

Never put real keys in docs, tests or the repository. `.env` is git-ignored (checked).

# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Human Review shows "recovery unavailable" with `UnsafeRecoveryTarget` | the database is missing from `RECOVERY_TARGET_ALLOWLIST`, is not `*_test`, is `dataops_sim` or a system database, or the connection string has no `DATABASE=`. The dashboard message states which. Do not loosen the guard. |
| Approval refused with `unauthenticated` / `not_permitted` | token unknown, revoked, or role lacks the approval permission |
| Resume returns `approval_already_used` | the approval was spent; a new request needs a new approval |
| Resume returns `expired` | approval older than its time-to-live (30 min default); request again |
| Outcome `verification_failed` | the action ran but the postcondition was not met (for example a retry while the cause persists). Escalated; investigate by hand |
| Outcome `uncertain` | the result is unknown (for example a restart after the approval was spent). Check the system; it is never retried automatically |
| `escalation_record_failed` in reasons | the ledger could not record the escalation; the status is still not success. Fix the ledger and review manually |

# Known limitations and future work

- Not run: SQL Server, Windows, a real browser for the stub check, and any live model call.
- INV-14 (fact vs hypothesis) is only partially enforced (structure plus deterministic review flags; nothing is promoted or cleared
  automatically). INV-20 (governed compensation) is not enforced: compensation is a plan only.
- Evidence provenance and freshness are checked by PN-02 (see `governance-evidence-policy.md`), but `collected_at` is stamped locally
  and unsigned. A deterministic idempotency key and an execution-claim audit trail exist; they do not make external effects
  exactly-once. The policy version is not part of the approval binding.
- The ledger is tamper-evident only. Only 2 invariants and 1 PreNode are compiled VSL gates.
- Future work, each needing its own governance lifecycle: executable compensation; signed evidence collection; a central lifecycle
  state machine.
