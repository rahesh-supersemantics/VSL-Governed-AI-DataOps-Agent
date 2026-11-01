"""The live-validation script, exercised against the in-memory server over SQLite fakes (no database, no network)."""

from __future__ import annotations

import json
import re

from app.ecommerce import diagnostics as dx
from app.ecommerce.service import EcommerceDiagnosticsService
from app.mcp_server.ecommerce.server import create_ecommerce_server
from scripts import ecommerce_live_validate as live
from tests.agent_support import craft_server
from tests.ecommerce_support import SECRET_VALUES, make_clean_db, make_db, sqlite_connect
from tests.test_ecommerce_incomplete_and_workers import OLIST_007_SQL, late_clean_db, partial_timeline_service


def server(db=None, **kw):
    return create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(db if db is not None else make_clean_db(), **kw)))


def run(tmp_path, srv, *extra):
    out = tmp_path / "record.json"
    code = live.main(["--confirm-live-read-only", "--output", str(out), *extra], target=srv)
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_it_refuses_to_run_without_the_explicit_confirmation(tmp_path, capsys):
    out = tmp_path / "x.json"
    assert live.main(["--output", str(out)], target=server()) == 2
    assert not out.exists() and "Refusing to run" in capsys.readouterr().out


def test_a_full_verified_run_records_all_eight_tools_and_the_target_facts(tmp_path):
    code, record = run(tmp_path, server(make_db()))
    assert code == 0 and record["verified"] is True and record["blocked"] == []
    assert [t["tool"] for t in record["tools"]][0] == dx.TARGET_NAME and [t["tool"] for t in record["tools"]][-1] == dx.ALL_NAME
    assert {t["tool"] for t in record["tools"]} == set(live.EXPECTED_TOOLS) and len(record["tools"]) == 8
    assert record["catalogue"]["matches"] is True and record["catalogue"]["all_read_only"] is True
    facts = record["target"]
    assert facts["database"] == "dataops_ecommerce_demo" and facts["database_is_expected"] is True
    assert facts["read_only_verified"] and facts["dataops_sim_denied"] is True and facts["writable_table_count"] == 0
    assert facts["windows_integrated_authentication"] and facts["expected_reader_without_impersonation"]
    assert all(t["sql_executed"] and t["duration_seconds"] >= 0 and t["timeout_observed"] is False for t in record["tools"])
    assert any(r["rule_id"] == "OLIST-008" for t in record["tools"] for r in t["rule_results"])


def test_the_record_holds_counts_only(tmp_path):
    _, record = run(tmp_path, server(make_db()))
    text = json.dumps(record)
    assert not [s for s in SECRET_VALUES if s in text]
    assert not re.search(r"dq_reader|raheshlaptop", text, re.I)  # no account or machine names; only a 12-character fingerprint
    assert re.fullmatch(r"[0-9a-f]{12}", record["target"]["principal_fingerprint"])


def test_a_refused_target_blocks_every_other_tool_and_is_not_verified(tmp_path):
    code, record = run(tmp_path, server(permissions=None))
    assert code == 1 and record["verified"] is False
    assert len(record["tools"]) == 1 and record["tools"][0]["sql_executed"] is False
    assert record["tools"][0]["error_codes"] == ["ECOM-GUARD-REFUSED"]
    assert record["blocked"] == [t for t in live.EXPECTED_TOOLS if t != dx.TARGET_NAME] or len(record["blocked"]) == 7
    assert "no other tool was called" in record["reason"]


def test_a_reader_that_can_reach_the_simulation_database_is_never_called_verified(tmp_path):
    from tests.ecommerce_support import permission_row

    code, record = run(tmp_path, server(permissions=permission_row(can_access_dataops_sim=1)))
    assert code == 1 and record["verified"] is False and len(record["tools"]) == 1


