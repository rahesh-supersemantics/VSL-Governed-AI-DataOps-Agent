# Operations prepared but NOT executed (each needs your explicit approval)

Last updated: 2026-11-02. Nothing below has been run unless it says "result". The consolidated status of every verification item is in [release-readiness.md](release-readiness.md); the file lists in section 3 are a snapshot from earlier in the day, so use `git status` for the current set.

## 0. Read-only database identity check - approved by you, but it could not run where I work

- Command (run on your Windows machine, repo root, venv active): `python -m scripts.check_database_identity`
- It runs only `SELECT DB_NAME()` and `SELECT @@SERVERNAME` through the application's `make_engine`; it prints no connection string
  or credential and changes nothing. Exit 0 match, 1 mismatch or no identity, 2 configuration or connection failure.
- **Result so far (exit 2):** configured `server=localhost\SQLEXPRESS03  database=dataops_sim  auth=windows`; then
  "The SQL Server Python driver (pyodbc / ODBC runtime) is not available." My workspace is Linux, has no ODBC driver and cannot reach
  your Windows-authenticated SQL Server, so the database was never contacted. This is not a pass and not a database finding.
- Note: the application's configured database is `dataops_sim` (the simulator), not `dataops_demo_test`. Expect OK for
  `dataops_sim` and the informational allowlist line to say it is NOT in `RECOVERY_TARGET_ALLOWLIST`; that is correct, since the
  simulator is never a recovery target. To check the demo database instead, set `DB_NAME=dataops_demo_test` for that one command
  (`$env:DB_NAME="dataops_demo_test"; python -m scripts.check_database_identity`); I have not changed `.env`.

## 1. SQL Server integration tests (`tests/integration/*_sqlserver.py`) - DESTRUCTIVE to the target database

Findings from reading the fixture (`tests/integration/conftest.py`):

- **Target selection.** The target is the environment variable `TEST_DB_NAME` (default `dataops_sim_test`). The fixture builds its
  own `Settings(db_name=TEST_DB, ...)`; it does not read `DB_NAME` from `.env`, so it cannot fall back to `dataops_sim`.
- **What writes.** The `svc` fixture runs `repo.wipe()` then `seed()` before **every** test; `metadata.create_all` runs once per
  session; `test_recovery_sqlserver.py` also removes an injected condition and appends a pipeline run. The approval store and the
  ledger go to pytest temp directories, not SQL Server. If the database does not exist the suite stops unless
  `SQLSERVER_TESTS_ALLOW_CREATE_DB=1` is set (that creates a database and needs an elevated login).
- **Guards that run first:** `assert_safe_integration_target` (name must end `_test`; `dataops_sim`, `dataops_ecommerce_demo`,
  `master`, `model`, `msdb`, `tempdb` refused by name; local server only; not production), an independent `SELECT DB_NAME()` that
  must equal the configured name, and `assert_safe_reset_target`.
- **Gap I found and closed (offline-tested, in `tests/test_integration_wipe_guard.py`):** `dataops_demo_test` ends in `_test` and is
  not in the protected list, so `TEST_DB_NAME=dataops_demo_test` would have been wiped. The suite now also requires
  `SQLSERVER_TESTS_CONFIRM_WIPE` to equal the target database's exact name, and refuses `dataops_demo_test` outright.
- **Not verifiable without SQL Server:** that no other module opens a different connection mid-suite. I read the integration tests:
  the only other `make_engine` use points at a deliberately nonexistent database for a failure test.

Proposed isolation procedure (nothing created yet):

1. A DBA (or you, with an elevated login) creates an empty disposable database, e.g. `dataops_integ_scratch_test`
   (`CREATE DATABASE [dataops_integ_scratch_test]`). I have not created it.
2. Check what the suite will connect to before anything is wiped (read-only):
   `$env:DB_NAME="dataops_integ_scratch_test"; python -m scripts.check_database_identity`
3. Run in PowerShell, one file first:
   `$env:RUN_SQLSERVER_TESTS="1"; $env:DB_SERVER="localhost\SQLEXPRESS03"; $env:TEST_DB_NAME="dataops_integ_scratch_test"; $env:SQLSERVER_TESTS_CONFIRM_WIPE="dataops_integ_scratch_test"; python -m pytest tests/integration/test_recovery_sqlserver.py -q`
4. Afterwards confirm `dataops_sim` and `dataops_demo_test` are untouched (compare row counts you note beforehand).

Backup or copy: I do not know whether a backup exists. Approval needed: "create `<name>` and run the SQL Server integration suite
against it, wipe and reseed allowed", with the exact database name.

## 2. Live model test (T-20) - external call, billable

- Provider and model: OpenAI, model from `OPENAI_MODEL` (the code default is `gpt-4o-mini`; your `.env` may differ; I have not read it).
  The key is never printed.
