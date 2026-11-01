# Ecommerce MCP server and agent (Stage 4 — scratch implementation, not yet run live)

Status: implemented and unit-tested in a scratch copy only. **No live SQL Server validation has been done.**

## Architecture

```
agent (profile: ECOMMERCE_PROFILE)  --stdio-->  python -m app.mcp_server.ecommerce
                                                   EcommerceDiagnosticsService
                                                   verified_ecommerce_connection  (per tool call)
                                                   SQL Server, database dataops_ecommerce_demo, Windows auth
```

The simulation server (`python -m app.mcp_server`, 11 tools, `dataops_sim`) is unchanged. The Ecommerce server is a separate
package with its own tools and a fixed target (`app/ecommerce/target.py`); it ignores `DB_NAME`.

## Connection verification (fail closed, on every tool call)

1. Windows integrated authentication is required, and the SQL Server must be local (`assert_local_target`).
2. The engine is built for `dataops_ecommerce_demo` (a constant, not a setting). A 30 s query timeout is applied and read back;
   if it cannot be applied the connection is refused. It is inside the agent's 60 s tool timeout.
3. One identity query: `DB_NAME()` must equal the target; `SUSER_SNAME()` must equal `ORIGINAL_LOGIN()` (no impersonation);
   the login must be `<this machine>\dq_reader`.
4. The existing `verify_read_only_principal` runs on the same connection (no write-capable role or permission, no writable
   table, definite answers only) and its principal must match step 3.
5. The account must not be able to access `dataops_sim`.
6. Only then are diagnostic queries run, on that same connection. Any failure returns an `ECOM-GUARD-REFUSED` error result;
   there is no override and no fallback to another identity.

The database principal's permissions are the real protection. Every statement also passes a gate (`check_statement`) that allows
only three fixed verification statements and single `SELECT COUNT_BIG/MIN/MAX` statements without literals, comments or write
keywords. That gate is defence in depth, not the boundary. The login name is never returned (only a 12-character fingerprint).

## Time limits (Stage 4.1)

A statement timeout alone does not bound a tool call: the full sweep runs about 58 statements one after another, and the
MCP server runs the work in a thread that the client cannot cancel. So every tool call has three layers:

1. **Per statement:** 30 s (`QUERY_TIMEOUT_SECONDS`), applied and read back on the driver connection.
2. **Per call (budget):** 45 s (`TOOL_BUDGET_SECONDS`) from the moment the call starts, covering connecting, verification and
   every statement. Before each statement the guard refuses to start it once the deadline has passed (`TimeBudgetExceeded`) and
   lowers the statement timeout to the time that is left (never above 30 s; if the lower value cannot be applied the statement
   is refused). Checks that did not finish return `ECOM-TIMEOUT` (status `error`); `run_ecommerce_diagnostics` keeps the
   checks that finished and reports the rest as not completed.
3. **Server hard limit:** 51 s (`TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS`). If a worker is stuck anyway the server answers
   with an explicit `ECOM-TIMEOUT` error and abandons the worker.

The agent's default tool timeout is 60 s, so the server answers first. If the agent timeout is set below 55 s
(`MIN_AGENT_TOOL_TIMEOUT_SECONDS`) the agent can time out before the server answers; the report then lists a limitation, and
the agent still treats a timeout as an error, never as a clean result.

A timeout, partial sweep or refused connection is always `error` / `execution: incomplete`; it is never `ok`. Remaining limits:
the abandoned worker can still finish its current read-only statement (at most 30 s, usually less, and its result is discarded) and keeps its worker slot until it ends;
the budget is cooperative between statements and relies on the driver honouring the query timeout (not yet verified live);
a check cut off part-way returns no partial counts for that check; the checks of one sweep are not a single database snapshot
(they run one after another, so counts are taken at slightly different moments).

### Timeout table (Stage 4.4)

