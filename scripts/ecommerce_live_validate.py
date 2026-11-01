"""LIVE, read-only validation of the eight Ecommerce MCP tools. Run it as the dedicated reader account.

    python -m scripts.ecommerce_live_validate --confirm-live-read-only [--output reports/ecommerce_live_validation.json]

It starts ``python -m app.mcp_server.ecommerce`` under the CURRENT Windows account and calls the tools through the real MCP
protocol. The server verifies the database, the account and its read-only permissions on every call and refuses otherwise;
this script adds no way around that. Order: catalogue -> check_ecommerce_target (stop if it is not ``ok``) -> the six
individual checks -> the combined sweep. Nothing is written to the database; only aggregate counts, rule ids, durations
and error codes are recorded (no record identifiers, no review text, no account names, no secrets).

Outcomes (decided only from code-controlled status fields, never from free text):
  VERIFIED                 target verified and all eight tools completed with no error code, partial rule, busy/timeout or cleanup warning
  VERIFIED_WITH_WARNINGS   everything completed but a cleanup warning was reported (NOT a full verification)
  NOT_VERIFIED / BLOCKED   anything else, including a server that could not start or a missing/duplicated/unexpected result
Exit code: 0 VERIFIED and recorded; 1 not (fully) verified; 2 usage; 4 the record could not be written (the run is not accepted).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import sys
import time
from pathlib import Path
from typing import Any

from mcp import Client

from app.ecommerce import diagnostics as dx
from app.ecommerce.target import ECOMMERCE_DATABASE, MIN_AGENT_TOOL_TIMEOUT_SECONDS
from app.ecommerce.service import CLEANUP_NOTE
from scripts._common import EXIT_CHECK_FAILED, EXIT_OK

EXIT_RECORDING_FAILED = 4

EXPECTED_TOOLS = (dx.TARGET_NAME, dx.INTEGRITY_NAME, dx.COMPLETENESS_NAME, dx.TIMELINE_NAME, dx.MISSING_NAME, dx.DUPLICATES_NAME,
                  dx.TIMESTAMPS_NAME, dx.ALL_NAME)
PURPOSE = {
    dx.TARGET_NAME: "verified target, account, read-only status, table presence and row counts",
    dx.INTEGRITY_NAME: "orphaned references against parent tables",
    dx.COMPLETENESS_NAME: "delivered orders missing customer or carrier delivery dates",
    dx.TIMELINE_NAME: "delivery before purchase; delivery later than estimated",
    dx.MISSING_NAME: "null key columns and null counts",
    dx.DUPLICATES_NAME: "candidate-key uniqueness (review_id reported as pending investigation)",
    dx.TIMESTAMPS_NAME: "aggregate min/max of timestamp columns",
    dx.ALL_NAME: "all checks on one verified connection",
}
NO_SQL_CODES = {"ECOM-GUARD-REFUSED", "ECOM-BUSY"}  # nothing was queried for these


LOG = logging.getLogger("scripts.ecommerce_live_validate")
OK_STATUSES = {"ok", "issue_detected", "warning"}  # findings are fine; only completeness matters for verification


def _rules(data: dict[str, Any]) -> list[dict[str, Any]]:
    results = data.get("results") or [data]
    return [e for r in results for e in (r.get("evidence") or []) if isinstance(e, dict) and e.get("type") == "rule_result"]


def assess(tool: str, data: Any, is_error: bool) -> tuple[list[str], list[str]]:
    """Decide from code-controlled fields only. Returns ``(problems, warnings)``; any problem means the tool did not complete.

    A problem is: an MCP error flag, a malformed or mismatched result, a status outside ok/issue_detected/warning, ANY error
    code (ECOM-PARTIAL, -BUSY, -TIMEOUT, -GUARD-REFUSED, -DB-ERROR, -SCHEMA-MISMATCH, ...), a rule that did not run, or (for the
    sweep) an incomplete execution. A cleanup warning is a warning, never a pass.
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict) or not data:
        return ["malformed_result"], warnings
    if is_error:
        problems.append("mcp_error_flag")
    sweep = tool == dx.ALL_NAME
    results = data.get("results") if sweep else [data]
    if not isinstance(results, list) or not results or not all(isinstance(r, dict) for r in results):
        return problems + ["malformed_result"], warnings
    names = [r.get("diagnostic_name") for r in results]
    expected = list(dx.CHECK_NAMES) if sweep else [tool]
    if sorted(map(str, names)) != sorted(expected):
        problems.append("unexpected_or_duplicate_results")
    if (data.get("overall_status") if sweep else data.get("status")) not in OK_STATUSES:
        problems.append(f"status_{data.get('overall_status') if sweep else data.get('status')}")
    for r in results:
        label = r.get("diagnostic_name")
        if r.get("status") not in OK_STATUSES:
            problems.append(f"{label}:status_{r.get('status')}")
        if r.get("error_code"):
            problems.append(f"{label}:{r['error_code']}")
        not_run = [e.get("rule_id") for e in (r.get("evidence") or []) if isinstance(e, dict)
                   and e.get("type") == "rule_result" and e.get("status") == "error"]
        if not_run:
            problems.append(f"{label}:rules_not_assessed:{','.join(map(str, not_run))}")
        if any(str(n).startswith(CLEANUP_NOTE) for n in (r.get("notes") or [])):
            warnings.append(f"{label}:cleanup_warning")
    if sweep:
        summary = data.get("ecommerce_summary")
        if not isinstance(summary, dict):
            problems.append("missing_ecommerce_summary")
        else:
            if summary.get("execution") != "complete":
                problems.append("sweep_execution_not_complete")
            if summary.get("checks_not_completed") != [] or summary.get("checks_partially_completed") != []:
                problems.append("sweep_has_incomplete_checks")
            if summary.get("time_budget_exceeded") is not False:
                problems.append("sweep_time_budget")
            if summary.get("connection_cleanup_warning") is not False:
                warnings.append("sweep:cleanup_warning")
    return list(dict.fromkeys(problems)), list(dict.fromkeys(warnings))