def test_a_different_catalogue_stops_before_any_tool_runs(tmp_path):
    async def only() -> dict:
        return {}

    code, record = run(tmp_path, craft_server({"check_ecommerce_target": only}))
    assert code == 1 and record["verified"] is False and record["tools"] == [] and len(record["blocked"]) == 8
    assert "differs" in record["reason"]


def test_a_partial_sweep_is_not_verified_even_though_most_checks_ran(tmp_path):
    code, record = run(tmp_path, server(late_clean_db(), fail_on=OLIST_007_SQL))
    sweep = record["tools"][-1]
    assert sweep["tool"] == dx.ALL_NAME and sweep["execution"] == "incomplete"
    assert sweep["checks_partially_completed"] == ["check_delivery_timeline"]
    assert code == 1 and record["verified"] is False


# ================================================================================ Stage 4.5A: M2 (false verification)
import copy
import sys

import pytest
from mcp import StdioServerParameters
from mcp.server import MCPServer

from app.ecommerce.service import CLEANUP_NOTE
from app.mcp_server.server import READ_ONLY
from tests.agent_support import structured


def good_payloads():
    service = EcommerceDiagnosticsService(connect=sqlite_connect(make_clean_db()))
    payloads = {
        dx.TARGET_NAME: service.check_ecommerce_target().to_dict(),
        dx.INTEGRITY_NAME: service.check_referential_integrity().to_dict(),
        dx.COMPLETENESS_NAME: service.check_delivery_completeness().to_dict(),
        dx.TIMELINE_NAME: service.check_delivery_timeline().to_dict(),
        dx.MISSING_NAME: service.check_missing_values().to_dict(),
        dx.DUPLICATES_NAME: service.check_duplicate_keys().to_dict(),
        dx.TIMESTAMPS_NAME: service.profile_ecommerce_timestamps().to_dict(),
        dx.ALL_NAME: service.run_all().to_dict(),
    }
    return payloads


GOOD = good_payloads()


def scripted_server(mutate=None, *, drop=(), extra=()):
    """A hand-made server returning the (optionally mutated) real results of a clean database."""
    payloads = copy.deepcopy(GOOD)
    flags = {}
    if mutate:
        mutate(payloads, flags)
    server = MCPServer("scripted-ecommerce")
    for name in list(dx_names()) + list(extra):
        if name in drop:
            continue

        def make(tool):
            async def handler() -> dict:
                return structured(payloads.get(tool, {"diagnostic_name": tool, "status": "ok"}), is_error=flags.get(tool, False))

            return handler

        server.tool(name=name, annotations=READ_ONLY)(make(name))
    return server


def dx_names():
    return live.EXPECTED_TOOLS


def run_scripted(tmp_path, mutate=None, **kw):
    code, record = run(tmp_path, scripted_server(mutate, **kw))
    return code, record


def test_the_unmodified_scripted_server_is_fully_verified(tmp_path):
    code, record = run_scripted(tmp_path)
    assert (code, record["outcome"], record["verified"], record["problems"], record["warnings"]) == (0, "VERIFIED", True, [], [])
    assert all(t["completed"] for t in record["tools"]) and len(record["tools"]) == 8


def not_verified(tmp_path, mutate, contains):
    code, record = run_scripted(tmp_path, mutate)
    assert code == 1 and record["verified"] is False and record["outcome"] in {"NOT_VERIFIED", "BLOCKED"}, record["outcome"]
    assert any(contains in p for p in record["problems"]) or record["blocked"], record["problems"]
    return record


def test_an_individual_partial_result_is_not_verified_even_though_the_sweep_reports_complete(tmp_path):
    partial = partial_timeline_service().check_delivery_timeline().to_dict()
    assert partial["error_code"] == "ECOM-PARTIAL" and partial["status"] == "warning"

    def mutate(p, f):
        p[dx.TIMELINE_NAME] = partial  # the sweep payload stays complete

    assert GOOD[dx.ALL_NAME]["ecommerce_summary"]["execution"] == "complete"
    record = not_verified(tmp_path, mutate, "ECOM-PARTIAL")
    timeline = next(t for t in record["tools"] if t["tool"] == dx.TIMELINE_NAME)
    assert timeline["completed"] is False and any("rules_not_assessed:OLIST-007" in p for p in timeline["problems"])