Every layer below is a constant in `app/ecommerce/target.py` unless the source column says otherwise. A test
(`tests/test_ecommerce_hardening.py`) fails if this table and the code disagree.

| Layer | Seconds | What it bounds | Source |
|---|---|---|---|
| `DB_LOGIN_TIMEOUT_SECONDS` default | 5 | ODBC login (pyodbc `timeout` connect argument) | `app/config.py` (shared Settings; allows 1..60 for the simulation) |
| `MAX_LOGIN_TIMEOUT_SECONDS` | 15 | Ecommerce ceiling for the login; also at most a third of the time left in the call | `target.py`, enforced by `guard._check_login_timeout` |
| `QUERY_TIMEOUT_SECONDS` | 30 | one statement (driver `Connection.timeout`), lowered to the time left in the budget | `target.py`, `guard._enforce_deadline` |
| `TOOL_BUDGET_SECONDS` | 45 | the whole tool call: login, verification and every statement; cooperative, between statements | `target.py`, `service._deadline` |
| `HARD_LIMIT_MARGIN_SECONDS` | 6 | room between the budget and the server's backstop; `budget + margin <= hard limit` is validated at start-up | `target.py`, `validate_timeouts` |
| server hard limit | 51 | how long the MCP caller waits (`TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS`); it does NOT stop the worker | `server.create_ecommerce_server` |
| `MIN_AGENT_TOOL_TIMEOUT_SECONDS` | 55 | an agent tool timeout below this can fire before the server answers; a limitation is reported | `target.py`, `profiles.py` |
| agent tool timeout default | 60 | the agent waits this long per call (`anyio.fail_after`); the MCP read timeout is this plus 5 s | `app/agent/config.py`, `mcp_client.py` |
| `MAX_IN_FLIGHT_WORKERS` | 4 | diagnostic workers running at once (a slot is freed only when a worker really ends) | `target.py`, `server.WorkerGate` |

Login timeout rule (A1): before any engine is built the guard checks `DB_LOGIN_TIMEOUT_SECONDS` against the time left in
the call. A value that is missing, not a finite number, below 1 s, above 15 s, or above a third of the time left is
**refused** (`ECOM-GUARD-REFUSED`, reason `login_timeout_invalid`, "no connection was attempted"); it is never silently
clamped. If a login fails after the whole budget was used up, the result is `ECOM-TIMEOUT`, not a generic connection error.
If a login succeeds but leaves no time, no SQL (not even the identity query) is sent.

Not proven by unit tests: that pyodbc really honours the login timeout and `Connection.timeout`, including values lowered
mid-connection. That needs the live validation (see "Live validation runbook").

### Stage 4.3 additions

- **Configuration is validated at start-up.** `create_ecommerce_server` fails closed (`ValueError`, the server does not start)
  unless `service budget + safety margin <= hard limit`. The margin (`HARD_LIMIT_MARGIN_SECONDS`, 6 s) is the room the service
  needs to notice its deadline, refuse the next statement and close the connection before the server's backstop fires. The
  defaults (45 + 6 = 51) sit exactly on the boundary. The budget alone must also be a positive, finite number.
- **In-flight workers are capped** (`MAX_IN_FLIGHT_WORKERS`, 4). A slot is taken before a worker thread starts and is given
  back **by the worker itself when it really ends**, not when the caller stops waiting. When all slots are busy the call is
  refused with `ECOM-BUSY` (status `error`, nothing started, no connection opened). Python cannot cancel a thread: after the
  51 s answer the worker is NOT cancelled; the timeout result says so (`worker_cancelled: false`). It can only finish the
  read-only statement it is in, after which the budget refuses further statements.