def _record(tool: str, arguments: dict[str, Any], data: dict[str, Any], seconds: float, is_error: bool) -> dict[str, Any]:
    results = data.get("results") if isinstance(data.get("results"), list) else [data]
    codes = sorted({r.get("error_code") for r in results if isinstance(r, dict) and r.get("error_code")})
    refused = bool(results) and all(isinstance(r, dict) and r.get("error_code") in NO_SQL_CODES for r in results)
    summary = data.get("ecommerce_summary") or {}
    problems, warnings = assess(tool, data, is_error)
    return {
        "tool": tool, "purpose": PURPOSE[tool], "input": arguments, "status": data.get("status") or data.get("overall_status"),
        "mcp_is_error": is_error, "error_codes": codes, "sql_executed": not refused, "duration_seconds": round(seconds, 3),
        "timeout_observed": "ECOM-TIMEOUT" in codes, "execution": summary.get("execution"),
        "checks_not_completed": summary.get("checks_not_completed"), "checks_partially_completed": summary.get("checks_partially_completed"),
        "connection_cleanup_warning": any(w.endswith("cleanup_warning") for w in warnings),
        "completed": not problems, "problems": problems, "warnings": warnings,
        "rule_results": [{k: v for k, v in e.items() if k != "type"} for e in _rules(data)],
        "summary": data.get("summary"),
    }


def target_facts(data: dict[str, Any]) -> dict[str, Any]:
    """What the target check proved (or did not). Never raises on a malformed result: unknown means not verified."""
    data = data if isinstance(data, dict) else {}
    evidence = next((e for e in (data.get("evidence") or []) if isinstance(e, dict) and e.get("type") == "target"), {})
    verification = evidence.get("verification") or {}
    observed = data.get("observed_value") or {}
    return {
        "database": observed.get("database"),
        "database_is_expected": observed.get("database") == ECOMMERCE_DATABASE,
        "read_only_verified": observed.get("read_only_verified") is True,
        "windows_integrated_authentication": verification.get("authentication") == "windows_integrated",
        "expected_reader_without_impersonation": verification.get("expected_reader") is True and verification.get("impersonation") is False,
        "dataops_sim_accessible": verification.get("dataops_sim_accessible"),
        "dataops_sim_denied": verification.get("dataops_sim_accessible") == 0,
        "writable_table_count": verification.get("writable_table_count"),
        "principal_fingerprint": verification.get("principal_fingerprint"),  # a 12-character hash, not a name
        "tables_found": observed.get("tables_found"), "tables_expected": observed.get("tables_expected"),
        "row_counts": evidence.get("row_counts"), "missing_tables": evidence.get("missing_tables"),
    }


