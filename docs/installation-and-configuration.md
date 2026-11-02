# Installation and configuration

Commands are for Windows PowerShell from the repository root. They were not executed on Windows by the author of this
documentation pass (the workspace was Linux); the Python entry points they call were checked against the source.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dashboard.txt
Copy-Item .env.example .env
```

`requirements.txt` installs the VSL packages from pinned GitHub commits, so Git and GitHub access are required.
`requirements-dashboard.txt` (Streamlit, Plotly, pandas) is kept separate so the API/Docker image stays small.
`requirements-reader.txt` is a UTF-16 `pip freeze` snapshot, not an install entry point.

`.env` is git-ignored. Edit it yourself; do not commit, print or paste it.

## Configuration variables

Settings come from environment variables, optionally `.env` (`app/config.py`, `pydantic-settings`; unknown keys ignored).

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `development`, `test`, `production`. Production enables fail-closed start-up validation |
| `LOG_LEVEL` | `INFO` | |
| `API_HOST`, `API_PORT` | `127.0.0.1`, `8000` | Used by `python -m app.main` |
| `DB_SERVER` | `localhost` | `.env.example` suggests `localhost\SQLEXPRESS03` |
| `DB_NAME` | `dataops_sim` | Must end `_sim` or `_test` for init/reset scripts |
| `DB_AUTH_MODE` | `windows` | or `sql` with `DB_USER` / `DB_PASSWORD` (local `.env` only) |
| `DB_DRIVER` | `ODBC Driver 18 for SQL Server` | Must match an installed driver |
| `DB_ENCRYPT`, `DB_TRUST_SERVER_CERTIFICATE`, `DB_LOGIN_TIMEOUT_SECONDS` | `true`, `false`, `5` | Trust-server-certificate `true` is for local development only |
| `SIM_SEED`, `FRESHNESS_THRESHOLD_MINUTES`, `SIM_LOG_DIR` | `20260115`, `1500`, `logs` | Simulator |
| `INVESTIGATION_MODE` | `offline` | `live` calls OpenAI (billable, **not validated**) and needs `OPENAI_API_KEY` |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | none, `gpt-4o-mini` | Placeholders only in `.env.example` |
| `RECOVERY_TARGET_ALLOWLIST` | empty | Comma list of `db` or `server/db`; every entry must also end `_test`. Empty means no SQL target |
| `RECOVERY_AUTO_AUTHORIZE_LOW_RISK` | `false` | Opt-in policy authorisation of one bounded low-risk retry |
| `APPROVAL_AUTH_MODE` | `disabled` | `disabled` or `dev_static` (refused in production) |
| `APPROVAL_DEV_CREDENTIALS` | unset | JSON list of `{subject, token_sha256, roles}`; digests only |
| `APPROVAL_SIGNING_KEY` | unset | Seals approvals; unset means approvals unavailable |
| `DATABASE_URL`, `APPROVAL_STORE_PATH`, `GOVERNANCE_LEDGER_PATH` | `data/...` defaults | Operations store, approval store and ledger locations; required explicitly in production |
| `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_API_URL` | none | Optional ticketing |
| `DASHBOARD_API_BASE_URL`, `DASHBOARD_API_TOKEN`, `DASHBOARD_INVESTIGATE_TOKEN`, `LEDGER_EXPLORER_API_TOKEN`, `OPS_DASHBOARD_URL`, `LEDGER_EXPLORER_URL` | set by `dev_stack` | Dashboard wiring; see `.env.example` |
| `LEDGER_MAX_SCAN_LINES`, `LEDGER_MAX_SCAN_BYTES` | built-in | Ledger view limits |
| `RUN_SQLSERVER_TESTS`, `TEST_DB_NAME`, `SQLSERVER_TESTS_CONFIRM_WIPE`, `SQLSERVER_TESTS_ALLOW_CREATE_DB` | unset | SQL Server integration tests only; see [testing-and-verification.md](testing-and-verification.md) |

## Authentication modes

| Mode | How | Where |
|---|---|---|
| `disabled` (default) | `DenyAllAuthenticator`: nobody can approve; tokens rejected | `app/recovery/auth.py` |
| `dev_static` | Tokens hashed (SHA-256) in `APPROVAL_DEV_CREDENTIALS`; roles per subject; compared in constant time | Local development only. `scripts.dev_stack` generates tokens into `.dev/dev_auth.json` (git-ignored) |
| Production | None provided. With `APP_ENV=production` the process refuses `dev_static` and refuses to start unless `DATABASE_URL`, `APPROVAL_STORE_PATH`, `GOVERNANCE_LEDGER_PATH` and `DB_NAME` are set | `app/core/startup.py` |

Check that a token matches the API's configured credentials without printing secrets (same PowerShell window as the API):
`$env:CHECK_TOKEN = $viewer; python -m scripts.check_auth_config; Remove-Item Env:CHECK_TOKEN`.

## Database setup and separation

```powershell
python -m scripts.check_prereqs                 # read-only diagnosis of driver, connection and auth
python -m scripts.db_init                       # create database and tables (never deletes)
python -m scripts.db_seed                       # deterministic baseline, idempotent
python -m scripts.db_verify                     # tables and baseline counts
python -m scripts.db_seed_demo_incidents --dry-run   # optional synthetic incidents; --dry-run changes nothing
python -m scripts.check_database_identity       # read-only: SELECT DB_NAME(), SELECT @@SERVERNAME; exit 0 ok, 1 mismatch, 2 config/connection
```

Separation by design:

| Store | Purpose | Write access |
|---|---|---|
| SQL Server `dataops_sim` | Simulator (the application's configured database) | Setup scripts and simulator; **never a recovery target** |
| A separate `*_test` database (for example `dataops_demo_test`) | Optional recovery demo target | Recovery, only if listed in `RECOVERY_TARGET_ALLOWLIST` |
| A different, disposable `*_test` database | SQL Server integration tests | **Wiped and reseeded** by the test fixture |
| `data/*.sqlite3`, `data/governance_ledger.jsonl` | Workflow, approvals, ledger | The application |

Safety restrictions enforced in code: recovery (`app/recovery/actions.py`, `UnsafeRecoveryTarget`) requires an in-memory fake or a
name ending `_test` that is in `RECOVERY_TARGET_ALLOWLIST`, and checks that the live connection really is that database
(`DB_NAME()`); a missing or conflicting `DATABASE=` is refused. The reset and integration-test guards (`app/db/safety.py`) refuse
`dataops_sim`, `dataops_ecommerce_demo`, `master`, `model`, `msdb` and `tempdb` by name, and the wipe also needs
`SQLSERVER_TESTS_CONFIRM_WIPE`. Do not loosen these.

## Enabling recovery safely

With `DB_NAME=dataops_sim` recovery is intentionally unavailable (HTTP 503 `recovery_unavailable`). To exercise it you must use
a separate `_test` database and allow-list it, for example in one PowerShell window only (this does not change `.env`):

```powershell
$env:DB_NAME = "dataops_demo_test"
$env:RECOVERY_TARGET_ALLOWLIST = "dataops_demo_test"
python -m scripts.db_init; python -m scripts.db_seed; python -m scripts.db_seed_demo_incidents; python -m scripts.db_verify
python -m scripts.scenario activate duplicate-records     # injects a simulated fault so a recovery can be proposed
python -m scripts.check_database_identity                 # confirm what the API will connect to
.\.venv\Scripts\python.exe -m scripts.dev_stack --show-approver-token --open
```

Recovery then changes rows in that test database. **This flow has not been run by the author** against SQL Server. Undo the
injected fault with `python -m scripts.scenario restore-all`. Do not use `dataops_demo_test` as a SQL integration-test target.

## Docker and CI (present, not run by the author)

`Dockerfile` (single Uvicorn worker, because SQLite and the ledger are single-writer), `docker-compose.yml` and
`.github/workflows/ci.yml` (Python 3.10 and 3.12, no secrets, SQL Server tests skipped) exist. None was built or observed running.