- **A partly assessed check is an incomplete run.** A result carrying `ECOM-PARTIAL` (even with status `warning` or
  `issue_detected`) is listed under `ecommerce_summary.checks_partially_completed`, has `execution: partial` and
  `rules_not_assessed`, and makes `execution: incomplete`. Findings from the rules that ran are kept; unassessed rules are
  never passes. The agent reports `inconclusive` (exit code 1) for a partial check without a confirmed issue; with a confirmed
  issue it reports `issue_identified` and states in the summary and limitations that the run is incomplete.
- **The headline of a non-clean run is written by code.** For `failed`, `inconclusive` and incomplete runs the agent report's
  summary is built from the execution record; the model's summary is not used, and model hypotheses, impact, explanations and
  next steps that read as a clean result are discarded. Clean, complete runs keep the model's summary.

`run_ecommerce_diagnostics` reads the rule file and the catalogue once per sweep and shares them between its seven checks
(individual tools read them once per call). Table row counts are reused between the target check and the null profile.

## Tools (all read-only, counts only)

| Tool | Input | Result |
|---|---|---|
| `check_ecommerce_target` | none | verification status, table presence, row counts |
| `check_referential_integrity` | optional `relationship` | orphan counts: orders→customers, items→orders/products/sellers, payments→orders, reviews→orders |
| `check_delivery_completeness` | none | delivered orders missing customer / carrier delivery date |
| `check_delivery_timeline` | none | delivery before purchase (integrity); delivery after estimate (business signal) |
| `check_missing_values` | optional `table` | null key columns (violations) and per-column null counts (observations) |
| `check_duplicate_keys` | none | duplicate groups per candidate key; `review_id` duplicates are a **pending investigation** (OLIST-009 stays disabled) |
| `profile_ecommerce_timestamps` | none | min/max of timestamp columns; no freshness verdict (historical data) |
| `run_ecommerce_diagnostics` | none | all of the above on one connection plus `ecommerce_summary` |

Results use the common `DiagnosticResult` shape. `affected_records` hold table tags only. `error` means the check could not
run (never "zero violations"). `ecommerce_summary` reports `target_verified`, `execution` (complete/incomplete) and
passed / failed / flagged / pending / skipped / unassessed counts per check and per rule.

## Duplicate-key semantics

`check_duplicate_keys` counts **duplicate groups**: distinct, *complete* key values that occur more than once. A row whose key
contains a NULL in any column is excluded, for single-column and composite keys alike, so NULL keys are never reported as
duplicates; NULL keys appear in `check_missing_values` instead. Each rule also reports `rows_in_duplicate_groups` (all rows
that carry a repeated key) and `surplus_rows` (rows beyond the first of each group). The rule's `violations` and the summary
text are group counts. This applies to the Ecommerce module only; the shared rule engine's duplicate query is unchanged.
`review_id` duplicates (OLIST-009) are still reported as a pending investigation and the rule stays disabled in the YAML.

## Business rules (Stage 4.4 review)

Labels: **Source** = read from the code/config in this repository; **Assumption** = believed but not verified against the live
database; **Historical** = a figure reported by an earlier run, never used as an expected value; **Pending** = needs a decision.
No rule was executed against the live database in Stage 4.4, so every "current result" is *not available*.

| Rule | Business meaning | Columns | NULL handling | SQL (bound value `'delivered'`) | Status / severity | Issue or unassessed |
|---|---|---|---|---|---|---|
| OLIST-005 | A delivered order must have a customer delivery date | `order_status`, `order_delivered_customer_date` | NULL date is the violation | `COUNT_BIG(*) WHERE order_delivered_customer_date IS NULL AND order_status = :v` | failed / high when > 0 | confirmed issue when counted; unassessed if the statement errors |
| OLIST-006 | A delivered order must have a carrier delivery date | `order_status`, `order_delivered_carrier_date` | NULL date is the violation | same shape on the carrier date | failed / medium when > 0 | as above |
| OLIST-007 | Delivery cannot precede purchase | `order_delivered_customer_date`, `order_purchase_timestamp` | rows with a NULL on either side are not judged | `... WHERE both IS NOT NULL AND NOT (delivered >= purchase)` | failed / high when > 0 | as above; undelivered orders are not judged |
| OLIST-008 | Delivered later than estimated (service level) | `order_delivered_customer_date`, `order_estimated_delivery_date`, `order_status` | NULL on either side is not judged | `... AND NOT (delivered <= estimated) AND order_status = :v` | flagged / low (business signal, never an integrity failure) | **Pending** (see below) |
| OLIST-009 | `review_id` unique | `review_id` | NULL keys excluded (OLIST-010 reports them) | grouped count, disabled | **disabled**, reported as pending investigation | not an issue, not a pass: unassessed by design |

