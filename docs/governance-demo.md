# Governance demo (repeatable, offline)

## 1. Scripted run (verified offline, no database, no network, no model)

```
pytest tests/test_governance_demo.py -s
```

It prints a narrated transcript and asserts each act: (1) an agent proposes a high-risk recovery and waits; (2) a viewer and
a forged token are refused; (3) an authorised person approves, which executes nothing; (4) resume runs the VSL gates, ledger
check, target check, single-use claim, execution and independent verification; (5) replaying the approval is refused;
(6) a failed verification produces a compensation plan that is only proposed, an escalation and no retry;
(7) the low-risk policy is off by default.

## 2. Live walk-through (NOT run during this work; you run it)

Precondition: the dev stack is up with `RECOVERY_TARGET_ALLOWLIST` set to your `_test` database. This changes data in that
test database, so run it only when you intend to. Use only synthetic scenarios.

1. Dashboard, Investigate: create an incident and run an offline investigation. No data leaves the machine.
2. Recovery & approvals: request a recovery. The page shows **PendingReview**, *NOT authorised*, *NOT attempted*.
3. As a viewer, try to approve: refused. As an approver, approve: the Governance block now says *By a PERSON* and execution
   is still *NOT attempted*.
4. Resume. The block shows *ATTEMPTED and independently VERIFIED* only when the postcondition check passed.
5. Resume again: refused, approval already used.
6. Failure path: when an outcome is `verification_failed` or `uncertain`, the block shows *ATTEMPTED, NOT verified*,
   *Compensation PROPOSED only, NOT executed* and *Escalated*.
7. VerbaLedger Explorer: open the decision id to see the hash-chained entries (tamper-evident, not immutable).

Optional live model: only after you explicitly approve it (cost; the incident text is sent to the provider).

## 3. The four demo scenarios (offline; execution is a counting MOCK executor or the in-memory simulation, never real)

Each runs as a test you can read and run: `python -m pytest <file>::<test> -s -q`. Every one uses fixtures; none writes to SQL Server.

| Demo | What it shows | Proof (all in `tests/`) |
|---|---|---|
| A. Bounded low-risk retry | evidence, policy classification, explicit authorisation by POLICY (opt-in, off by default), VSL gates, execution claim, independently verified outcome; the second automatic retry for the same incident needs a person | `test_governance_acceptance.py` T-01 tests listed in `acceptance_map.py` (`test_low_risk_retry_is_authorised_by_policy_executed_and_verified`, `test_second_automatic_retry_for_the_same_incident_needs_a_person`) |
| B. High-risk approval | denial without approval; approval bound to the exact action, parameters, target and evidence; approving executes nothing; resuming runs gates, claim, execution, verification | `test_governance_demo.py` (narrated, run with `-s`), T-02, T-03 |
| C. Failure and escalation | failed or uncertain action: compensation plan PROPOSED only, escalation recorded, no retry, uncertain claim blocks reruns until a person reconciles | `test_governance_demo.py` step 6, T-12, T-13, `test_governance_idempotency.py::test_an_uncertain_outcome_blocks_the_same_operation_until_a_person_reconciles_it` |
| D. Unsafe target | simulator, system, unallowlisted and mismatched live databases are refused with no write (mismatch is checked against a SQL-like fake repo reporting a different DB_NAME) | T-04 and T-22 in `test_governance_acceptance.py`; the real server check is the pending identity diagnostic |

## 4. Stubbed dashboard check (`scripts/stub_governance_demo.py`) - not live
Canned `[STUB]` incidents cover: verified success, pending (nothing authorised or attempted), failed verification with an escalated plan,
unknown outcome, expired-while-stored-as-pending, empty governance response, partial response, and a blocked duplicate. The governance
views are produced by the real `build_governance_view` from SYNTHETIC ledger outcomes, so they show the real derivation on invented
inputs; they say nothing about any real recovery. The stub API is read-only. The same cases run headless in
`tests/test_stub_governance_panel.py`.