- **Smoke command** `python -m scripts.openai_smoke --yes-call-openai` sends one fixed request: system text
  `Reply with the JSON object {"ok": true}.` and user content `{"instruction": "Reply with ok=true."}` with a strict JSON schema.
  **No incident text, evidence or project data is sent.** Expected response: `{"ok": true}`. It writes no local or external state
  (prints usage). Cost: a few dozen tokens, which I expect to be a fraction of a cent, but I have not verified current rates.
  Without the flag it prints key state and model and makes no request (tested offline).
- **Full investigation** `python -m scripts.investigate "<incident>" --max-rounds 2 --max-tool-calls 6` is different: it sends the
  incident text (truncated to 2,000 characters) and the outputs of read-only diagnostic tools (counts, run statuses, data-quality
  findings) to the provider. It reads the database and writes nothing. Not part of the smoke test.
- Offline mode (`--offline`) is proven by test not to open any network connection or build the live client.
- Approval needed: "run the smoke test" (no project data) and/or "run an investigation with this incident text".

## 3. Publishing (nothing committed or pushed by me)

- Branch `main`; remote `origin` is `https://github.com/rahesh-supersemantics/AI-DataOps-Agent.git` (fetch and push). I did not
  contact GitHub, so write access is unverified.
- Proposed named paths to stage (review each; I have not staged anything):
  - Modified (23): `.env.example`, `app/agent/models.py`, `app/agent/report.py`, `app/api/v1/routes/recoveries.py`,
    `app/core/logging.py`, `app/db/safety.py`, `app/governance/policy.py`, `app/recovery/actions.py`, `approvals.py`,
    `governance_catalogue.py`, `lifecycle.py`, `runner.py`, `workflow.py`, `app/services/recoveries.py`,
    `dashboard/ops_app.py`, `ops_client.py`, `ops_data.py`, `docs/governance-audit-and-gap-analysis.md`,
    `tests/integration/conftest.py`, `tests/recovery_support.py`, `tests/test_governance_acceptance.py`,
    `tests/test_governance_catalogue.py`, `tests/test_governance_static.py`.
  - New: `app/recovery/governance_view.py`, `app/recovery/prenode_verdicts.py`, `scripts/check_database_identity.py`,
    `scripts/generate_governance_docs.py`, `tests/acceptance_map.py`, `tests/test_check_database_identity.py`,
    `tests/test_governance_demo.py`, `test_governance_evidence.py`, `test_governance_idempotency.py`,
    `test_governance_integrity_and_review.py`, `test_governance_lifecycle.py`, `test_governance_offline_mode.py`,
    `test_governance_prenodes.py`, `test_governance_view_api.py`, `test_integration_wipe_guard.py`, and the docs
    `governance-acceptance-tests.md`, `governance-architecture.md`, `governance-catalogue.md`, `governance-demo.md`,
    `governance-evidence-policy.md`, `pending-approvals.md`.
- Secret-pattern scan of these files (patterns for `sk-` keys, AWS keys, private-key blocks, and quoted password/secret/api-key
  assignments): one hit, `tests/test_check_database_identity.py` line 84, a literal dummy password used to prove the redactor hides
  passwords. No real credential found. `.env`, `.venv`, `data/*.sqlite3` and `logs/` are git-ignored (`git check-ignore`); no tracked
  `.env`, database, log or key file.
- Before pushing: confirm owner and repository, confirm your GitHub account has write access, stage named files only, and write
  or approve the commit message.

## 4. Browser and Windows verification

**Done (2026-11-02), and what it does and does not prove**
- Your running stack (restarted 15:47) serves `/api/v1/recoveries/{id}/governance` and the dashboard has no import error. The governance
  panel itself could not render there: the API is configured for `dataops_sim`, so recovery is switched off by the `_test` guard
  (HTTP 503 `recovery_unavailable`). That is the guard working; I did not change it.
- **Stubbed** panel verification (not live, not end to end): `tests/test_stub_governance_panel.py` drives the real `dashboard/ops_app.py`
  with Streamlit AppTest against canned `[STUB]` responses. It checks recorded vs implied verdicts, failed and unknown verdicts,
  `NOT RECORDED (this is not a pass)`, NOT authorised / Not attempted, an expired request (no decision control offered, while a
  pending one has it), and empty and partial responses (no success shown). This is headless, not a browser.

**Still pending: a real browser look at the stub (you start it; I then view it)**
- Command, from the repo root with your venv: `.\.venv\Scripts\python.exe -m scripts.stub_governance_demo`
- It starts a read-only stub API on 8600 and the real dashboard code on 8601. It touches no database, store, ledger, `.env` or model,
  refuses ports 8000/8501/8502 and any busy port (it stops nothing), and refuses every write with 405. Ctrl+C stops it.
- My workspace cannot start processes on your computer or be reached by your browser, which is why you run it. Tell me when it is up
  and I will open http://127.0.0.1:8601 in the browser pane (Recovery & approvals, incidents `[STUB] ...`) and report.

**Windows:** the offline suite ran in a Linux workspace. Please run `python -m pytest -q` from the repo root in your venv and send the summary.
