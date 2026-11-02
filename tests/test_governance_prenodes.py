"""Per-PreNode verdicts come only from recorded ledger entries; 'not recorded' is never a pass. Plus the compiled-VSL inventory."""
import pytest

from app.governance.gates import Gates
from app.governance.policy import build_policy
from app.recovery import RECOVERY_ACTIONS
from app.recovery.prenode_verdicts import PRENODES, SAFE_KEYS, derive_prenode_verdicts
from dashboard.ops_data import governance_panel
from tests.agent_support import apply_scenario
from tests.recovery_support import approve, fresh_evidence, make_env, prop, run, start
from tests.test_governance_acceptance import REPROCESS, SqlLikeRepo, stack

INGEST = ("rerun_pipeline", {"pipeline_id": "order_ingestion"})


def verdicts(env_or_recorder, request_id=None):
    recorder = getattr(env_or_recorder, "recorder", env_or_recorder)
    payloads = [{k: e.payload.get(k) for k in SAFE_KEYS if k in e.payload} for e in recorder.ledger.store.all_entries()]
    return derive_prenode_verdicts(payloads)


def short(v):
    return {pn: x["verdict"] for pn, x in v.items()}


def test_with_no_ledger_entries_every_prenode_is_not_recorded():
    v = derive_prenode_verdicts([])
    assert set(v) == set(PRENODES) and {x["verdict"] for x in v.values()} == {"not_recorded"}


def test_a_pending_request_only_shows_the_stages_that_really_ran(tmp_path):
    env = make_env(tmp_path)
    start(env, prop(*INGEST))
    v = short(verdicts(env))
    assert v["PN-02"] == "passed" and v["PN-03"] == "passed"
    assert all(v[pn] == "not_recorded" for pn in ("PN-01", "PN-04", "PN-05", "PN-06", "PN-07", "PN-08"))


def test_a_verified_success_passes_every_stage_and_marks_which_were_implied(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)
    run(env.workflow.resume(out.request_id))
    v = verdicts(env)
    assert set(short(v).values()) == {"passed"}
    assert v["PN-08"]["basis"] == "recorded" and v["PN-01"]["basis"] == "recorded" and v["PN-05"]["basis"] == "recorded"
    assert v["PN-04"]["basis"] == "implied_by_later_step" and v["PN-07"]["basis"] == "implied_by_later_step"


def test_an_unmet_postcondition_fails_pn08_and_an_unknown_outcome_is_unknown_not_failed_or_passed(tmp_path):
    env = make_env(tmp_path)
    apply_scenario(env.sim, "duplicate-records")
    env.executor.skip_effect = True
    out = start(env, prop(*REPROCESS))
    approve(env, out.request_id)
    run(env.workflow.resume(out.request_id))
    assert verdicts(env)["PN-08"]["verdict"] == "failed"
    env2 = make_env(tmp_path, name="b")
    apply_scenario(env2.sim, "duplicate-records")
    env2.executor.fail_with = RuntimeError("x")
    o2 = start(env2, prop(*REPROCESS))
    approve(env2, o2.request_id)
    run(env2.workflow.resume(o2.request_id))
    assert verdicts(env2)["PN-08"]["verdict"] == "unknown"


def test_stale_evidence_fails_pn02_and_nothing_later_is_claimed(tmp_path):
    env = make_env(tmp_path)
    run(env.workflow.start(prop(*INGEST, evidence=fresh_evidence(("E1", "E2"), status="ok"))))
    v = short(verdicts(env))
    assert v["PN-02"] == "failed" and v["PN-08"] == "not_recorded" and v["PN-04"] == "not_recorded"


def test_a_target_mismatch_fails_pn04_only_and_no_effect_is_implied(tmp_path):
    repo = SqlLikeRepo("dataops_demo_test", live="other_test")
    sim, executor, approvals, workflow, store, recorder = stack(tmp_path, repo, {"dataops_demo_test"})
    apply_scenario(sim, "duplicate-records")
    out = run(workflow.start(prop(*REPROCESS)))
    from tests.recovery_support import TOKENS
    approvals.approve(out.request_id, TOKENS["admin"], "ok")
    run(workflow.resume(out.request_id))
    v = short(verdicts(recorder))
    assert v["PN-04"] == "failed" and v["PN-08"] == "not_recorded" and v["PN-06"] == "not_recorded"


