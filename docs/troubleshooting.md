# Troubleshooting

Commands are for Windows PowerShell from the repository root with the venv active.

## Start-up and environment

| Symptom | Cause and fix |
|---|---|
| `[Errno 10048]`, port 8000 in use | Find the owner: `Get-NetTCPConnection -LocalPort 8000 -State Listen \| Select OwningProcess`, then `Get-Process -Id <pid>`. Stop it yourself if it is yours, or run the launcher with `--api-port 8010` (likewise `--ops-port`, `--ledger-port`). The launcher never kills anything |
| `ModuleNotFoundError: dashboard / app / scripts` | Run from the project root with the venv active and use `python -m ...` |
| `pip` fails installing `vsl-langgraph` | The VSL packages install from pinned GitHub commits; check Git and GitHub access |
| "The SQL Server Python driver (pyodbc / ODBC runtime) is not available" | Install Microsoft ODBC Driver 17/18 and match `DB_DRIVER`; see [windows-setup.md](windows-setup.md). Run `python -m scripts.check_prereqs` |
| "Named Pipes Provider" or login errors | Wrong `DB_SERVER`, stopped SQL Server service, or wrong driver/auth mode |
| `.env` changes ignored | Restart the process; run from the project root. Env vars in the shell override `.env` |
| `dev_stack` refuses to run | `APP_ENV=production` is set; it is development-only |
| Production start-up refuses | `APP_ENV=production` needs explicit `DATABASE_URL`, `APPROVAL_STORE_PATH`, `GOVERNANCE_LEDGER_PATH`, `DB_NAME` and rejects `dev_static` |

## Authentication

| Symptom | Cause and fix |
|---|---|
| `401 unauthenticated` from the API or a dashboard | No token, or `APPROVAL_AUTH_MODE=disabled` (default). Start with `scripts.dev_stack`, not separate `streamlit run` windows |
| Which token does the API accept? | `$env:CHECK_TOKEN = $tok; python -m scripts.check_auth_config; Remove-Item Env:CHECK_TOKEN` (prints subject and roles only, never the token) |
| `403 forbidden` on approve | Role lacks `approval:decide` or the per-action approver permission |
| `429 rate_limited` | Repeated failed authentication; wait for the `Retry-After` period |

## Recovery and governance

| Symptom | Cause and fix |
|---|---|
| Human Review: "switched off by a safety guard", `503 recovery_unavailable` | Expected with `DB_NAME=dataops_sim`. The database must be in-memory or a `_test` database listed in `RECOVERY_TARGET_ALLOWLIST`. See [installation-and-configuration.md](installation-and-configuration.md#enabling-recovery-safely). Do not loosen the guard |
| `UnsafeRecoveryTarget` | Database not allow-listed, not `*_test`, is `dataops_sim` or a system database, or the connection string has no/conflicting `DATABASE=` |
| Request shows **EXPIRED** but stored status says `pending` | By design: the stored status flips only when a decision is attempted. Expired requests cannot be decided; request a new recovery |
| Approval refused `approval_already_used` | The approval was spent; a new request needs a new approval |
| Resume: `evidence_stale` / `evidence_integrity_failed` / `evidence_metadata_missing` | Evidence older than 60 minutes, changed after request, or the request predates the evidence policy. Re-run the investigation and request again; the approval stays unspent |
| Outcome `verification_failed` | The action ran but the postcondition was not met; escalated, investigate by hand |
| Outcome `uncertain` | The result is unknown; an open claim blocks the same operation until a person reconciles it (service method only). Check the target system first |
| Panel shows `NOT RECORDED (this is not a pass)` | The ledger holds no record for that stage; it is never treated as a pass |
| `escalation_record_failed` | The ledger could not record the escalation; the status is still not success. Fix the ledger and review manually |
| Ledger integrity failure | The hash chain does not verify; recovery is blocked (fail closed). Inspect `data/governance_ledger.jsonl`; do not edit it |

## Dashboard and data

| Symptom | Cause and fix |
|---|---|
| Explorer empty, hint to seed | Run `python -m scripts.db_seed_demo_incidents` |
| Seeder says there is no baseline | Run `python -m scripts.db_seed` first |
| Investigate shows "still running" | The dashboard waits up to 10 s; press **Refresh data** |
| Human Review empty or ledger shows 0 events | Correct on a fresh install; they fill only after the workflow runs |
| Stub dashboard will not start | A needed port (8600/8601) is busy or you chose 8000/8501/8502, which it refuses |

## Tests

| Symptom | Cause and fix |
|---|---|
| 44 skipped tests | SQL Server integration tests; skipped unless `RUN_SQLSERVER_TESTS=1`. A skip is not a pass |
| Integration suite refuses to run | It needs `SQLSERVER_TESTS_CONFIRM_WIPE=<exact db name>`, a `_test` database, and refuses `dataops_sim` and `dataops_demo_test` |
| Governance docs drift test fails | Run `python -m scripts.generate_governance_docs` after changing the catalogue, lifecycle or acceptance map |
| Full suite exceeds a time limit | Run it in file chunks: `ls tests/test_*.py tests/*/test_*.py \| sed -n 1,25p \| xargs python -m pytest -q` (bash) |