Source facts: the importer stores these columns as `DateTime`; the Ecommerce rule file is the unchanged shared YAML. Assumptions
(live-unverified): `order_status = 'delivered'` matches the stored spelling under the database collation (the test fixture
also covers an upper-case value); the estimate column carries a midnight time. Historical (not expected values): 8 delivered
orders without a customer date, 2 without a carrier date, 0 deliveries before purchase, 7,826 deliveries after the estimate;
789 duplicate `review_id` groups with 814 surplus rows.

### OLIST-008: the business question and the two readings (decision PENDING; rule unchanged)

*Question for the business owner:* "Is an order late if it arrives **after the estimated moment**, or only if it arrives on a
**later calendar day** than the estimated day?"

| | Full timestamp (current) | Calendar date |
|---|---|---|
| SQL | `NOT (delivered <= estimated)` | `NOT (CAST(delivered AS date) <= CAST(estimated AS date))` |
| Late means | any time after the estimate | a later day than the estimate |
| Estimate `2020-01-10 00:00`, delivered `2020-01-10 14:00` | late | **not late** |
| Delivered `2020-01-10 00:00` | not late | not late |
| Delivered `2020-01-09 23:00` | not late | not late |
| Effect | flags every same-day delivery when the estimate is stored at midnight | flags fewer orders: it removes exactly the same-day-after-midnight group |

If estimates are stored at midnight (assumption), the timestamp reading treats "arrives any time on the promised day" as late,
which most service-level definitions would not. That is the evidence for preferring the calendar-date reading, but only the
business owner can decide, and the size of the difference is unknown until the live data is counted (the 7,826 figure is
historical and is by timestamp). The decision stays **pending**; the rule and the shared YAML are unchanged. Tests needed if
the owner chooses calendar date (already present for both readings on synthetic rows: same day after midnight, next day,
exactly the estimate, early, undelivered, NULL estimate): add the new rule's expected counts, a boundary at 23:59:59 vs
00:00:00 of the next day, and a time-zone note if timestamps are not stored in one zone. Read-only comparison query for the
owner (aggregate counts only; run as the reader account, not through the server):

```sql
SELECT COUNT_BIG(*) AS delivered_with_both_dates,
       SUM(CASE WHEN order_delivered_customer_date > order_estimated_delivery_date THEN 1 ELSE 0 END) AS late_by_timestamp,
       SUM(CASE WHEN CAST(order_delivered_customer_date AS date) > CAST(order_estimated_delivery_date AS date) THEN 1 ELSE 0 END) AS late_by_calendar_date,
       SUM(CASE WHEN order_delivered_customer_date > order_estimated_delivery_date
                 AND CAST(order_delivered_customer_date AS date) <= CAST(order_estimated_delivery_date AS date) THEN 1 ELSE 0 END) AS same_day_after_estimate,
       SUM(CASE WHEN CAST(order_estimated_delivery_date AS time) <> '00:00:00' THEN 1 ELSE 0 END) AS estimates_with_a_time_of_day
FROM dbo.ecom_orders
WHERE order_status = 'delivered' AND order_delivered_customer_date IS NOT NULL AND order_estimated_delivery_date IS NOT NULL;
```

### OLIST-009 stays disabled

