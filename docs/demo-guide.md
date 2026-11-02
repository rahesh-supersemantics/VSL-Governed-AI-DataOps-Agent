# Demo guide

Everything here is synthetic and offline unless stated. Execution in the offline demos is a counting **mock** executor or the
in-memory simulation; nothing writes to SQL Server. More detail: [governance-demo.md](governance-demo.md).

## 1. Fastest check (no database, network or model)

```powershell
python -m pytest tests/test_governance_demo.py -s -q
```

Prints a narrated transcript and asserts each act: an agent proposes a high-risk recovery and waits; a viewer and a forged token are
refused; an authorised person approves (which executes nothing); resume runs the gates, ledger check, target check, single-use
claim, execution and independent verification; replaying the approval is refused; a failed verification produces a compensation
plan that is only proposed plus an escalation and no retry; the low-risk policy is off by default.

## 2. The four governance demos

| Demo | Shows | Proof in `tests/` |
|---|---|---|
| A. Bounded low-risk retry | Policy authorisation (opt-in, off by default), VSL gates, claim, independently verified outcome; the second automatic retry needs a person | `test_governance_acceptance.py` (T-01 tests in `acceptance_map.py`) |
| B. High-risk approval | Denial without approval; approval bound to action, parameters, target and evidence; approving executes nothing | `test_governance_demo.py`, T-02, T-03 |
| C. Failure and escalation | Compensation plan only proposed; escalation; no retry; an `uncertain` claim blocks reruns until reconciled | `test_governance_demo.py`, T-12, T-13, `test_governance_idempotency.py` |
| D. Unsafe target | Simulator, system, unallowlisted and mismatched databases refused with no write | T-04, T-22; the real-server check is `scripts.check_database_identity` |

Run one: `python -m pytest <file>::<test> -s -q`. The T-numbers map to tests in [governance-acceptance-tests.md](governance-acceptance-tests.md).

## 3. Stubbed dashboard (real browser; you start it)

```powershell
.\.venv\Scripts\python.exe -m scripts.stub_governance_demo
```

Starts a read-only stub API on 8600 and the real dashboard code on 8601; touches no database, store, ledger, `.env` or model; refuses
8000/8501/8502 and any busy port; refuses writes with 405; `Ctrl+C` stops it. Open http://127.0.0.1:8601, go to **Recovery &
approvals** and read incidents `[STUB] stub-1` to `stub-8`. Expect: verified success; pending (nothing authorised or attempted);
failed verification with an escalated, not-executed plan; unknown outcome; expired-while-stored-pending (**EXPIRED**, no decision
control); empty and partial responses (no success shown); a blocked duplicate. `NOT RECORDED (this is not a pass)` marks unrecorded
stages. Do not press any approve control. The same cases run headless in `tests/test_stub_governance_panel.py`.

## 4. Live walk-through (not run by the author)

Needs a separate allow-listed `_test` database (see [installation-and-configuration.md](installation-and-configuration.md#enabling-recovery-safely));
it changes rows in that test database, so run it only when you intend to.

1. Start the stack: `.\.venv\Scripts\python.exe -m scripts.dev_stack --show-approver-token --open`.
2. Dashboard, **Incidents**: import a simulator incident, then **Investigate** (offline mode, no data leaves the machine).
3. **Recovery & approvals**: request a recovery. Expect PendingReview, *NOT authorised*, *NOT attempted*.
4. As a viewer, try to approve: refused. As the approver (token you printed), approve: execution is still *NOT attempted*.
5. Resume: *ATTEMPTED and independently VERIFIED* appears only if the postcondition passed. Resume again: refused (already used).
6. Open the VerbaLedger Explorer, find the decision, run **Integrity**.
7. Undo any injected fault with `python -m scripts.scenario restore-all`.

The synthetic `DEMO-nnnn` incidents are browse-only and have no recovery proposal, by design.
