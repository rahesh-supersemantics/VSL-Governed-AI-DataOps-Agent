# Governance audit and gap analysis (Phase A, with the first Phase B fix)

Status: audit by code reading plus offline tests. Nothing here was run against SQL Server.
"Read" means checked in source; "tested" means an offline test exercises it.

## 1. Root cause of `recovery_target_unavailable:UnsafeRecoveryTarget`

The guard (`app/recovery/actions.py`, `assert_recovery_target`) took the database from `engine.url.database`.
The application builds every engine as `URL.create("mssql+pyodbc", query={"odbc_connect": <ODBC string>})`
(`app/db/connection.py`, `make_engine`). SQLAlchemy keeps that string opaque, so `engine.url.database` is `None`
for it (checked directly on the VM). The guard therefore saw an empty name and refused every SQL Server target,
including `dataops_demo_test`. The dashboard still showed `dataops_demo_test` because that value comes from
`settings.db_name`, not from the engine.

Why tests missed it: the Phase 13 test replaced `make_engine` with a URL in a different shape
(`mssql+pyodbc://user@host/<name>`), and `test_recovery_auth_actions.py` used a hand-made object with a
`database` attribute. Neither matched the real engine. Both now use the real shape.

Not confirmed live: the running API process on your machine was not inspected. A stale process or a different
`DB_NAME` in that process would be a second, separate cause; `dev_stack` prints the API's own answer.

## 2. Change made (smallest correct fix, guard not weakened)

- `resolve_connection_target(engine)` reads server and database from the ODBC string. Two different values for the
  same key, or a URL/string mismatch, raise `UnsafeRecoveryTarget`.
- A target must now satisfy BOTH: name ends with `_test` AND appears in `RECOVERY_TARGET_ALLOWLIST`
  (`database` or `server/database`). An empty allowlist denies every SQL target. `dataops_sim`, `master`, `model`,
  `msdb`, `tempdb` are refused even if listed.
- `assert_live_target` asks the server `SELECT DB_NAME()` in the precondition step, before any side effect, and
  requires it to equal the validated database. Unverifiable means no action (`PreconditionFailed`).
- The target is revalidated inside `check_precondition`, not only at start-up.
- Behaviour change you must act on: set `$env:RECOVERY_TARGET_ALLOWLIST = "dataops_demo_test"` before starting
  the API. Without it Human Review stays off, with a message that says so.

## 3. Database baseline discrepancy (dataops_demo_test)

Active scenario `SCN-03-DUPLICATE-RECORDS` is created by `build_duplicate_orders`, which copies exactly 3 existing
orders and their items with new order ids. That matches "3 scenario-tagged orders" and 173 + 3 = 176. The item
difference (448 to 454, 6 rows) is consistent with 3 copied orders but was not proven from code. The verifier
reports the active scenario as a deviation from the clean baseline, which is expected while it is active.
This is an expected state, not evidence of corruption. No rows were touched or deleted. Recovery safety does not
depend on these counts. Re-running the verifier against SQL Server needs your approval.

## 4. What already existed (read)

| Area | Where | Notes |
|---|---|---|
| VSL gates | `app/governance/policy.py`, `gates.py`, `workflow.py` | 2 invariants (allowlist, not suspended), 1 PreNode (actionable report, cited known evidence, matching approval), 1 terminal state. Compiled with `vsl_langgraph`. |
| Action allowlist, strict schema | `app/recovery/actions.py` | `rerun_pipeline` (medium), `remove_injected_condition` (high). Unknown fields rejected. |
| Approval binding, seal, expiry, single use | `app/recovery/approvals.py` | SHA digest over incident, action, params, target, evidence; HMAC seal; atomic `consume`. |
| Runner order | `app/recovery/runner.py` | attempt record, suspension recheck, precondition, consume approval, execute, independent verify, result record. Outcomes executed, blocked, failed, verification_failed, uncertain. |
| Audit | `app/governance/ledger.py` | hash-chained ledger, verified before acting. Tamper-evident, not immutable. |
| Restart reconciliation | `app/services/recoveries.py` `reconcile_all` | A consumed approval with no result becomes `recovery_uncertain`. |