Not enabled, YAML unchanged, nothing deleted or merged. The tools report it only as a "pending investigation" count and never
expose review ids or text. Possible explanations to test with aggregate evidence before any enablement decision: the same
review re-exported on several rows (different `order_id`), an import that appended instead of replacing, or a source where
`review_id` is unique only together with `order_id`. Evidence needed: how many duplicate groups share one `order_id`, how
many differ only in a timestamp column, and whether `(review_id, order_id)` is unique.

### Report semantics (verified by tests in this repository)

`ECOM-PARTIAL`, `ECOM-TIMEOUT` and `ECOM-BUSY` stay distinct codes in the evidence. A confirmed issue inside a partial check
stays visible (`issue_identified`) while the headline and limitations say the run is incomplete; a partial warning or a timeout
makes the run `inconclusive`.

**Code-controlled headline is opt-in (Stage 4.5A).** The rule that replaces the model's headline (and drops model-written
hypotheses, root cause and steps) on a non-clean run applies only when the profile sets `code_controlled_summary=True`, which
only `ECOMMERCE_PROFILE` does. `finalize_report(..., code_controlled=False)` is the default and reproduces the pre-Stage-4
behaviour of the simulation agent exactly (checked against the original function on 84 scenarios, 0 differences;
`tests/test_agent_report_compat.py`). The `claims_clean` text filter is a second line of defence, not a proof: the status and
exit code come from tool evidence, never from free text. Whether to extend the code-controlled headline to the default
simulation profile is a separate decision for you (see the Stage 4.6 report); nothing changes for it until you decide.


## Live validation runbook (Windows, run by you, as the reader account)

Stage 4.4 could not do this itself: the assistant's shell is a Linux VM with no ODBC driver and no Windows integrated
authentication, so it cannot connect as `<machine>\dq_reader`, and no other account may be used. **No live validation has been
run; nothing below has been verified live.**

Prerequisites: SQL Server `RaheshLaptop\SQLEXPRESS03` running; `ODBC Driver 18 for SQL Server`; a Python 3.10+ environment with
`requirements-reader.txt` installed; a validation checkout that contains the proposed changes (apply
`stage44_proposed.diff` to a copy you choose, NOT the project you rely on, e.g. with `patch -p1 < stage44_proposed.diff`); a
`.env` there with `DB_AUTH_MODE=windows` and `DB_SERVER=localhost\SQLEXPRESS03` and no password (`DB_NAME` is ignored by the
Ecommerce path); `allow_unverified_read_only` must stay false; the reader account must be able to read the checkout and the
Python environment.

### Launcher and environment: the server runs as a child process (read before steps 4 and 5)

`python -m scripts.ecommerce_check --verify` and `python -m scripts.ecommerce_live_validate` do not connect to SQL Server
themselves. Each starts the Ecommerce MCP server as a **child process** (`python -m app.mcp_server.ecommerce`, working
directory = the checkout root) and talks to it over stdio. The MCP client library hands a child process only a short
allow-list of variables (on Windows: `PATH`, `PATHEXT`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `USERNAME`, `USERPROFILE`, `SYSTEMROOT`
and a few similar ones). **Variables you `set` in the shell, such as `DB_SERVER`, `DB_DRIVER`, `DB_ENCRYPT`,
`DB_TRUST_SERVER_CERTIFICATE` and `DB_LOGIN_TIMEOUT_SECONDS`, are not passed on.** The server reads its configuration from a
`.env` file in the checkout root; with no `.env` it falls back to its defaults (`DB_SERVER=localhost`, which is the default
instance and not `SQLEXPRESS03`; encryption on; server certificate not trusted).

*Symptom:* the identity is correct (`whoami` prints `<machine>\dq_reader`, `sqlcmd` connects) but the tool returns
`ECOM-GUARD-REFUSED` with `connection_failed`. A longer `DB_LOGIN_TIMEOUT_SECONDS` does not help, because the child never sees it.

