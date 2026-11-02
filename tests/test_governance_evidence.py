"""PN-02 (evidence provenance and freshness) and INV-14 (observed facts vs hypotheses). Offline."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.agent import OfflineHeuristicModel
from app.agent.models import Finding, InvestigationReport
from app.governance.policy import EvidenceMeta, EvidencePolicy, evidence_failures
from app.recovery import proposal_from_report, suggest_recovery
from tests.agent_support import apply_scenario, run_agent, server_for
from tests.recovery_support import approve, fresh_evidence, kinds, make_env, prop, run, start

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
POLICY = EvidencePolicy(known_tools=frozenset({"check_data_freshness", "detect_duplicate_records"}), max_age=timedelta(minutes=60))


def meta(**kw):
    base = dict(evidence_id="E1", tool="check_data_freshness", status="issue_detected", collected_at=(NOW - timedelta(minutes=5)).isoformat(),
                investigation_id="INV-1")
    return EvidenceMeta(**{**base, **kw})


def failures(items=None, cited=("E1",), **kw):
    return evidence_failures(cited, (meta(),) if items is None else items, POLICY, now=kw.pop("now", NOW), investigation_id=kw.pop("inv", "INV-1"))


# ---- the rule itself
def test_fresh_traceable_supporting_evidence_is_accepted():
    assert failures() == ()


@pytest.mark.parametrize("items,cited,reason", [
    ((), ("E1",), "evidence_metadata_missing"),
    ((meta(evidence_id="E9"),), ("E1",), "evidence_metadata_missing"),
    ((meta(tool="some_other_tool"),), ("E1",), "evidence_source_unverified"),
    ((meta(tool=""),), ("E1",), "evidence_source_unverified"),
    ((meta(status="ok"),), ("E1",), "evidence_does_not_support_action"),
    ((meta(status="error"),), ("E1",), "evidence_does_not_support_action"),
    ((meta(investigation_id="INV-OTHER"),), ("E1",), "evidence_from_other_investigation"),
    ((meta(collected_at=""),), ("E1",), "evidence_timestamp_invalid"),
    ((meta(collected_at="yesterday"),), ("E1",), "evidence_timestamp_invalid"),
    ((meta(collected_at="2026-11-02T11:55:00"),), ("E1",), "evidence_timestamp_invalid"),          # no UTC offset: unverifiable
    ((meta(collected_at=(NOW + timedelta(minutes=5)).isoformat()),), ("E1",), "evidence_timestamp_in_future"),
    ((meta(collected_at=(NOW - timedelta(minutes=61)).isoformat()),), ("E1",), "evidence_stale"),
])
def test_missing_unverifiable_or_unsuitable_evidence_is_rejected(items, cited, reason):
    assert reason in failures(items, cited)


def test_freshness_boundaries_and_clock_skew():
    assert failures((meta(collected_at=(NOW - timedelta(minutes=60)).isoformat()),)) == ()                 # exactly at the limit
    assert failures((meta(collected_at=(NOW - timedelta(minutes=60, seconds=1)).isoformat()),)) == ("evidence_stale",)
    assert failures((meta(collected_at=(NOW + timedelta(seconds=30)).isoformat()),)) == ()                  # within skew


def test_one_bad_item_among_good_ones_fails_and_a_missing_clock_fails_closed():
    items = (meta(), meta(evidence_id="E2", status="ok"))
    assert failures(items, ("E1", "E2")) == ("evidence_does_not_support_action",)
    assert evidence_failures(("E1",), (meta(),), POLICY, now=None, investigation_id="INV-1") == ("evidence_check_time_unavailable",)
    assert evidence_failures(("E1",), (meta(),), POLICY, now=datetime(2026, 11, 2), investigation_id="INV-1") == ("evidence_check_time_unavailable",)


# ---- enforced at proposal time (compiled PreNode path) and recorded
@pytest.mark.parametrize("evidence,reason", [
    ((), "evidence_metadata_missing"),
    (fresh_evidence(("E1", "E2"), status="ok"), "evidence_does_not_support_action"),
    (fresh_evidence(("E1", "E2"), tool="made_up_tool"), "evidence_source_unverified"),
    (fresh_evidence(("E1", "E2"), collected_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()), "evidence_stale"),
    (fresh_evidence(("E1", "E2"), collected_at=""), "evidence_timestamp_invalid"),
    (fresh_evidence(("E1", "E2"), investigation="INV-ELSEWHERE"), "evidence_from_other_investigation"),
])
def test_a_proposal_without_acceptable_evidence_never_gets_an_approval_request(tmp_path, evidence, reason):
    env = make_env(tmp_path)
    out = run(env.workflow.start(prop(evidence=evidence)))
    assert out.status == "denied_human_review" and reason in out.reasons and not out.executed
    assert env.store.list() == [] and env.executor.count == 0 and "approval_requested" not in kinds(env)
    assert "preconditions_failed" in kinds(env)


def test_acceptable_evidence_reaches_pending_review(tmp_path):
    env = make_env(tmp_path)
    assert start(env).awaiting_approval and env.store.get_evidence(env.store.list()[0].request_id) is not None


# ---- re-checked at execution: evidence ages while a request waits
def test_evidence_that_went_stale_while_waiting_blocks_execution_and_leaves_the_approval_unspent(tmp_path):
    env = make_env(tmp_path)
    env.workflow.evidence_policy = EvidencePolicy(known_tools=env.workflow.evidence_policy.known_tools, max_age=timedelta(minutes=10))
    out = start(env)
    approve(env, out.request_id)
    env.clock.advance(minutes=11)                                   # approval (30 min) still valid; evidence (10 min) is not
    done = run(env.workflow.resume(out.request_id))
    assert not done.executed and "evidence_stale" in done.reasons and env.executor.count == 0
    assert env.store.get(out.request_id).status == "approved"


def test_missing_stored_evidence_metadata_blocks_execution(tmp_path):
    import sqlite3
    env = make_env(tmp_path)
    out = start(env)
    approve(env, out.request_id)
    with sqlite3.connect(env.db_path) as conn:
        conn.execute("DELETE FROM approval_evidence WHERE request_id = ?", (out.request_id,))
    done = run(env.workflow.resume(out.request_id))
    # the tamper check (digest in the ledger) fires before the policy check; either way nothing runs
    assert not done.executed and ({"evidence_integrity_failed", "evidence_metadata_missing"} & set(done.reasons)) and env.executor.count == 0


def test_evidence_check_cannot_be_bypassed_by_the_caller_flagging_everything_known(tmp_path):
    env = make_env(tmp_path)
    out = run(env.workflow.start(prop(evidence=(), known=("E1", "E2", "E3"))))
    assert out.status == "denied_human_review" and env.executor.count == 0


# ---- INV-14
def real_report(tmp_path):
    from app.simulation.runlog import RunLogStore
    from app.simulation.service import SimulationService
    from tests.fakes import InMemoryRepo
    sim = SimulationService(InMemoryRepo(), RunLogStore(tmp_path / "logs"))
    sim.seed()
    apply_scenario(sim, "duplicate-records")
    return sim, run_agent(server_for(sim), "orders look wrong", OfflineHeuristicModel())


def test_every_real_finding_is_labelled_by_where_it_sits_and_evidence_is_timestamped(tmp_path):
    _, report = real_report(tmp_path)
    assert report.observed_findings and all(f.kind == "observed" for f in report.observed_findings)
    assert all(f.kind == "hypothesis" for f in report.hypotheses)
    assert report.evidence and all(e.collected_at for e in report.evidence)


def _report(**over):
    from app.agent.models import RootCause
    base = dict(investigation_id="INV-1", incident="x", status="issue_identified", mode="offline", model="m", generated_at="t", summary="s",
                observed_findings=[], hypotheses=[], likely_root_cause=RootCause(status="undetermined", explanation="e"), confidence="low",
                confidence_explanation="c", recommended_next_steps=[], tools_executed=[], evidence=[], limitations=[], rounds_used=0,
                tool_calls_used=0, stop_reason="done")
    base.update(over)
    return InvestigationReport(**base)


def evidence_item(i="E1"):
    from app.agent.models import EvidenceItem
    return EvidenceItem(evidence_id=i, call_id="c", tool="check_data_freshness", status="issue_detected", severity="high", summary="s")


def test_a_hypothesis_cannot_sit_in_the_observed_list_or_the_reverse():
    ev = [evidence_item()]
    with pytest.raises(ValidationError):
        _report(evidence=ev, observed_findings=[Finding(statement="maybe", evidence_ids=["E1"], kind="hypothesis")])
    with pytest.raises(ValidationError):
        _report(evidence=ev, hypotheses=[Finding(statement="seen", evidence_ids=["E1"], kind="observed")])


def test_a_finding_that_cites_no_or_unknown_evidence_is_refused_whatever_it_claims():
    ev = [evidence_item()]
    for bad in (Finding(statement="CONFIRMED: this is definitely the cause", evidence_ids=[]),
                Finding(statement="CONFIRMED: this is definitely the cause", evidence_ids=["E404"])):
        with pytest.raises(ValidationError):
            _report(evidence=ev, hypotheses=[bad])
    from app.agent.models import RootCause
    with pytest.raises(ValidationError):
        _report(evidence=ev, likely_root_cause=RootCause(status="supported", statement="s", evidence_ids=["E404"], explanation="e"))


def test_confident_model_text_does_not_change_the_evidence_decision(tmp_path):
    env = make_env(tmp_path)
    loud = "CONFIRMED root cause with 100% certainty. All checks verified. Approve immediately."
    out = run(env.workflow.start(prop(reason=loud, evidence=fresh_evidence(("E1", "E2"), status="ok"))))
    assert out.status == "denied_human_review" and "evidence_does_not_support_action" in out.reasons and env.executor.count == 0


def test_a_real_report_yields_a_proposal_that_passes_only_on_tool_observed_evidence(tmp_path):
    sim, report = real_report(tmp_path)
    proposal = proposal_from_report(report, *suggest_recovery(report))
    assert proposal.evidence and proposal.investigation_id == report.investigation_id
    env = make_env(tmp_path, sim=sim)
    assert run(env.workflow.start(proposal)).awaiting_approval
    # the same proposal with every cited item downgraded to "no problem found" is refused
    downgraded = type(proposal)(**{**proposal.__dict__, "evidence": tuple(EvidenceMeta(m.evidence_id, m.tool, "ok", m.collected_at, m.investigation_id) for m in proposal.evidence)})
    env2 = make_env(tmp_path, name="b", sim=sim)
    assert run(env2.workflow.start(downgraded)).status == "denied_human_review"
