# Testing and verification

All figures below are from a real run on **2026-11-02** in the Linux workspace (Python 3.10, venv `~/s3/v`), against the working tree
at that time. They are not claims about Windows, Python 3.12 or CI.

## Commands

```powershell
python -m pytest -q                       # full offline suite (no SQL Server, no OpenAI key, no network)
python -m pytest -q -rs                   # same, with the reason for every skip
python -m pytest tests/test_governance_demo.py -s -q          # narrated governance demo
python -m pytest tests/test_approval_expiry_presentation.py -q # expiry presentation regression tests
python -m pytest tests/test_stub_governance_panel.py -q        # headless dashboard check against stub data
python -m scripts.generate_governance_docs                      # regenerates governance-catalogue.md and governance-acceptance-tests.md
```

`pytest.ini` sets `testpaths = tests`, `pythonpath = .` and the `integration` marker. No linter, type checker or formatter
configuration exists in the repository (no `pyproject.toml`, `setup.cfg`, `ruff`/`mypy`/`flake8` config), so **no static check was run**;
CI runs only pytest and `pip check`.

## Result of the last full run

The environment limits a command to about 120 seconds, so the 92 test files were run in five sequential chunks
(`ls tests/test_*.py tests/*/test_*.py | sed -n A,Bp | xargs python -m pytest -q -p no:cacheprovider`):

| Files | Passed | Skipped |
|---|---|---|
| 1-25 | 702 | 44 (SQL Server integration) |
| 26-50 | 784 | 1 (live model) |
| 51-62 | 345 | 0 |
| 63-75 | 106 | 0 |
| 76-92 | 324 | 0 |
| **Total** | **2,261** | **45** |

`pytest --collect-only` reports 2,306 tests, which equals 2,261 + 45. **0 failed.**

## Skipped tests (not passes)

* **44** in `tests/integration/test_sqlserver.py` and the other `*_sqlserver.py` files: need `RUN_SQLSERVER_TESTS=1` and a real SQL
  Server. Not run.
* **1** in `tests/test_governance_acceptance.py` (T-20): a real OpenAI request; explicitly skipped ("NOT RUN") until approved.

## What the offline tests cover

API routes and permissions (in-memory fakes), persistence, the agent graph with a fake model and fake MCP server, MCP read-only
behaviour, VSL gates and workflow, approvals (binding, single use, expiry), recovery actions and runner, evidence policy and integrity
digest, idempotency claims, per-PreNode verdicts, expiry presentation, the governance catalogue (docs drift test), the 22 acceptance
scenarios T-01..T-22 (`tests/acceptance_map.py`), the dashboards (Streamlit `AppTest`), start-up validation, the launcher, the SQL
wipe-guard, and the database-identity script.

## Browser verification

| Check | Kind | Status |
|---|---|---|
| Governance panel in all supported states (verified success, pending, failed verification with escalated plan, unknown, expired-while-stored-pending, empty, partial, blocked duplicate) | **Stubbed**, headless Streamlit `AppTest`, real `dashboard/ops_app.py` against canned `[STUB]` API data (`tests/test_stub_governance_panel.py`, 9 tests) | Done, passing |
| Same, in a real browser (`python -m scripts.stub_governance_demo` then http://127.0.0.1:8601) | **Stubbed**, real browser | **Not done**: needs the user to start it (the workspace cannot host a server the user's browser can reach) |
| Live dashboard governance panel | **Live** | **Not possible with `DB_NAME=dataops_sim`**: recovery is guarded off (503 `recovery_unavailable`); not changed |

The stub scenarios use synthetic ledger outcomes through the real `build_governance_view`; they demonstrate the derivation, not any
real recovery.

## Outstanding integration checks (each needs your approval)

1. **SQL Server integration suite** (destructive: wipes and reseeds its target). Create an empty disposable database yourself, for
   example `dataops_integ_scratch_test`, confirm it with `python -m scripts.check_database_identity`, then:
   ```powershell
   $env:RUN_SQLSERVER_TESTS="1"; $env:DB_SERVER="localhost\SQLEXPRESS03"; $env:TEST_DB_NAME="dataops_integ_scratch_test"
   $env:SQLSERVER_TESTS_CONFIRM_WIPE="dataops_integ_scratch_test"
   python -m pytest tests/integration/test_recovery_sqlserver.py -q
   ```
   Never point this at `dataops_sim` or `dataops_demo_test` (both refused).
2. **Live model (T-20)**: `python -m scripts.openai_smoke --yes-call-openai` sends one fixed prompt, no project data; billable.
3. **Windows full suite**: `python -m pytest -q` in your venv.
4. **Real-browser stub check** as above.
5. **Docker build and CI run**: not executed.

The Windows SQL Server identity diagnostic **passed** on the user's machine (configured and actual database `dataops_sim`; Windows
authentication; not in `RECOVERY_TARGET_ALLOWLIST`), as reported by the user on 2026-11-02.