def facts_ok(facts: dict[str, Any]) -> bool:
    return (facts["database_is_expected"] and facts["read_only_verified"] and facts["windows_integrated_authentication"]
            and facts["expected_reader_without_impersonation"] and facts["dataops_sim_denied"] and facts["writable_table_count"] == 0)


def new_report() -> dict[str, Any]:
    return {"schema_version": 2, "platform": platform.platform(), "python": platform.python_version(),
            "target_database": ECOMMERCE_DATABASE, "tools": [], "blocked": [], "catalogue": {}, "target": None,
            "outcome": "NOT_VERIFIED", "verified": False, "problems": [], "warnings": []}


def finalize(report: dict[str, Any]) -> dict[str, Any]:
    """Compute the outcome from the recorded tool results. Fail closed: anything unexpected is NOT_VERIFIED."""
    tools = report["tools"]
    names = [t["tool"] for t in tools]
    problems = list(report.get("problems") or [])
    if report["blocked"] or sorted(names) != sorted(EXPECTED_TOOLS):
        problems.append("not_all_eight_tools_ran_exactly_once")
    for t in tools:
        problems += [f"{t['tool']}:{p}" for p in t["problems"]]
    if not (report.get("target") and facts_ok(report["target"])):
        problems.append("target_not_verified")
    warnings = [f"{t['tool']}:{w}" for t in tools for w in t["warnings"]]
    report["problems"] = list(dict.fromkeys(problems))
    report["warnings"] = list(dict.fromkeys(warnings))
    if report["blocked"] and not tools:
        report["outcome"] = "BLOCKED"
    elif report["problems"]:
        report["outcome"] = "NOT_VERIFIED"
    elif report["warnings"]:
        report["outcome"] = "VERIFIED_WITH_WARNINGS"
    else:
        report["outcome"] = "VERIFIED"
    report["verified"] = report["outcome"] == "VERIFIED"
    return report


async def validate(target: Any, report: dict[str, Any] | None = None, *,
                   timeout_seconds: float = MIN_AGENT_TOOL_TIMEOUT_SECONDS + 5) -> dict[str, Any]:
    """Fills ``report`` as it goes, so partial evidence survives a failure part-way (the caller finalizes it)."""
    report = report if report is not None else new_report()
    async with Client(target, read_timeout_seconds=timeout_seconds) as client:
        listing = (await client.list_tools()).tools
        listed = {t.name: t for t in listing}
        report["catalogue"] = {"expected": sorted(EXPECTED_TOOLS), "actual": sorted(t.name for t in listing),
                               "matches": sorted(t.name for t in listing) == sorted(EXPECTED_TOOLS),  # duplicates cannot match
                               "all_read_only": all(bool(t.annotations and t.annotations.read_only_hint) for t in listing)}
        if not report["catalogue"]["matches"] or not report["catalogue"]["all_read_only"]:
            report["blocked"] = list(EXPECTED_TOOLS)
            report["reason"] = "the server catalogue differs from the expected eight read-only tools; stopped before calling any tool"
            return report
        order = [dx.TARGET_NAME] + [t for t in EXPECTED_TOOLS if t not in (dx.TARGET_NAME, dx.ALL_NAME)] + [dx.ALL_NAME]
        for index, tool in enumerate(order):
            started = time.monotonic()
            result = await client.call_tool(tool, {}, read_timeout_seconds=timeout_seconds)
            seconds = time.monotonic() - started
            data = result.structured_content if isinstance(result.structured_content, dict) else {}
            report["tools"].append(_record(tool, {}, data, seconds, bool(result.is_error)))
            if tool == dx.TARGET_NAME:
                report["target"] = target_facts(data)
                if not report["tools"][-1]["completed"] or not facts_ok(report["target"]):
                    report["blocked"] = list(order[index + 1:])
                    report["reason"] = ("the target check did not verify the database, the reader account, the read-only "
                                        "permissions and the denial of dataops_sim; no other tool was called")
                    return report
    return report


