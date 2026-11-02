"""The governance view separates policy from person, plan from execution, attempt from verified success, and is read-only."""
import pytest

from app.recovery.governance_view import build_governance_view
from tests.api_support import api  # noqa: F401  (fixture)
from tests.test_api_governance import decide, pending


def view(e, rec_id, role="reader"):
    r = e.get(f"/recoveries/{rec_id}/governance", role)
    assert r.status_code == 200, r.text
    return r.json()


def test_pending_request_shows_no_authorisation_no_attempt_and_no_plan(api):
    e = api()
    _, rec, _ = pending(e)
    v = view(e, rec["recovery_id"])
    assert v["lifecycle"]["state"] == "PendingReview" and v["authorisation"]["kind"] == "none"
    assert v["execution"] == {"attempted": False, "verified_success": False, "note": "Not attempted."}
    assert v["compensation"]["plan"] is None and v["compensation"]["executed"] is False and v["escalated"] is False
    assert e.env.executor.count == 0


def test_human_approval_is_labelled_human_and_does_not_imply_execution(api):
    e = api()
    _, rec, req = pending(e)
    e.env.approvals.approve(req, __import__("tests.recovery_support", fromlist=["TOKENS"]).TOKENS["admin"], "ok")
    v = view(e, rec["recovery_id"])
    assert v["lifecycle"]["state"] == "Authorized" and v["authorisation"]["kind"] == "human"
    assert v["execution"]["attempted"] is False and e.env.executor.count == 0


def test_verified_success_is_distinct_from_the_attempt(api):
    e = api()
    _, rec, req = pending(e)
    assert decide(e, req).status_code == 200
    v = view(e, rec["recovery_id"])
    assert v["lifecycle"]["state"] == "Succeeded" and v["execution"]["attempted"] and v["execution"]["verified_success"]
    assert v["authorisation"]["kind"] == "human" and v["escalated"] is False


@pytest.mark.parametrize("fault,state", [("fail_with", "Escalated"), ("skip_effect", "Escalated")])
def test_failed_or_unknown_outcome_shows_attempt_without_verification_and_a_plan_that_did_not_run(api, fault, state):
    e = api()
    _, rec, req = pending(e)
    setattr(e.env.executor, fault, RuntimeError("simulated") if fault == "fail_with" else True)
    decide(e, req)
    v = view(e, rec["recovery_id"])
    assert v["lifecycle"]["state"] == state and v["escalated"] is True
    assert v["execution"]["attempted"] is True and v["execution"]["verified_success"] is False
    assert v["compensation"]["executed"] is False and v["compensation"]["plan"]["executable"] is False
    assert "simulated" not in str(v)


def test_unknown_recovery_is_404_and_the_view_needs_the_read_permission(api):
    e = api()
    assert e.get("/recoveries/nope/governance", "reader").status_code == 404
    _, rec, _ = pending(e)
    assert e.get(f"/recoveries/{rec['recovery_id']}/governance", None).status_code in (401, 403)


def test_the_view_is_get_only(api):
    e = api()
    _, rec, _ = pending(e)
    assert e.post(f"/recoveries/{rec['recovery_id']}/governance", "admin").status_code == 405


# pure-function cases the API cannot easily produce
def test_policy_authorisation_is_labelled_policy_and_never_human():
    rec = {"recovery_id": "r", "status": "executed", "action_type": "rerun_pipeline", "parameters": {"pipeline_id": "order_validation"}}
    appr = {"status": "consumed", "decided_by": "policy:risk-policy-v1"}
    v = build_governance_view(rec, appr, [{"outcome": "approval_granted_by_policy"}, {"outcome": "recovery_attempt"}, {"outcome": "recovery_succeeded"}])
    assert v["authorisation"]["kind"] == "policy" and "No person" in v["authorisation"]["note"]