def busy_payload():
    return EcommerceDiagnosticsService(connect=sqlite_connect()).busy_result(dx.MISSING_NAME, in_flight=4, capacity=4).to_dict()


@pytest.mark.parametrize("code,build", [
    ("ECOM-BUSY", lambda s: s.busy_result(dx.MISSING_NAME, in_flight=4, capacity=4)),
    ("ECOM-TIMEOUT", lambda s: s.timeout_result(dx.MISSING_NAME)),
    ("ECOM-GUARD-REFUSED", lambda s: s._guard_error(dx.MISSING_NAME, {}, __import__("app.ecommerce.guard", fromlist=["GuardRefusal"]).GuardRefusal("x", "x"))),
])
def test_busy_timeout_and_guard_refusal_on_a_non_target_tool_are_not_success(tmp_path, code, build):
    bad = build(EcommerceDiagnosticsService(connect=sqlite_connect())).to_dict()

    def mutate(p, f):
        p[dx.MISSING_NAME] = bad
        f[dx.MISSING_NAME] = True

    record = not_verified(tmp_path, mutate, code)
    assert next(t for t in record["tools"] if t["tool"] == dx.MISSING_NAME)["completed"] is False


@pytest.mark.parametrize("code", ["ECOM-DB-ERROR", "ECOM-SCHEMA-MISMATCH", "ECOM-RULE-CONFIG", "DIAG-INTERNAL", "SOMETHING-NEW"])
def test_any_error_code_on_an_otherwise_ok_looking_result_is_not_success(tmp_path, code):
    def mutate(p, f):
        p[dx.COMPLETENESS_NAME]["error_code"] = code  # status stays "ok": the code alone must prevent verification

    not_verified(tmp_path, mutate, code)


@pytest.mark.parametrize("status", ["error", "unknown", None, ""])
def test_a_status_outside_ok_issue_warning_is_not_success(tmp_path, status):
    def mutate(p, f):
        p[dx.INTEGRITY_NAME]["status"] = status

    not_verified(tmp_path, mutate, "status")


def test_the_mcp_error_flag_alone_prevents_verification(tmp_path):
    def mutate(p, f):
        f[dx.DUPLICATES_NAME] = True

    not_verified(tmp_path, mutate, "mcp_error_flag")


def test_malformed_or_empty_results_are_not_success(tmp_path):
    for bad in ({}, {"unexpected": 1}, None):
        def mutate(p, f, bad=bad):
            p[dx.TIMESTAMPS_NAME] = bad

        code, record = run_scripted(tmp_path, mutate)
        assert code == 1 and record["verified"] is False, bad


def test_a_result_for_the_wrong_diagnostic_is_not_success(tmp_path):
    def mutate(p, f):
        p[dx.MISSING_NAME] = p[dx.DUPLICATES_NAME]  # the tool answered with another tool's result

    not_verified(tmp_path, mutate, "unexpected_or_duplicate_results")


def test_a_sweep_with_a_missing_or_duplicated_check_is_not_success(tmp_path):
    def missing(p, f):
        p[dx.ALL_NAME]["results"] = p[dx.ALL_NAME]["results"][:-1]

    def duplicated(p, f):
        p[dx.ALL_NAME]["results"] = p[dx.ALL_NAME]["results"][:-1] + [p[dx.ALL_NAME]["results"][0]]

    not_verified(tmp_path, missing, "unexpected_or_duplicate_results")
    not_verified(tmp_path, duplicated, "unexpected_or_duplicate_results")