*Correct procedure, as the reader (`runas /user:%COMPUTERNAME%\dq_reader cmd`, never `/netonly`):*

1. `whoami` must print `<machine>\dq_reader`. If it does not, stop.
2. In the validation checkout only (never the original project), create a validation-only `.env` containing exactly these
   non-secret lines (Windows authentication; there is no password to store):
   ```bat
   cd /d <validation checkout>
   echo DB_AUTH_MODE=windows>.env
   echo DB_SERVER=localhost\SQLEXPRESS03>>.env
   echo DB_DRIVER=ODBC Driver 18 for SQL Server>>.env
   echo DB_ENCRYPT=true>>.env
   echo DB_TRUST_SERVER_CERTIFICATE=true>>.env
   type .env
   ```
   `type .env` must show those five lines and nothing else. Do not copy the original project's `.env`.
3. Run `python -m scripts.ecommerce_check --verify`, and only if it shows `status: ok` run
   `python -m scripts.ecommerce_live_validate --confirm-live-read-only --output reports\ecommerce_live_validation.json`.
4. Delete the validation `.env` when you have finished (`del .env`) and check it is gone (`dir .env` reports file not found).

*Safety checks that are unchanged by this procedure:* the server still refuses unless Windows authentication and a local
server are configured, the login is `<machine>\dq_reader` without impersonation, read-only permissions are definitely
verified, and `HAS_DBACCESS('dataops_sim')` is exactly 0. The database is fixed to `dataops_ecommerce_demo`; `DB_NAME` in the
`.env` is ignored. There is no override and `allow_unverified_read_only` is never used on this path. A `.env` only supplies
connection settings; it cannot relax any of these checks.

**Run the checks from an environment that really is the reader.** Before step 4, confirm in the same shell:

- *Identity:* `whoami` prints `<machine>\dq_reader`. If it prints your own account, stop.
- *Python executable:* `python -c "import sys; print(sys.executable); print(sys.version)"` must print the interpreter you
  intend (3.10+) and `python -c "import pyodbc; print(pyodbc.version)"` must succeed. `where python` shows which one wins on PATH.
- *Working directory:* `cd` then `cd` again with no arguments (or `echo %CD%`) must print the validation checkout, and
  `dir scripts\ecommerce_live_validate.py` must exist.
- *Checkout permissions:* `dq_reader` needs read access to the checkout and to the Python installation / virtual environment
  (`icacls "<path>"`). Do not change permissions on the main project to make this work; use a separate validation copy.

1. Unit tests first (no database): `python -m pytest -q tests/test_ecommerce_*.py tests/test_simulation_catalogue_intact.py`.
2. Catalogue, no connection: `python -m scripts.ecommerce_check --list`.
3. Start a shell as the reader: `runas /user:%COMPUTERNAME%\dq_reader cmd` (you enter the password), then `cd` to the checkout.
   **Do not use `runas /netonly`.** `/netonly` only changes the identity used for *network* connections; the process (and
   Windows integrated authentication to a local instance) still runs as you, so the guard would see the wrong login, or worse,
   appear to work with the wrong identity. The guard requires `SUSER_SNAME() = ORIGINAL_LOGIN() = <machine>\dq_reader`.
   `runas` caveats: a fresh shell may have a different PATH (Python may not be found), may not see your user-level Python or
   virtual environment, and may lack permission to read the checkout; fix those by installing/using a path readable by
   `dq_reader`, not by running as another account. If `runas` fails or the checks above fail, stop.
4. Identity and target only: `python -m scripts.ecommerce_check --verify`. Expect `status: ok`, database
   `dataops_ecommerce_demo`, `read_only_verified: true`. Any other result: stop and send it back; do not switch accounts.
