"""Stage 4.3: partial checks are incomplete runs (H1), model text cannot contradict the execution record (H2), the
in-flight worker cap (M1) and timeout-configuration validation (M2).

Everything is local: SQLite fakes, scripted models, threads released by events. No database, network or sleep-based timing.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from contextlib import contextmanager

import pytest
from mcp import Client

from app.agent.config import AgentConfig
from app.agent.graph import GraphDeps, build_graph
from app.agent.models import InvestigationReport, ToolOutcome
from app.agent.profiles import ECOMMERCE_PROFILE
from app.agent.report import compute_status, claims_clean
from app.agent.models import EvidenceItem
from app.ecommerce import diagnostics as dx
from app.ecommerce.service import INCOMPLETE_CODES, EcommerceDiagnosticsService
from app.ecommerce.target import (
    HARD_LIMIT_MARGIN_SECONDS,
    MAX_IN_FLIGHT_WORKERS,
    TOOL_BUDGET_SECONDS,
    validate_timeouts,
)
from app.mcp_server.ecommerce.server import WorkerGate, create_ecommerce_server, run_in_worker
from scripts.investigate_ecommerce import EXIT_BY_STATUS
from tests.agent_support import FakeToolbox, ScriptedModel, draft, plan, result_dict, tc
from tests.ecommerce_support import make_clean_db, sqlite_connect
from tests.test_ecommerce_agent import offline

OLIST_007_SQL = "NOT (t.[order_delivered_customer_date] >= t.[order_purchase_timestamp])"
CLEAN_CLAIMS = "Everything is healthy: all checks passed and there are no issues. No action is needed."
ECOM_TOOLS = sorted(ECOMMERCE_PROFILE.approved_tools)


def late_clean_db():
    """Valid data except one delivery later than estimated: a business-signal warning, no integrity violation."""
    db = make_clean_db()
    db.execute("update ecom_orders set order_delivered_customer_date = '2020-01-20'")
    return db


def partial_timeline_service(**kw):
    """The timeline check is a WARNING (OLIST-008 flagged) while OLIST-007 cannot run -> ECOM-PARTIAL."""
    return EcommerceDiagnosticsService(connect=sqlite_connect(late_clean_db(), fail_on=OLIST_007_SQL), **kw)


# ======================================================================================== H1: partial means incomplete
def test_a_warning_check_with_an_unassessed_rule_is_partial_and_the_sweep_is_incomplete():
    timeline = partial_timeline_service().check_delivery_timeline()
    assert timeline.status == "warning" and timeline.error_code == "ECOM-PARTIAL"
    report = partial_timeline_service().run_all().to_dict()
    results = {r["diagnostic_name"]: r for r in report["results"]}
    summary = report["ecommerce_summary"]
    # everything else is complete and clean
    assert all(results[n]["status"] == "ok" for n in dx.CHECK_NAMES if n != "check_delivery_timeline"), \
        {n: results[n]["status"] for n in dx.CHECK_NAMES}
    # ...but the roll-up cannot call this run complete
    assert report["overall_status"] == "warning"
    assert summary["execution"] == "incomplete"
    assert summary["checks_partially_completed"] == ["check_delivery_timeline"] and summary["checks_not_completed"] == []
    listing = {d["diagnostic"]: d for d in summary["diagnostics"]}
    assert listing["check_delivery_timeline"]["execution"] == "partial"
    assert listing["check_delivery_timeline"]["rules_not_assessed"] == ["OLIST-007"]
    assert all(d["execution"] == "complete" for n, d in listing.items() if n != "check_delivery_timeline")
    assert summary["rules"]["unassessed"] == 1


def test_completed_findings_are_kept_and_distinguished_from_the_unassessed_rule():
    result = partial_timeline_service().check_delivery_timeline().to_dict()
    rules = {e["rule_id"]: e for e in result["evidence"] if e["type"] == "rule_result"}
    assert rules["OLIST-008"]["status"] == "flagged" and rules["OLIST-008"]["violations"] == 1   # finding kept
    assert rules["OLIST-007"]["status"] == "error" and rules["OLIST-007"]["violations"] is None  # not a pass
    assert result["observed_value"]["rules_not_assessed"] == ["OLIST-007"]
    assert any(n.startswith("INCOMPLETE: OLIST-007") and "not a pass" in n for n in result["notes"])


def test_a_fully_assessed_warning_is_still_a_complete_run():
    service = EcommerceDiagnosticsService(connect=sqlite_connect(late_clean_db()))
    summary = service.run_all().to_dict()["ecommerce_summary"]
    assert summary["execution"] == "complete" and summary["checks_partially_completed"] == []


def test_the_agent_reports_inconclusive_never_clean_for_a_partial_timeline_check():
    server = create_ecommerce_server(partial_timeline_service())
    report = offline("is the ecommerce database healthy?", server)
    assert report.status == "inconclusive"
    assert report.status not in {"no_issue_detected", "warnings_only"}
    assert EXIT_BY_STATUS[report.status] == 1 != EXIT_BY_STATUS["no_issue_detected"]
    assert report.summary.startswith("INCONCLUSIVE")
    text = " ".join(report.limitations)
    assert "INCOMPLETE RUN" in text and "OLIST-007" in text
    # the completed late-delivery finding is still reported as an observed fact
    assert any("check_delivery_timeline" in f.statement for f in report.observed_findings)
    assert "not a clean result" in report.summary


def test_the_partial_run_cannot_be_rendered_as_clean():
    from app.agent.report import render_text

    report = offline("is the ecommerce database healthy?", create_ecommerce_server(partial_timeline_service()))
    rendered = render_text(report)
    assert "Status: inconclusive" in rendered and "No diagnostic evidence indicates a problem" not in rendered


def test_the_cli_exit_codes_keep_incomplete_states_non_zero():
    assert EXIT_BY_STATUS["inconclusive"] != 0 and EXIT_BY_STATUS["failed"] != 0
    assert {s for s, code in EXIT_BY_STATUS.items() if code == 0} == {"no_issue_detected", "warnings_only"}


def evidence(tool, status, severity="low", code=None):
    return EvidenceItem(evidence_id="E1", call_id="E1", tool=tool, status=status, severity=severity, summary="s", error_code=code)


def test_compute_status_partial_warning_is_inconclusive_with_the_profile_codes_and_unchanged_without():
    core = ECOMMERCE_PROFILE.core_diagnostics
    items = [evidence(t, "ok", "info") for t in core if t != "check_delivery_timeline"]
    items.append(evidence("check_delivery_timeline", "warning", "low", "ECOM-PARTIAL"))
    assert compute_status(items, [], None, core, ECOMMERCE_PROFILE.incomplete_codes) == "inconclusive"
    assert compute_status(items, [], None, core) == "warnings_only"  # default (simulation) behaviour is untouched
    items[-1] = evidence("check_delivery_timeline", "warning", "low", None)
    assert compute_status(items, [], None, core, ECOMMERCE_PROFILE.incomplete_codes) == "warnings_only"


def test_an_issue_in_a_partial_check_stays_an_issue_but_the_run_is_labelled_incomplete():
    core = ECOMMERCE_PROFILE.core_diagnostics
    toolbox = FakeToolbox({"run_ecommerce_diagnostics": {"overall_status": "issue_detected", "results": [
        result_dict(t, "ok", "info") for t in core if t != "check_referential_integrity"] + [
        result_dict("check_referential_integrity", "issue_detected", "high", error_code="ECOM-PARTIAL")]}},
        names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("run_ecommerce_diagnostics"))], draft(summary=CLEAN_CLAIMS)), toolbox)
    assert report.status == "issue_identified"  # a confirmed finding is not downgraded...
    assert report.summary.startswith("ISSUE(S) IDENTIFIED, but the run is incomplete")  # ...but it is not called complete
    assert not claims_clean(report.summary)


def test_profile_incomplete_codes_match_the_service():
    assert ECOMMERCE_PROFILE.incomplete_codes == INCOMPLETE_CODES == {"ECOM-PARTIAL", "ECOM-TIMEOUT"}


# ====================================================================== H2: model text cannot contradict the status
def run_ecom_graph(llm, toolbox, incident="is the ecommerce database healthy?") -> InvestigationReport:
    cfg = AgentConfig()
    graph = build_graph(GraphDeps(llm=llm, toolbox=toolbox, config=cfg, progress=lambda _m: None, profile=ECOMMERCE_PROFILE))

    async def go():
        state = await graph.ainvoke({"investigation_id": "INV-TEST", "incident": incident},
                                    config={"recursion_limit": 8 * cfg.max_rounds + 20})
        return InvestigationReport.model_validate(state["report"])

    return asyncio.run(go())


def misleading(**extra):
    return draft(summary=CLEAN_CLAIMS, confidence="high", confidence_explanation="All good, the data is healthy.",
                 hypotheses=[{"statement": "The database is healthy and nothing is wrong.", "evidence_ids": ["E1"]},
                             {"statement": "Late deliveries may reflect carrier delays (hypothesis).", "evidence_ids": ["E1"]}],
                 impact={"statement": "No impact: there are no issues.", "evidence_ids": ["E1"]},
                 recommended_next_steps=["No action is needed.", "Review the carrier SLA."],
                 limitations=["Nothing is wrong."], **extra)


def all_ok(skip=()):
    return {t: result_dict(t, "ok", "info") for t in ECOMMERCE_PROFILE.core_diagnostics if t not in skip}


def assert_code_controlled(report, status, lead):
    assert report.status == status
    assert report.summary.startswith(lead), report.summary
    assert not claims_clean(report.summary)
    blob = json.dumps(report.model_dump())
    for forbidden in ("Everything is healthy", "No action is needed", "Nothing is wrong", "no issues", "database is healthy"):
        assert forbidden not in blob, forbidden
    assert report.confidence == "low"


def test_failed_run_headline_is_code_controlled():
    refused = result_dict("check_ecommerce_target", "error", "unknown", error_code="ECOM-GUARD-REFUSED")
    report = run_ecom_graph(ScriptedModel([plan(tc("check_ecommerce_target"))], misleading()),
                            FakeToolbox({"check_ecommerce_target": refused}, names=ECOM_TOOLS))
    assert_code_controlled(report, "failed", "FAILED")


def test_timed_out_run_headline_is_code_controlled():
    timeout = result_dict("run_ecommerce_diagnostics", "error", "unknown", error_code="ECOM-TIMEOUT")
    toolbox = FakeToolbox({"run_ecommerce_diagnostics": {"overall_status": "error", "results": [timeout]}}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("run_ecommerce_diagnostics"))], misleading()), toolbox)
    assert report.status in {"failed", "inconclusive"} and report.status not in {"no_issue_detected", "warnings_only"}
    assert report.summary.startswith(("FAILED", "INCONCLUSIVE")) and "ECOM-TIMEOUT" in " ".join(report.limitations)
    assert not claims_clean(report.summary) and "Everything is healthy" not in json.dumps(report.model_dump())


def test_incomplete_run_headline_is_code_controlled():
    partial = result_dict("check_delivery_timeline", "warning", "low", error_code="ECOM-PARTIAL")
    toolbox = FakeToolbox({"run_ecommerce_diagnostics": {"overall_status": "warning", "results":
                           list(all_ok(("check_delivery_timeline",)).values()) + [partial]}}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("run_ecommerce_diagnostics"))], misleading()), toolbox)
    assert_code_controlled(report, "inconclusive", "INCONCLUSIVE")
    assert "only partly assessed" in report.summary


def test_inconclusive_run_with_unrun_checks_headline_is_code_controlled():
    toolbox = FakeToolbox({"check_ecommerce_target": result_dict("check_ecommerce_target", "ok", "info")}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("check_ecommerce_target"))], misleading()), toolbox)
    assert_code_controlled(report, "inconclusive", "INCONCLUSIVE")
    assert "required checks without a usable result" in report.summary


def test_a_neutral_model_summary_is_also_replaced_on_a_non_clean_run():
    toolbox = FakeToolbox({"check_ecommerce_target": result_dict("check_ecommerce_target", "ok", "info")}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("check_ecommerce_target"))], draft(summary="Looks good to me, ship it.")), toolbox)
    assert report.summary.startswith("INCONCLUSIVE") and "ship it" not in report.summary


def test_useful_model_explanations_survive_where_they_cannot_contradict():
    toolbox = FakeToolbox({"check_ecommerce_target": result_dict("check_ecommerce_target", "ok", "info")}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("check_ecommerce_target"))], misleading()), toolbox)
    assert [h.statement for h in report.hypotheses] == ["Late deliveries may reflect carrier delays (hypothesis)."]
    assert "Review the carrier SLA." in report.recommended_next_steps
    assert any("read as a clean result were discarded" in n for n in report.limitations)


def test_a_clean_successful_run_keeps_the_model_summary_unchanged():
    toolbox = FakeToolbox({t: r for t, r in all_ok().items()}, names=ECOM_TOOLS)
    calls = [tc(t) for t in sorted(ECOMMERCE_PROFILE.core_diagnostics)]
    report = run_ecom_graph(ScriptedModel([plan(*calls[:3]), plan(*calls[3:])], draft(summary="No integrity problems were found.")), toolbox)
    assert report.status == "no_issue_detected", (report.limitations, report.tools_executed)
    assert report.summary == "No integrity problems were found."


def test_claims_clean_does_not_fire_on_ordinary_finding_text():
    for text in ("1 of 6 integrity check(s) found violations: OLIST-004 1 row(s).", "Late delivery is a business signal.",
                 "business signal(s): OLIST-008 1 row(s)", "2 findings; most significant: orphaned orders."):
        assert not claims_clean(text), text


# ======================================================================================= M1: bounded in-flight workers
def test_worker_gate_validation_and_counting():
    for bad in (0, -1, True, "2", 1.5, None):
        with pytest.raises(ValueError):
            WorkerGate(bad)
    gate = WorkerGate(2)
    assert gate.try_acquire() and gate.try_acquire() and not gate.try_acquire() and gate.in_flight == 2
    gate.release()
    assert gate.in_flight == 1 and gate.try_acquire()
    gate.release(), gate.release(), gate.release()
    assert gate.in_flight == 0  # never negative
    assert MAX_IN_FLIGHT_WORKERS >= 1 and WorkerGate().capacity == MAX_IN_FLIGHT_WORKERS


def test_run_in_worker_frees_the_slot_only_when_the_function_ends_even_if_the_caller_gave_up():
    release, started = threading.Event(), threading.Event()
    gate = WorkerGate(1)

    def slow():
        started.set()
        release.wait(10)
        return "late"

    async def go():
        assert gate.try_acquire()
        with pytest.raises(TimeoutError):
            import anyio

            with anyio.fail_after(0.2):
                await run_in_worker(slow, gate)
        in_flight_after_giving_up = gate.in_flight
        release.set()
        deadline = time.monotonic() + 5
        while gate.in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return in_flight_after_giving_up

    try:
        held = asyncio.run(go())
    finally:
        release.set()
    assert started.is_set() and held == 1  # the slot was still held after the caller stopped waiting
    assert gate.in_flight == 0              # ...and freed once the worker really finished


def test_run_in_worker_releases_on_exception_and_when_the_thread_cannot_start(monkeypatch):
    gate = WorkerGate(1)

    def boom():
        raise RuntimeError("worker failed")

    async def failing():
        assert gate.try_acquire()
        with pytest.raises(RuntimeError, match="worker failed"):
            await run_in_worker(boom, gate)

    asyncio.run(failing())
    assert gate.in_flight == 0

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)

    async def no_thread():
        assert gate.try_acquire()
        with pytest.raises(RuntimeError, match="can't start"):
            await run_in_worker(lambda: 1, gate)

    asyncio.run(no_thread())
    assert gate.in_flight == 0


def stuck_then_healthy(blockers):
    """connect(): the first len(blockers) calls block until their event is set (then fail); later calls use a healthy fake."""
    starts: list[int] = []
    lock = threading.Lock()

    @contextmanager
    def connect(deadline=None):
        with lock:
            n = len(starts)
            starts.append(n)
        if n < len(blockers):
            blockers[n].wait(10)
            raise RuntimeError("released late")
        with sqlite_connect(make_clean_db())(deadline=deadline) as conn:
            yield conn

    return connect, starts


async def wait_until(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        await asyncio.sleep(0.01)
    return predicate()


def test_cap_timeout_busy_refusal_late_completion_and_recovery():
    blockers = [threading.Event(), threading.Event()]
    connect, starts = stuck_then_healthy(blockers)
    service = EcommerceDiagnosticsService(connect=connect, budget_seconds=0.2)
    server = create_ecommerce_server(service, hard_limit_seconds=0.3, safety_margin_seconds=0.1, max_in_flight=2)
    gate = server.worker_gate
    seen = {}

    async def go():
        async with Client(server) as client:
            seen["t1"] = await client.call_tool("check_delivery_timeline", {})
            seen["after1"] = gate.in_flight
            seen["t2"] = await client.call_tool("check_delivery_completeness", {})
            seen["after2"] = gate.in_flight
            seen["starts_before_busy"] = len(starts)
            seen["busy"] = await client.call_tool("check_missing_values", {})
            seen["busy_roll"] = await client.call_tool("run_ecommerce_diagnostics", {})
            seen["starts_after_busy"] = len(starts)
            blockers[0].set()  # worker 1 really finishes: only now does one slot come back
            seen["freed"] = await wait_until(lambda: gate.in_flight == 1)
            seen["ok"] = await client.call_tool("check_delivery_completeness", {})
            blockers[1].set()
            seen["all_freed"] = await wait_until(lambda: gate.in_flight == 0)

    try:
        asyncio.run(go())
    finally:
        for b in blockers:
            b.set()
    for key in ("t1", "t2"):  # timeouts: explicit, incomplete, and honest about the worker
        r = seen[key]
        assert r.is_error is True and r.structured_content["error_code"] == "ECOM-TIMEOUT"
        ev = r.structured_content["evidence"][0]
        assert ev["completed"] is False and ev["worker_cancelled"] is False
        assert "could not be cancelled" in r.structured_content["summary"]
    assert (seen["after1"], seen["after2"]) == (1, 2)  # capacity is NOT released when the caller stops waiting
    # cap reached: clear error, no new worker, no connection, no statement
    busy = seen["busy"]
    assert busy.is_error is True and busy.structured_content["error_code"] == "ECOM-BUSY"
    assert busy.structured_content["evidence"][0] == {"type": "worker_capacity", "in_flight": 2, "capacity": 2, "started": False}
    roll = seen["busy_roll"].structured_content
    assert seen["busy_roll"].is_error is True and roll["results"][0]["error_code"] == "ECOM-BUSY"
    assert roll["ecommerce_summary"]["execution"] == "incomplete"
    assert seen["starts_before_busy"] == seen["starts_after_busy"] == 2
    # recovery: a finished worker frees exactly one slot and the next call really runs
    assert seen["freed"] is True and seen["ok"].is_error is False and len(starts) == 3
    assert seen["ok"].structured_content["status"] in {"ok", "issue_detected", "warning"}
    assert seen["all_freed"] is True and gate.in_flight == 0
    # the stuck workers failed after their callers were gone: nothing they did reached anyone
    assert seen["t1"].structured_content["error_code"] == "ECOM-TIMEOUT"


def test_a_busy_refusal_is_an_error_the_agent_treats_as_incomplete():
    busy = result_dict("check_delivery_timeline", "error", "unknown", error_code="ECOM-BUSY")
    toolbox = FakeToolbox({"check_delivery_timeline": busy}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("check_delivery_timeline"))], misleading()), toolbox)
    assert report.status in {"failed", "inconclusive"} and report.summary.startswith(("FAILED", "INCONCLUSIVE"))


# ================================================================================ M2: timeout configuration consistency
def test_default_timeouts_are_valid_and_unchanged():
    assert (TOOL_BUDGET_SECONDS, HARD_LIMIT_MARGIN_SECONDS) == (45, 6)
    validate_timeouts(TOOL_BUDGET_SECONDS, TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS)
    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_clean_db())))
    assert server.worker_gate.capacity == MAX_IN_FLIGHT_WORKERS


def test_a_longer_budget_than_the_hard_limit_minus_margin_fails_closed():
    with pytest.raises(ValueError, match="exceeds the server hard limit"):
        create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(), budget_seconds=46))
    with pytest.raises(ValueError, match="exceeds the server hard limit"):
        create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect()), hard_limit_seconds=50)
    with pytest.raises(ValueError):
        create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(), budget_seconds=60), hard_limit_seconds=55)


@pytest.mark.parametrize("budget,hard,margin,ok", [
    (45, 51, 6, True), (45, 51, 6.0, True), (45.0001, 51, 6, False), (45, 50.9999, 6, False),
    (1, 1, 0, True), (1, 0.999, 0, False), (0.2, 0.3, 0.1, True), (0.2, 0.3, 0.11, False),
    (0, 10, 1, False), (-1, 10, 1, False), (10, 0, 0, False), (10, 20, -1, False),
    (math.nan, 51, 6, False), (45, math.inf, 6, False), (math.inf, math.inf, 0, False), (True, 51, 6, False), ("45", 51, 6, False),
])
def test_validate_timeouts_boundaries(budget, hard, margin, ok):
    if ok:
        validate_timeouts(budget, hard, margin)
    else:
        with pytest.raises(ValueError):
            validate_timeouts(budget, hard, margin)


def test_the_service_itself_rejects_an_unusable_budget():
    for bad in (0, -5, math.nan, math.inf):
        with pytest.raises(ValueError):
            EcommerceDiagnosticsService(connect=sqlite_connect(), budget_seconds=bad)


def test_the_hard_limit_stays_below_the_default_agent_tool_timeout():
    assert TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS < AgentConfig().tool_timeout_seconds