def test_a_success_label_needs_both_the_stored_status_and_the_ledger_entry():
    rec = {"recovery_id": "r", "status": "executed", "action_type": "rerun_pipeline", "parameters": {"pipeline_id": "order_validation"}}
    assert build_governance_view(rec, {"status": "consumed", "decided_by": "a"}, [{"outcome": "recovery_attempt"}])["execution"]["verified_success"] is False
    rec2 = dict(rec, status="recovery_uncertain")
    assert build_governance_view(rec2, None, [{"outcome": "recovery_attempt"}, {"outcome": "recovery_succeeded"}])["execution"]["verified_success"] is False


# ---- dashboard panel rows come only from the backend view and keep the distinctions
def test_dashboard_panel_labels_policy_plan_attempt_and_escalation_separately():
    from dashboard.ops_data import governance_panel
    view = {"lifecycle": {"state": "Escalated", "exactness": "derived", "stored_status": "recovery_verification_failed", "note": "x"},
            "authorisation": {"kind": "policy", "by": "policy:risk-policy-v1", "note": "Authorised by a versioned policy. No person approved this."},
            "execution": {"attempted": True, "verified_success": False, "note": "n"},
            "compensation": {"plan": {"strategy": "S"}, "executed": False, "note": "n"}, "escalated": True}
    rows = {label: (kind, text) for label, kind, text in governance_panel(view)}
    assert "POLICY" in rows["Authorisation"][1] and "PERSON" not in rows["Authorisation"][1].replace("No person", "")
    assert "NOT verified" in rows["Execution"][1] and rows["Execution"][0] == "error"
    assert "PROPOSED only, NOT executed" in rows["Compensation"][1] and "Escalated" in rows["Escalation"][1]


def test_dashboard_panel_never_shows_success_for_an_empty_or_partial_view():
    from dashboard.ops_data import governance_panel
    for view in ({}, {"execution": {}}, {"execution": {"attempted": True}}):
        rows = {label: (kind, text) for label, kind, text in governance_panel(view)}
        assert rows["Execution"][0] != "success" and "VERIFIED" not in rows["Execution"][1].replace("NOT verified", "")
        assert "NOT authorised" in rows["Authorisation"][1]


# ---- the real Streamlit page, real backend
def test_the_recovery_page_shows_backend_governance_and_executes_nothing(api):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from dashboard import runtime
    from dashboard.ops_client import OpsClient
    from tests.api_support import API_TOKENS
    from tests.test_phase10_dashboard_app import APP, go, text_of
    e = api()
    iid = e.new_incident()["incident_id"]
    e.investigate(iid)
    assert e.request_recovery(iid).status_code == 202
    runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS["viewer"], http=e.client))
    try:
        at = AppTest.from_file(APP, default_timeout=30)
        go(at, "Recovery & approvals")
        text = text_of(at)
        assert "Governance" in text and "NOT authorised" in text and "NOT attempted" in text and "PendingReview" in text
        assert "VERIFIED" not in text.replace("NOT verified", "") and e.env.executor.count == 0
    finally:
        runtime.set_override("ops", None)


def test_the_view_reports_the_analysis_mode_separates_facts_from_hypotheses_and_shows_the_request(api):
    e = api()
    _, rec, _ = pending(e)
    v = view(e, rec["recovery_id"])
    assert v["analysis"]["mode"] == "offline" and "no model was called" in v["analysis"]["note"]
    assert isinstance(v["analysis"]["observed_findings"], int) and isinstance(v["analysis"]["hypotheses"], int)
    assert v["request"]["risk"] and v["request"]["target"] and v["request"]["evidence_refs"]
    assert v["timestamps"]["created_at"] and v["timestamps"]["completed_at"] is None


def test_the_panel_shows_missing_data_as_missing_not_as_success():
    from dashboard.ops_data import governance_panel
    rows = {label: text for label, _, text in governance_panel({"analysis": {"mode": None, "note": "Analysis mode not recorded."}})}
    assert "not recorded" in rows["Analysis"] and "not started" in rows["Timestamps"]