5. All eight tools: `python -m scripts.ecommerce_live_validate --confirm-live-read-only --output reports\ecommerce_live_validation.json`.
   It stops after the target check unless the database, the Windows reader without impersonation, the zero writable tables and
   `HAS_DBACCESS('dataops_sim') = 0` (recorded as `dataops_sim_denied`) are all confirmed. The JSON holds counts, rule ids,
   durations and error codes only.

   | Outcome | Meaning | Exit |
   |---|---|---|
   | `VERIFIED` | all eight tools ran once, every result complete, no error code, sweep complete, no cleanup warning, and the record was written and read back | 0 |
   | `VERIFIED_WITH_WARNINGS` | complete, but a cleanup warning was reported; not accepted as unqualified | 1 |
   | `NOT_VERIFIED` | a tool was partial, busy, timed out, refused, errored, missing or duplicated, or the session failed part-way | 1 |
   | `BLOCKED` | the target check failed or the server could not start; later tools were not run | 1 |
   | (usage error) | missing `--confirm-live-read-only` | 2 |
   | `UNRECORDED` | the outcome could not be saved (bad `--output`); not accepted as evidence | 4 |

   **Report-generation failure vs a completed validation.** A completed validation prints per-tool lines for all eight tools,
   `RESULT: <outcome>`, and `record written to <path> and read back`. A report-generation failure prints `RECORDING FAILED:`
   and `RESULT: UNRECORDED` (exit 4): the checks may have run, but there is no evidence and nothing is accepted. A server or
   session failure prints `RESULT: NOT_VERIFIED` or `BLOCKED` with the error type only (add `--verbose` for the local
   traceback on stderr; it may contain local paths and is never written to the record). Only `RESULT: VERIFIED` with exit 0 and
   the "record written ... read back" line is a completed, recorded validation.
6. OLIST-008 comparison (read-only, aggregate counts only). Run it yourself, NOT through the MCP server, **as `dq_reader`
   against `dataops_ecommerce_demo`** (the query is the one in the OLIST-008 section above):
   - *sqlcmd (in the `dq_reader` shell from step 3):*
     `sqlcmd -S localhost\SQLEXPRESS03 -d dataops_ecommerce_demo -E -C -b -i olist008_aggregate.sql`
     (`-E` = Windows authentication as the current process identity, `-C` trusts the local certificate, `-b` fails on error).
     First run `sqlcmd -S localhost\SQLEXPRESS03 -d dataops_ecommerce_demo -E -C -Q "SELECT SUSER_SNAME(), DB_NAME()"` and
     confirm it prints `<machine>\dq_reader` and `dataops_ecommerce_demo`; if not, stop.
   - *SSMS:* SSMS must itself run as `dq_reader` (start it from the same `runas` shell; "Run as different user" works, `runas
     /netonly` does not). Connect with Windows Authentication to `localhost\SQLEXPRESS03`, choose database
     `dataops_ecommerce_demo` in the connection's *Options* tab, run `SELECT SUSER_SNAME(), DB_NAME();` first, and only then run
     the aggregate query. Do not run it under your own or an administrator login.
7. Send back the JSON record (it contains no names or identifiers) and the console output.

**No live validation is attempted until the correct environment and identity exist** (Windows host, local instance, ODBC
driver 18, `dq_reader` via `runas` without `/netonly`, readable checkout and interpreter). A failed identity check is a stop,
not something to work around with an administrator or alternative account.

What this run can show, and cannot: it shows SQL Server accepts every statement, the identity checks pass on the real server,
`HAS_DBACCESS('dataops_sim')` is 0, and real timings. It does not prove the driver's statement timeout fires (no tool can
issue a deliberately slow statement through the guard); to test that you would need a separately approved probe.

## Offline test network guard (Stage 4.5C)