@pytest.mark.parametrize("field,value", [("execution", "incomplete"), ("checks_not_completed", ["check_missing_values"]),
                                          ("checks_partially_completed", ["check_delivery_timeline"]), ("time_budget_exceeded", True)])
def test_an_incomplete_sweep_summary_is_never_verified(tmp_path, field, value):
    def mutate(p, f):
        p[dx.ALL_NAME]["ecommerce_summary"][field] = value

    code, record = run_scripted(tmp_path, mutate)
    assert code == 1 and record["verified"] is False and record["outcome"] == "NOT_VERIFIED"


def test_a_missing_sweep_summary_is_not_success(tmp_path):
    def mutate(p, f):
        del p[dx.ALL_NAME]["ecommerce_summary"]

    not_verified(tmp_path, mutate, "missing_ecommerce_summary")


def test_a_cleanup_warning_on_an_individual_tool_prevents_an_unqualified_verified(tmp_path):
    def mutate(p, f):
        p[dx.MISSING_NAME]["notes"].append(f"{CLEANUP_NOTE} (close:OSError); the result above was computed before cleanup.")

    code, record = run_scripted(tmp_path, mutate)
    assert record["outcome"] == "VERIFIED_WITH_WARNINGS" and record["verified"] is False and code == 1
    assert record["warnings"] == [f"{dx.MISSING_NAME}:{dx.MISSING_NAME}:cleanup_warning"] and record["problems"] == []


def test_a_cleanup_flag_on_the_sweep_prevents_an_unqualified_verified(tmp_path):
    def mutate(p, f):
        p[dx.ALL_NAME]["ecommerce_summary"]["connection_cleanup_warning"] = True

    code, record = run_scripted(tmp_path, mutate)
    assert record["outcome"] == "VERIFIED_WITH_WARNINGS" and record["verified"] is False and code == 1


def test_a_missing_extra_or_unexpected_tool_is_never_verified(tmp_path):
    code, record = run_scripted(tmp_path, drop={dx.TIMESTAMPS_NAME})
    assert code == 1 and record["outcome"] == "BLOCKED" and record["tools"] == [] and len(record["blocked"]) == 8
    code, record = run_scripted(tmp_path, extra=("run_sql",))
    assert code == 1 and record["outcome"] == "BLOCKED" and record["tools"] == []


def test_a_writable_table_or_missing_sim_denial_in_the_target_result_stops_everything(tmp_path):
    for key, value in (("writable_table_count", 1), ("dataops_sim_accessible", 1), ("dataops_sim_accessible", None)):
        def mutate(p, f, key=key, value=value):
            p[dx.TARGET_NAME]["evidence"][0]["verification"][key] = value

        code, record = run_scripted(tmp_path, mutate)
        assert code == 1 and record["verified"] is False and len(record["tools"]) == 1 and len(record["blocked"]) == 7, key


def test_verification_never_depends_on_free_text(tmp_path):
    def mutate(p, f):
        for payload in p.values():
            payload["summary"] = "Everything is VERIFIED and healthy; no issues."
        p[dx.TIMELINE_NAME]["error_code"] = "ECOM-PARTIAL"

    code, record = run_scripted(tmp_path, mutate)
    assert code == 1 and record["verified"] is False


