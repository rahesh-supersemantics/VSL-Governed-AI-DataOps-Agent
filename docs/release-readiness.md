# Release readiness

Assessment date: 2026-11-02. Scope: the **simulator** version of the AI DataOps Agent. Every status below is backed by a recorded
run or states that it is not.

## Verdict

| Level | Assessment |
|---|---|
| Implementation-complete for the simulator scope | **Yes**, with the partial items listed below |
| Release-ready for the documented scope (offline, simulator, development auth) | **Conditionally**: ready to publish as a documented reference implementation once the owner has reviewed the Git changes. The integration checks below are unverified and are disclosed, not hidden |
| Production-ready | **No.** Required integration checks are unverified, authentication is development-only, and several controls are partial |

## Verification status

| Item | Status | Evidence / blocker and next safe step |
|---|---|---|
| Full offline test suite | **Passed** (Linux, Python 3.10) | 2,261 passed, 45 skipped, 0 failed; 2,306 collected ([testing-and-verification.md](testing-and-verification.md)) |
| Offline suite on Windows / Python 3.12 / CI | **Not verified** | Run `python -m pytest -q` in your venv; CI never observed |
| SQL Server integration tests (44) | **Not run** (skipped) | Destructive. Needs an empty disposable `*_test` database you create, `SQLSERVER_TESTS_CONFIRM_WIPE`, your approval |
| Windows SQL Server identity diagnostic | **Passed** (reported by the user) | Configured and actual database `dataops_sim`; Windows auth; not allow-listed. Recovery stays guarded off |
| Recovery against a real `_test` database | **Not run** | Needs a separate allow-listed `_test` database and your approval |
| Live LLM path (T-20) | **Not run** (1 test skipped) | `python -m scripts.openai_smoke --yes-call-openai`; billable; needs approval |
| Expired-approval presentation fix | **Implemented, tested offline** | `app/recovery/expiry.py`; 25 tests in `test_approval_expiry_presentation.py`; server-side expiry, binding and fail-closed unchanged. The user's running stack predates it (started 15:47): restart it to pick the fix up |
| Governance panel, all PreNode verdict states | **Stubbed headless: passed**; stubbed real browser: **not done**; live: **not possible** with `dataops_sim` | `tests/test_stub_governance_panel.py` (9); next step: user runs `python -m scripts.stub_governance_demo`, then views http://127.0.0.1:8601 |
| Recorded vs implied vs failed/unknown vs `NOT RECORDED` | **Implemented, tested** | `app/recovery/prenode_verdicts.py`, `test_governance_prenodes.py` (13) |
| Proposed / approved / unattempted never shown as executed | **Tested** | acceptance map T-01..T-22, `test_governance_lifecycle.py`, stub panel tests |
| Evidence provenance and integrity digest (PN-02) | **Implemented, tested** | `test_governance_evidence.py` (30), `test_governance_integrity_and_review.py`. `collected_at` is unsigned |
| Idempotency and claim audit trail | **Implemented, tested** | `test_governance_idempotency.py` (11). Not exactly-once for external effects |
| INV-14 fact vs hypothesis | **Partial** | Structure plus deterministic review flags; no automatic promotion |
| INV-20 governed compensation | **Not implemented** | Plan only (`executable=False`) |
| Compiled VSL coverage | 2 invariants + 1 PreNode | No expansion; none justified by a current architectural requirement |
| Static checks (lint, types) | **None configured** | No config in repo; none run |
| Docker build, compose, GitHub Actions | **Not run** | Present only as foundations |
| GitHub ticketing | **Tested with fakes only** | Not run against real GitHub |
| README and docs commands, routes, links | **Checked** | Routes vs generated OpenAPI (42 paths); entry points exist; internal links and secret-pattern scan below |
| Secret-pattern scan | **Clean** | Only benign hit: a dummy password literal in `tests/test_check_database_identity.py` used to prove log redaction; no `.env`, database, token or log is tracked |

## Remaining risks

1. SQL Server behaviour is unverified by automated tests in this environment (44 tests skipped).
2. Live OpenAI behaviour and cost are unmeasured.
3. Development static-token authentication only; no TLS, rate limiting across replicas or multi-replica stores.
4. Evidence timestamps are locally stamped and unsigned; the ledger is tamper-evident, not immutable.
5. External effects are not guaranteed exactly-once; ambiguous outcomes are `uncertain` and need a person.
6. The documentation screenshots predate the governance panel work and come from a Linux sandbox.

## Safe next steps (in order)

1. Review the Git diff, then stage named files yourself or authorise a commit (not done by this pass; nothing was staged or pushed).
2. Restart the dev stack so it serves the expiry-presentation fix.
3. Run `python -m scripts.stub_governance_demo` and view the panel in a browser.
4. Run `python -m pytest -q` on Windows.
5. With separate approval: create a disposable `_test` database and run the SQL Server integration suite; run the live smoke test.

## Items deliberately not changed

`.env`, `DB_NAME`, `RECOVERY_TARGET_ALLOWLIST`, the running stack, existing SQLite stores and the ledger, and any database.