def record_report(report: dict[str, Any], path: Path) -> str | None:
    """Write and read back the JSON record. Returns ``None`` on success, otherwise a plain-language reason (never raises)."""
    try:
        text = json.dumps(report, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        return f"the report could not be serialised ({type(exc).__name__})"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"the output directory could not be created ({type(exc).__name__}: {exc.strerror or 'no detail'})"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"the report file could not be written ({type(exc).__name__}: {exc.strerror or 'no detail'})"
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("outcome") != report["outcome"]:
            return "the written report does not match what was validated"
    except (OSError, ValueError):
        return "the written report could not be read back"
    return None


def _leaf_type(exc: BaseException) -> str:
    """Name of the innermost exception (anyio wraps session failures in an ExceptionGroup)."""
    while getattr(exc, "exceptions", None):  # ExceptionGroup (builtin on 3.11+, backport on 3.10)
        exc = exc.exceptions[0]
    return type(exc).__name__


def _print(report: dict[str, Any], recording_failure: str | None = None) -> None:
    target = report.get("target") or {}
    print(f"target database: {report['target_database']}  expected: {target.get('database_is_expected')}  "
          f"read-only verified: {target.get('read_only_verified')}  dataops_sim denied: {target.get('dataops_sim_denied')}")
    for t in report["tools"]:
        print(f"  {t['tool']:<32} {str(t['status']):<15} {t['duration_seconds']:>8.3f}s  sql={t['sql_executed']}  "
              f"completed={t['completed']}  codes={','.join(t['error_codes']) or '-'}  execution={t['execution'] or '-'}")
    for name in report["blocked"]:
        print(f"  {name:<32} BLOCKED (not run)")
    for item in report["problems"]:
        print(f"  problem: {item}")
    for item in report["warnings"]:
        print(f"  warning: {item}")
    if recording_failure:  # never leave a passing-looking final line when the evidence could not be saved
        print(f"RECORDING FAILED: {recording_failure}. Choose a writable --output path.")
        print(f"RESULT: UNRECORDED - the outcome was {report['outcome']}, but no record exists, so this run is NOT accepted as evidence.")
        return
    print("RESULT:", report["outcome"] + ("" if report["outcome"] == "VERIFIED" else f" - {report.get('reason', 'see the problems above')}"))


def main(argv: list[str] | None = None, *, target: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ecommerce_live_validate", description=__doc__.splitlines()[0])
    parser.add_argument("--confirm-live-read-only", action="store_true",
                        help="required: acknowledge this runs read-only queries against the live Ecommerce database")
    parser.add_argument("--output", default="reports/ecommerce_live_validation.json", help="JSON file for the counts-only record")
    parser.add_argument("--verbose", action="store_true", help="print the full local traceback of a session failure to stderr")
    args = parser.parse_args(argv)
    if not args.confirm_live_read_only:
        print("Refusing to run: pass --confirm-live-read-only to acknowledge live, read-only queries as the current account.")
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if target is None:
        from app.agent.mcp_client import default_server_params

        target = default_server_params("app.mcp_server.ecommerce")
    report = new_report()
    try:
        asyncio.run(validate(target, report))
    except Exception as exc:  # noqa: BLE001 - a failed session must still produce an honest record
        kind = _leaf_type(exc)
        LOG.warning("the validation session failed (%s); no verification was achieved. Re-run with --verbose for the local "
                    "traceback (it may contain local paths).", kind)
        LOG.debug("session failure detail", exc_info=True)
        report["failure"] = {"stage": "server_session", "error_type": kind}
        report["reason"] = ("the MCP server could not be started or the session failed before every tool completed "
                            f"({kind}); partial results, if any, are listed")
        report["blocked"] = [t for t in EXPECTED_TOOLS if t not in {x["tool"] for x in report["tools"]}]
    finalize(report)
    path = Path(args.output)
    failure = record_report(report, path)
    _print(report, failure)
    if failure:
        return EXIT_RECORDING_FAILED
    print(f"record written to {path} and read back (counts, rule ids, durations and error codes only)")
    return EXIT_OK if report["verified"] else EXIT_CHECK_FAILED


if __name__ == "__main__":
    sys.exit(main())