def test_a_duplicate_operation_fails_pn07(tmp_path):
    env = make_env(tmp_path)
    a, b = start(env, prop(*INGEST)), start(env, prop(*INGEST))
    approve(env, a.request_id), approve(env, b.request_id)
    assert env.store.claim_execution(__import__("tests.test_governance_idempotency", fromlist=["key_of"]).key_of(env, b.request_id), "other", "d", "t")
    run(env.workflow.resume(b.request_id))
    assert verdicts(env)["PN-07"]["verdict"] == "failed"


def test_a_refused_reviewer_fails_pn01_until_someone_authorised_decides(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    with pytest.raises(Exception):
        env.approvals.approve(out.request_id, "forged", "x")
    assert verdicts(env)["PN-01"]["verdict"] == "failed"
    approve(env, out.request_id)
    assert verdicts(env)["PN-01"]["verdict"] == "passed"


def test_the_panel_shows_unrecorded_stages_as_not_recorded_never_as_passed():
    view = {"prenodes": derive_prenode_verdicts([])}
    rows = {label: (kind, text) for label, kind, text in governance_panel(view) if label.startswith("PN-")}
    assert len(rows) == 8 and all(kind == "info" and "NOT RECORDED" in text and "not a pass" in text for kind, text in rows.values())
    ok = {label: kind for label, kind, text in governance_panel({"prenodes": derive_prenode_verdicts([{"outcome": "recovery_succeeded"}])})}
    assert ok["PN-08"] == "success" and ok["PN-01"] == "info"


def test_the_governance_view_carries_the_verdicts_from_the_real_backend(tmp_path):
    from app.recovery.governance_view import build_governance_view
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    payloads = [{k: e.payload.get(k) for k in SAFE_KEYS if k in e.payload} for e in env.recorder.ledger.store.all_entries()]
    view = build_governance_view({"recovery_id": "r", "status": "awaiting_approval", "action_type": INGEST[0], "parameters": INGEST[1]}, None, [], None, payloads)
    assert view["prenodes"]["PN-02"]["verdict"] == "passed" and view["prenodes"]["PN-08"]["verdict"] == "not_recorded"


# ---- compiled VSL inventory: guard against labelling ordinary Python stages as compiled gates
def test_exactly_three_compiled_vsl_constructs_exist_and_they_are_the_documented_ones():
    from dataclasses import fields
    assert [f.name for f in fields(Gates)] == ["suspension", "allowlist", "prenode"]
    p = build_policy(RECOVERY_ACTIONS)
    assert (p.suspension_invariant.name, p.allowlist_invariant.name, p.pre_node.name) == (
        "no_execution_while_suspended", "action_within_allowlist", "verified_report_and_approval")


def test_only_catalogue_entries_enforced_inside_the_governance_package_are_labelled_vsl_gate():
    from app.recovery.governance_catalogue import INVARIANTS, PRENODES
    for e in PRENODES + INVARIANTS:
        in_vsl = any(ref.startswith("app.governance.") for ref in e.enforcement)
        assert (e.layer == "vsl_gate") == in_vsl or e.layer == "code", e.id
        if e.layer == "vsl_gate":
            assert in_vsl, f"{e.id} is labelled a compiled VSL gate but names no app.governance enforcement point"


def test_the_compiled_prenode_denies_bad_evidence_through_the_gate_path(tmp_path):
    """The monitor of the compiled PreNode reports FAIL; the VSL runtime (not a Python if) routes the proposal to human review."""
    env = make_env(tmp_path)
    out = run(env.workflow.start(prop(*INGEST, evidence=fresh_evidence(("E1", "E2"), status="error"))))
    entries = list(env.recorder.ledger.store.all_entries())
    assert out.status == "denied_human_review"
    assert any(e.entry_type.value == "PRE_NODE" and "evidence_does_not_support_action" in (e.payload.get("failed") or []) for e in entries)