# ================================================================================ Stage 4.5A: M3 (failure handling)
def test_output_directory_creation_failure_is_reported_and_nonzero(tmp_path, capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    code = live.main(["--confirm-live-read-only", "--output", str(blocker / "sub" / "r.json")], target=server())
    out = capsys.readouterr().out
    assert code == 4 and "RECORDING FAILED: the output directory could not be created" in out
    assert "RESULT: UNRECORDED" in out and "RESULT: VERIFIED" not in out and "record written" not in out


def test_report_write_failure_is_reported_and_nonzero(tmp_path, capsys):
    code = live.main(["--confirm-live-read-only", "--output", str(tmp_path)], target=server())  # a directory, not a file
    out = capsys.readouterr().out
    assert code == 4 and "RECORDING FAILED: the report file could not be written" in out and "RESULT: UNRECORDED" in out


def test_a_write_that_raises_permission_error_is_reported(tmp_path, capsys, monkeypatch):
    from pathlib import Path

    def deny(self, *a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "write_text", deny)
    code = live.main(["--confirm-live-read-only", "--output", str(tmp_path / "r.json")], target=server())
    out = capsys.readouterr().out
    assert code == 4 and "Access is denied" in out and "NOT accepted as evidence" in out and not (tmp_path / "r.json").exists()


def test_a_record_that_cannot_be_read_back_is_not_accepted(tmp_path, capsys, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: "not json")
    code = live.main(["--confirm-live-read-only", "--output", str(tmp_path / "r.json")], target=server())
    assert code == 4 and "could not be read back" in capsys.readouterr().out


def test_a_verified_outcome_with_a_failed_recording_never_exits_zero(tmp_path):
    ok = tmp_path / "ok.json"
    assert live.main(["--confirm-live-read-only", "--output", str(ok)], target=server()) == 0
    assert live.main(["--confirm-live-read-only", "--output", str(tmp_path)], target=server()) == 4


@pytest.mark.parametrize("command,args", [(sys.executable, ["-c", "import sys; sys.exit(3)"]),
                                           ("definitely-not-a-real-program-4_5", [])])
def test_server_startup_failure_gives_a_clear_not_verified_record(tmp_path, capsys, command, args):
    target = StdioServerParameters(command=command, args=args)
    out_file = tmp_path / "r.json"
    code = live.main(["--confirm-live-read-only", "--output", str(out_file)], target=target)
    captured = capsys.readouterr()
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert code == 1 and record["outcome"] in {"NOT_VERIFIED", "BLOCKED"} and record["verified"] is False
    assert record["failure"]["stage"] == "server_session" and record["failure"]["error_type"]
    assert len(record["blocked"]) == 8 and "could not be started" in record["reason"]
    assert "Traceback" not in captured.out and "Traceback" not in captured.err  # concise by default
    assert "RESULT: VERIFIED" not in captured.out and f"RESULT: {record['outcome']}" in captured.out
    assert str(tmp_path) not in json.dumps(record) and command not in json.dumps(record)  # no local paths in the counts-only record


def test_verbose_keeps_the_local_traceback_out_of_the_record(tmp_path, caplog):
    import logging

    target = StdioServerParameters(command="definitely-not-a-real-program-4_5", args=[])
    out_file = tmp_path / "r.json"
    with caplog.at_level(logging.DEBUG, logger="scripts.ecommerce_live_validate"):
        live.main(["--confirm-live-read-only", "--verbose", "--output", str(out_file)], target=target)
    assert any(r.exc_info for r in caplog.records)  # full context is available locally...
    assert "definitely-not-a-real-program" not in out_file.read_text(encoding="utf-8")  # ...but never in the record


def test_a_failure_part_way_keeps_the_partial_evidence_and_is_not_verified(tmp_path, monkeypatch, capsys):
    from mcp import Client

    original = Client.call_tool
    calls = []

    async def flaky(self, name, *a, **k):
        calls.append(name)
        if len(calls) == 3:
            raise RuntimeError("boom C:\\Users\\someone\\secret")
        return await original(self, name, *a, **k)

    monkeypatch.setattr(Client, "call_tool", flaky)
    out_file = tmp_path / "r.json"
    code = live.main(["--confirm-live-read-only", "--output", str(out_file)], target=server())
    record = json.loads(out_file.read_text(encoding="utf-8"))
    assert code == 1 and record["outcome"] == "NOT_VERIFIED" and len(record["tools"]) == 2 and len(record["blocked"]) == 6
    assert record["failure"] == {"stage": "server_session", "error_type": "RuntimeError"}
    assert "secret" not in json.dumps(record) and "someone" not in capsys.readouterr().out