State-changing routes (read): `POST /incidents`, `/incidents/{id}/investigations`, `/incidents/{id}/recoveries`,
`/recoveries/{id}/resume`, `/approvals/{id}/decision`, `/simulator/incidents/{id}/import`, and the two ticket
routes. The approval route records a decision only; execution is in `recoveries/resume`.

## 5. Gaps against the brief (not yet implemented)

| Brief item | Status |
|---|---|
| Actions `retry` and `reprocess` | Not present. Existing actions are `rerun_pipeline` and `remove_injected_condition`. Mapping or renaming needs your decision. |
| Low-risk automatic authorization (INV-09, T-01) | Not present. Every action needs human approval today. Adding it is a security-policy change. |
| Versioned risk policy, policy version in records | Not present. Risk is a static per-action string. |
| Compensation and rollback (INV-19, INV-20, T-15, T-16) | Not present. Failures stop and report; nothing compensates. |
| Eight named PreNodes PN-01 to PN-08 | One PreNode exists. Target, action and evidence checks run in code outside the VSL gates. |
| Lifecycle states in section 8 | Existing states differ (`awaiting_approval`, `resuming`, `executed`, `recovery_uncertain`, ...). A mapping is needed, not a second machine. |
| Invariant catalogue with tests per invariant | Not present as a document. Many are covered by existing tests; coverage per invariant is unmapped. |
| T-01 to T-22 as named tests | Some behaviours are covered by existing tests, not named or mapped. |
| Dashboard panels for PreNode and invariant results, compensation, policy version | Not present. |
| Live LLM call evidence (T-20) | Needs your explicit approval (cost, data sent to the provider). |

## 6. Decisions taken (your answers) and what was implemented after the audit

| Decision | Choice | Implemented |
|---|---|---|
| Action names | Map onto existing: `rerun_pipeline` is the retry, `remove_injected_condition` is the reprocess | Names unchanged, no migration. |
| Low-risk path | Add, bounded retry only | `app/recovery/risk_policy.py`, `risk-policy-v1`. Off unless `RECOVERY_AUTO_AUTHORIZE_LOW_RISK=true`. |
| Compensation | Design only plus escalate | Not implemented yet. |

### Risk policy v1 (what it does and does not do)

- LOW: `rerun_pipeline` for `order_validation` and `reporting_prep`. They read business tables and write only a run
  record, validation rows, a log file, a simulator incident on failure, and advance the simulation clock.
- HIGH: `rerun_pipeline` for `order_ingestion` (it inserts orders and is not proven idempotent) and
  `remove_injected_condition`. UNKNOWN: anything else, which is denied.
- Automatic authorisation needs all of: policy switched on, class LOW, report `issue_identified`, cited evidence,
  and fewer than 1 earlier automatic authorisation for the same incident, action and target.
- It is recorded as `approval_granted_by_policy` (ledger) with `decided_by = policy:risk-policy-v1`, the policy
  version and rule id. It is sealed, bound to the request digest, expiring and single-use like a human approval.
- It is re-checked each time it is used. Switching the policy off after authorisation makes it invalid
  (`policy_no_longer_permits`). The action still goes through the same VSL gates, target checks and independent
  verification. A person cannot present as the policy principal: `:` is not allowed in subjects, and the approval
  service refuses the principal anyway.
- Honest limit: the retry's verification demands a run that finishes `success`. When the underlying condition
  persists (for example an active duplicate-records scenario), the run fails and the outcome is
  `verification_failed`, never success. This is covered by a test.

### Tests added in this pass

- `tests/test_recovery_auth_actions.py`: real-shaped (ODBC) engines, allowlist, ambiguous strings, live-target check.
- `tests/test_phase13_human_review_config.py`: now uses the real engine URL shape (the old shape hid the defect).
- `tests/test_risk_policy.py` (19): T-01, T-02, T-13 style revalidation, T-22, boundedness, impersonation, tampering.
- `tests/test_stage6_ui.py` (6) from the earlier UI stage.

Offline suite after this pass: 1,924 passed, 0 failed, 44 SQL Server tests skipped (not run). No SQL Server,
OpenAI or other network call was made.

### Still open from section 5

Compensation plan and escalation state, the eight named PreNodes and per-invariant catalogue with a test map,
lifecycle-state mapping, the dashboard governance panel, T-03 to T-21 as a named, mapped suite (several are already
covered by existing tests but not mapped), the demo script, and any live LLM evidence (needs your approval).