`tests/test_ecommerce_agent.py` blocks network access during the offline agent tests through `tests/offline_network_guard.py`.
The first version refused every `socket.connect`. On Windows with Python 3.10 each `asyncio.run()` creates its event loop's
internal self-pipe with `socket.socketpair()`, which Windows implements in Python by connecting a loopback socket to itself,
so that guard stopped the event loop from starting and six tests failed with `network access attempted` (plus unawaited-coroutine
and half-initialised-loop warnings). The guard now refuses every `connect`/`connect_ex` except a loopback connect made from
inside `socket.socketpair()` itself; real outbound addresses, loopback connections to other local services (such as a database)
and host names stay blocked, and everything is restored by `monkeypatch`. Regression tests in the same file show the old guard
fails, the new guard lets the loop start, and outbound and non-socket-pair loopback connects are still refused. The
failure itself was observed on Windows; the Linux run proves the mechanism with a Windows-style socket pair but does not
reproduce the original Windows failure, so confirm on Windows (`python -m pytest -q tests/test_ecommerce_agent.py`).

## Integration plan and rollback (not applied; needs your explicit approval)

Files to add (26): `app/ecommerce/{__init__,target,guard,diagnostics,service,verification}.py`,
`app/mcp_server/ecommerce/{__init__,__main__,schemas,server}.py`, `app/agent/profiles.py`,
`scripts/{ecommerce_check,ecommerce_live_validate,investigate_ecommerce}.py`, `docs/ecommerce-agent.md`,
`tests/{ecommerce_support,test_simulation_catalogue_intact}.py`, `tests/data/sim_mcp_catalogue.json` and
`tests/test_ecommerce_{agent,diagnostics,guard,hardening,incomplete_and_workers,live_validate,mcp_server,timeouts}.py`.
Files to modify (5, all optional-parameter, default-preserving): `app/agent/{graph,mcp_client,offline,report,runner}.py`.
Must stay unchanged (byte-identical to the main project at review time): `app/db/readonly_preflight.py`, `app/db/connection.py`,
`app/config.py`, `app/diagnostics/data_quality_engine.py`, `config/data_quality_rules.yaml`, `app/mcp_server/{server,provider,schemas,__main__}.py`.
Dependencies/config: none new (`mcp`, `sqlalchemy`, `pyodbc`, `pydantic` already used); no `.env` change.
Simulation impact: the simulation server and tools are untouched. Behaviour change worth knowing: `report.finalize_report`
now writes the headline of every non-clean run (failed, inconclusive, incomplete) itself, for the simulation agent too.

Before: record the main project's test results; take a file-hash manifest. After: rerun the whole unit suite in chunks and the
simulation/agent/MCP files; run steps 1-2 of the runbook. Rollback: the change is purely additive plus five agent files, so
remove the 26 added files and restore the five agent files from the manifest/backup; nothing in the database changes.

## Result meanings

- `issue_detected`: an integrity rule found violations (a confirmed count; the cause is not established).
- `warning` (low): a business signal (late delivery) or a pending investigation.
- `error`: the guard refused, the schema differed, the rule file was unusable, or SQL failed.
- The agent separates confirmed facts from hypotheses and does not state a root cause for this profile.

## Commands

```
python -m scripts.ecommerce_check --list            # lists the tools; no server, settings or database
python -m app.mcp_server.ecommerce                  # stdio server (connects only when a tool is called)
python -m pytest tests/test_ecommerce_guard.py tests/test_ecommerce_diagnostics.py tests/test_ecommerce_mcp_server.py tests/test_ecommerce_agent.py tests/test_simulation_catalogue_intact.py
python -m scripts.investigate_ecommerce --offline "incident text"   # LIVE database access: only after approval
```

## Known limitations

- The identity and catalogue SQL and the driver `timeout` attribute are untested against a real SQL Server, and so is the
  time budget (the tests simulate time).
- The whole agent process must be launched as `dq_reader` (the MCP subprocess inherits the identity); how that account reads
  the project files and `.env` is not established.
- The rule file `config/data_quality_rules.yaml` is reused unchanged; the payments and reviews checks live in the module.
