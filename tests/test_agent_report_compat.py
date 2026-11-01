"""Stage 4.5A (M1): the incomplete-run handling is opt-in. The default (simulation) report behaves exactly as before.

The "unchanged" tests pin the original behaviour explicitly: a model summary and model text are used as the model wrote them,
even when the run is failed or inconclusive. That is the pre-existing behaviour (including its known weakness); this change
deliberately does not alter it for the simulation profile.
"""

from __future__ import annotations

import inspect

from app.agent.graph import GraphDeps
from app.agent.profiles import ECOMMERCE_PROFILE
from app.agent.report import CORE_DIAGNOSTICS, claims_clean, finalize_report
from tests.agent_support import FakeToolbox, ScriptedModel, draft, plan, result_dict, run_graph, tc
from tests.test_ecommerce_incomplete_and_workers import CLEAN_CLAIMS, ECOM_TOOLS, misleading, run_ecom_graph

SIM_CORE = sorted(CORE_DIAGNOSTICS)
SIM_MISLEADING = draft(
    summary=CLEAN_CLAIMS, confidence="high", confidence_explanation="All good, the data is healthy.",
    hypotheses=[{"statement": "The database is healthy and nothing is wrong.", "evidence_ids": ["E1"]}],
    impact={"statement": "No impact: there are no issues.", "evidence_ids": ["E1"]},
    recommended_next_steps=["No action is needed."], limitations=["Nothing is wrong."])


def sim_run(outcomes, report, rounds=2):
    first = [tc(t) for t in SIM_CORE[:3]]
    second = [tc(t) for t in SIM_CORE[3:]]
    plans = [plan(*first), plan(*second)][:rounds]
    return run_graph(ScriptedModel(plans, report), FakeToolbox(outcomes, names=SIM_CORE))


def all_ok():
    return {t: result_dict(t, "ok", "info") for t in SIM_CORE}


def test_the_default_is_off_and_only_the_ecommerce_profile_turns_it_on():
    assert inspect.signature(finalize_report).parameters["code_controlled"].default is False
    assert GraphDeps.__dataclass_fields__["profile"].default is None
    assert ECOMMERCE_PROFILE.code_controlled_summary is True


def test_a_clean_simulation_run_keeps_the_model_summary():
    report = sim_run(all_ok(), draft(summary="All six diagnostics found nothing."))
    assert report.status == "no_issue_detected" and report.summary == "All six diagnostics found nothing."


def test_a_simulation_run_with_an_issue_and_one_failed_tool_is_unchanged():
    outcomes = all_ok()
    outcomes[SIM_CORE[0]] = result_dict(SIM_CORE[0], "issue_detected", "high")
    outcomes[SIM_CORE[1]] = result_dict(SIM_CORE[1], "error", "unknown", error_code="DIAG-DB-ERROR")
    report = sim_run(outcomes, SIM_MISLEADING)
    assert report.status == "issue_identified"
    assert report.summary == CLEAN_CLAIMS  # the model's summary is used as written (original behaviour)
    assert [h.statement for h in report.hypotheses] == ["The database is healthy and nothing is wrong."]
    assert report.impact is not None and "Nothing is wrong." in report.limitations
    assert not any("INCOMPLETE RUN" in n or "read as a clean result" in n for n in report.limitations)


def test_an_inconclusive_simulation_run_is_unchanged():
    outcomes = {SIM_CORE[0]: result_dict(SIM_CORE[0], "ok", "info")}  # most core diagnostics never ran
    report = run_graph(ScriptedModel([plan(tc(SIM_CORE[0]))], SIM_MISLEADING), FakeToolbox(outcomes, names=SIM_CORE))
    assert report.status == "inconclusive"
    assert report.summary == CLEAN_CLAIMS and "No action is needed." in report.recommended_next_steps
    assert not any("INCOMPLETE RUN" in n for n in report.limitations)


def test_a_failed_simulation_run_is_unchanged():
    outcomes = {SIM_CORE[0]: result_dict(SIM_CORE[0], "error", "unknown", error_code="DIAG-DB-ERROR")}
    report = run_graph(ScriptedModel([plan(tc(SIM_CORE[0]))], SIM_MISLEADING), FakeToolbox(outcomes, names=SIM_CORE))
    assert report.status == "failed" and report.summary == CLEAN_CLAIMS
    assert not any("INCOMPLETE RUN" in n for n in report.limitations)


def test_ecommerce_failed_runs_get_a_code_written_headline():
    refused = result_dict("check_ecommerce_target", "error", "unknown", error_code="ECOM-GUARD-REFUSED")
    report = run_ecom_graph(ScriptedModel([plan(tc("check_ecommerce_target"))], misleading()),
                            FakeToolbox({"check_ecommerce_target": refused}, names=ECOM_TOOLS))
    assert report.status == "failed" and report.summary.startswith("FAILED")
    assert "Everything is healthy" not in report.model_dump_json() and not claims_clean(report.summary)


def test_ecommerce_incomplete_runs_get_a_code_written_headline():
    partial = result_dict("check_delivery_timeline", "warning", "low", error_code="ECOM-PARTIAL")
    ok = {t: result_dict(t, "ok", "info") for t in ECOMMERCE_PROFILE.core_diagnostics if t != "check_delivery_timeline"}
    toolbox = FakeToolbox({"run_ecommerce_diagnostics": {"overall_status": "warning", "results": list(ok.values()) + [partial]}},
                          names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("run_ecommerce_diagnostics"))], misleading()), toolbox)
    assert report.status == "inconclusive" and report.summary.startswith("INCONCLUSIVE")
    assert any("INCOMPLETE RUN" in n for n in report.limitations)


def test_the_flag_not_the_wording_decides_the_difference():
    """Same evidence, same misleading model text: only code_controlled changes the outcome."""
    from tests.agent_support import result_dict as rd
    from app.agent.models import EvidenceItem, ReportDraft

    evidence = [EvidenceItem(evidence_id="E1", call_id="E1", tool=SIM_CORE[0], status="error", severity="unknown",
                             summary="x", error_code="DIAG-DB-ERROR")]
    base = dict(investigation_id="X", incident="i", mode="offline", model="m", tool_runs=[], evidence=evidence, limitations=[],
                security_notes=[], rounds_used=1, tool_calls_used=1, stop_reason="done", approved_tools=[],
                draft=ReportDraft.model_validate(SIM_MISLEADING))
    assert finalize_report(**base).summary == CLEAN_CLAIMS
    assert finalize_report(**base, code_controlled=True).summary.startswith("INCONCLUSIVE")
