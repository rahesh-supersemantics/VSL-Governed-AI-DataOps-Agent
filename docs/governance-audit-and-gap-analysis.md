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
| Compensation | Design only plus escalate | `app/recovery/compensation.py` (plan, `executable=False`) and ledger entries `compensation_plan_proposed` and `recovery_escalated`. No compensating write exists. |

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

## 7. Status after the second pass (completed, partial, outstanding)

Offline suite: **2,088 passed, 45 skipped, 0 failed** (run in 8 file chunks). The 45 skips are 44 SQL Server integration
tests and 1 live-model test. **They were not run and have not passed.** No SQL Server, database write, scenario
activation, recovery against a real database, or external model call was made.

| Requirement | Status | Evidence |
|---|---|---|
| Design-only compensation plan and escalation | Completed | `compensation.py`, `runner._escalate`, `reconcile`; T-15, T-16, T-17 |
| Eight PreNodes and 22 invariants, tied to enforcement | Completed as a catalogue, **partial as VSL**: only 2 invariants and 1 PreNode are compiled VSL; the rest are code stages, labelled | `docs/governance-catalogue.md`, resolving test |
| Lifecycle mapping | Completed. 5 of 14 states are not stored (Investigating, Proposed, ReadyToExecute, Compensating, Compensated) and are documented as such | `lifecycle.py`, catalogue doc |
| T-03 to T-21 as named, mapped tests | Completed offline; T-20's real call is not run | `docs/governance-acceptance-tests.md` |
| Dashboard governance panel from backend and ledger | Completed; verified with AppTest on the real backend, not in a browser this pass | `governance_view.py`, `GET /recoveries/{id}/governance`, `ops_data.governance_panel` |
| Demo script | Completed offline; the live walk-through is documented and not run | `tests/test_governance_demo.py`, `docs/governance-demo.md` |

Still partial or outstanding:

- INV-14 (fact vs hypothesis) is not enforced. INV-20 (governed compensation) is not implemented, by decision.
- PN-02 does not check evidence freshness or provenance. PN-03 has no idempotency or compensation-readiness gate; PN-07
  has no separate idempotency key (single-use approval bound to the digest serves instead).
- The policy version is not a field of the approval binding; the policy context is bound through the governance-state reference.
- The ledger is tamper-evident, not immutable.
- Nothing in this pass ran against SQL Server (including `current_database()`), on Windows, or in a browser.
- T-20 real model call: awaiting your explicit approval.

## 8. Third pass: reconciliation with the consolidated master prompt

- Invariant titles now follow the latest brief. INV-12 absorbs the single-execution-claim check; INV-17 (no unsupported success
  claims) and INV-18 (independent verification) are separate entries.
- `tests/acceptance_map.py` maps the latest T-01..T-22 numbering to tests; `docs/governance-acceptance-tests.md` is generated from it.
  Test function names in `test_governance_acceptance.py` keep the earlier numbering.
- Added: PreNode specifications, a traceability matrix (invariant, PreNode, ledger outcome, dashboard), a lifecycle table with preceding
  and next states, lifecycle transition tests against the real store (T-19), scenarios A and D in the demo, analysis mode (offline or
  live), observed-findings and hypotheses counts, request risk, target, evidence and timestamps in the governance view and panel.
- Generated docs are checked against the code by a test, so they cannot drift.
- Prepared, not executed: `docs/pending-approvals.md` (SQL Server plan, live-model plan, publishing checklist).
- **Finding:** the SQL Server integration fixture wipes and reseeds the `_test` database before every test. See that document.

Not done or only partial against the latest brief: the dashboard does not show a per-PreNode result (the view reports ledger
outcomes, not a PreNode verdict); INV-14 and INV-20 remain unenforced; evidence freshness, a central state machine and executable
compensation remain future work; browser verification of the new panel was not repeated; no run on Windows or SQL Server.

Offline suite after this pass: **2,119 passed, 45 skipped, 0 failed** (83 test files, run in 8 chunks). The 45 skips are 44 SQL Server
integration tests and the one live-model test; they were not run and have not passed. No lint or type tool was run.

## 9. Fourth pass: remaining safe gaps (status)

| Gap | Status | What changed / why it stops there |
|---|---|---|
| Read-only DB identity diagnostic | **Implemented, not run** | `scripts/check_database_identity.py` runs only `SELECT DB_NAME()` and `SELECT @@SERVERNAME` through the app's `make_engine`. Exit 0 match, 1 mismatch or no identity, 2 config/connection failure. Mocked unit tests only; never run against SQL Server. |
| INV-14 facts vs hypotheses | **Partially implemented** | Findings carry `kind` (observed / hypothesis) set by code from the list they sit in; the report validator rejects mismatches and findings that cite no or unknown evidence; only tool-observed `issue_detected`/`warning` evidence can support an action. Not verified: whether hypothesis wording is suitably hedged (cannot be checked without trusting model text). |
| PN-02 provenance and freshness | **Implemented** | `docs/governance-evidence-policy.md`. Checked at proposal and again at execution inside the compiled PreNode's monitor. Missing metadata, unknown tool, wrong investigation, bad/future/stale timestamp, or a non-supporting status all deny. Limit: metadata is stamped by our code, not signed; the stored table is not sealed. |
| Idempotency | **Implemented** | Key = hash(incident, action, parameters, target); atomic claim with one open row per key; uncertain claims block reruns until an approver reconciles them (service method only, no HTTP/CLI route: deferred). Tests include concurrent duplicates. Plain Python, not a compiled gate. |
| Dashboard per-PreNode verdicts | **Implemented** | Verdicts (passed / failed / unknown / not recorded) derive only from recorded ledger payloads, and each states whether it was recorded or implied by a later step. Unrecorded stages show "NOT RECORDED (this is not a pass)". Not checked in a browser. |
| VSL coverage | **Deferred (no expansion)** | Compiled constructs remain 2 invariants (`action_within_allowlist`, `no_execution_while_suspended`) and 1 PreNode (`verified_report_and_approval`). The VSL contract compiles an async boolean invariant or a gamma-threshold monitor; target guard, idempotency, lifecycle and verdict derivation need database state and side effects, which that contract does not express, so they stay ordinary Python and are labelled `code`. A test fails if any entry is labelled `vsl_gate` without an `app.governance` enforcement point, or if a fourth compiled gate appears unnoticed. |

Not run in this pass: SQL Server (including the identity script), browser, live LLM, Windows.

## 10. Fifth pass: execution of the final master prompt (2026-11-02)

| Item | Status |
|---|---|
| Identity diagnostic (Stage A/J) | **Run, exit 2, database not reached.** Workspace is Linux with no ODBC driver and no route to the Windows-authenticated instance. Config read: `localhost\SQLEXPRESS03`, `dataops_sim`, windows auth. Needs a run on your machine (`docs/pending-approvals.md` section 0). |
| Evidence integrity | **Strengthened.** Digest recorded in the ledger at request time and re-verified before execution (`evidence_integrity_failed`). Timestamps remain unsigned. No migration. Tests: `tests/test_governance_integrity_and_review.py`. |
| INV-14 | **Still partial.** Added a deterministic review screen (certain-sounding hypotheses, hedged facts, facts resting only on failed tools). It flags, never promotes or clears; callers cannot pre-clear a flag. Hypotheses cannot supply evidence to a proposal (tested). Semantic correctness of wording is not established and not claimed. |
| Idempotency audit trail | **Strengthened.** `idempotency_claim_acquired` is recorded before the approval is spent (if it cannot be recorded the claim is released and nothing runs); the final entry records how the claim was settled; denials carry the key; reconciliation was already recorded. |
| T-15 / T-20 | T-15 gained a second offline proof (no network, live client never built). T-20 remains PENDING; the smoke script's payload is a fixed prompt with no project data (tested offline). |
| SQL Server isolation (Stage K) | **Plan written, nothing run or created.** Found that `TEST_DB_NAME=dataops_demo_test` would have been wiped (`_test` suffix, not protected); added `assert_wipe_confirmed` (typed confirmation and refusal of the demo database) with offline tests. |
| VSL coverage | Unchanged: 2 invariants + 1 PreNode compiled. No new compiled gate was added; see section 9 for why. |
| Stages not verifiable here | Browser view of the dashboard, Windows run, SQL Server, live model. |

## 11. Sixth pass: expired-approval presentation (2026-11-02)

**Confirmed defect (display only).** An approval is stored `pending` until someone tries to decide it, so a request past `expires_at`
still counted as "Awaiting approval", read "PendingReview", and offered a decision control. Server-side enforcement was already correct
(a decision after expiry is refused as `expired`; execution re-validates expiry), so nothing could run.

**Fix (code only, no stored data changed).** One predicate, `app.recovery.expiry` (`is_expired`, `effective_status`): only a stored-`pending`
request can be effectively expired; `expires_at <= now` (the boundary counts); a trailing `Z` or no zone means UTC; a missing or
unparseable `expires_at` is NOT reinterpreted and stays as stored (the contract Human Review already used). It feeds: the workflow status
(new value `approval_expired`, so the Overview "Awaiting approval" count drops it while "Open high/critical" still counts the unresolved
incident), `recovery_phase`, `ApprovalOut.effective_status` (additive field), `logical_state` (`Expired`, derived) and the governance
`request.expired`, and on the dashboard `approval_actionable` and `recovery_summary`. Human Review now uses the same predicate.
Evidence-integrity checks are untouched: requests with no recorded digest still fail closed at execution.

**Stubbed verification (B1).** `tests/test_stub_governance_panel.py` and `scripts/stub_governance_demo.py`; labelled stubbed, not live. A
real browser look at the stub awaits you running the launcher (`docs/pending-approvals.md` section 4).
