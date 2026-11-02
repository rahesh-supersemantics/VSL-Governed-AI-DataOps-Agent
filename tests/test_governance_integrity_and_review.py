"""Evidence tamper-detection, claim audit events and INV-14 review flags (offline)."""
import sqlite3

import pytest

from app.agent.models import Finding, RootCause, review_reasons
from app.recovery.approvals import evidence_digest
from app.governance.policy import EvidenceMeta
from tests.recovery_support import approve, fresh_evidence, kinds, make_env, prop, run, start
from tests.test_governance_evidence import _report, evidence_item
from tests.test_governance_idempotency import key_of

INGEST = ("rerun_pipeline", {"pipeline_id": "order_ingestion"})


# ---- INV-14: deterministic review flags (never a promotion, never a clearance)
@pytest.mark.parametrize("kind,text,statuses,expected", [
    ("hypothesis", "The loader is definitely the cause", ["issue_detected"], ["hypothesis_uses_certainty_language"]),
    ("hypothesis", "The loader may be stuck", ["issue_detected"], []),
    ("observed", "Orders are possibly stale", ["issue_detected"], ["observed_uses_hedged_language"]),
    ("observed", "Freshness check reported lag", ["issue_detected"], []),
    ("observed", "Freshness check reported lag", ["error"], ["observed_cites_only_failed_tool_evidence"]),
    ("observed", "Freshness check reported lag", ["error", "ok"], []),
    ("hypothesis", "", [], []),
])
def test_review_reasons(kind, text, statuses, expected):
    assert review_reasons(kind, text, statuses) == expected


def test_a_flagged_finding_is_marked_for_review_and_stays_in_its_list():
    r = _report(evidence=[evidence_item()],
                hypotheses=[Finding(statement="This is confirmed as the root cause", evidence_ids=["E1"])],
                observed_findings=[Finding(statement="It seems stale", evidence_ids=["E1"])])
    assert r.hypotheses[0].kind == "hypothesis" and r.hypotheses[0].review_required
    assert r.observed_findings[0].kind == "observed" and r.observed_findings[0].review_required


def test_a_caller_cannot_pre_clear_the_review_flag():
    r = _report(evidence=[evidence_item()],
                hypotheses=[Finding(statement="Definitely the cause", evidence_ids=["E1"], review_required=False, review_reasons=[])])
    assert r.hypotheses[0].review_required is True


def test_evidence_cited_only_by_hypotheses_cannot_authorise_a_proposal():
    from app.recovery import proposal_from_report
    r = _report(evidence=[evidence_item()], hypotheses=[Finding(statement="maybe", evidence_ids=["E1"])])
    p = proposal_from_report(r, *INGEST)
    assert p.evidence_refs == ()  # only a supported root cause carries evidence into a proposal


def test_malformed_findings_are_rejected():
    with pytest.raises(Exception):
        _report(evidence=[evidence_item()], observed_findings=[Finding(statement="x", evidence_ids=[])])
    with pytest.raises(Exception):
        _report(evidence=[evidence_item()], observed_findings=[Finding(statement="x", evidence_ids=["E9"])])
    with pytest.raises(Exception):
        _report(evidence=[evidence_item()], likely_root_cause=RootCause(status="supported", statement="s", evidence_ids=["E9"], explanation="e"))


# ---- evidence digest
def test_the_digest_is_order_independent_and_changes_with_any_field():
    a = EvidenceMeta("E1", "t", "issue_detected", "2026-11-02T00:00:00+00:00", "INV")
    b = EvidenceMeta("E2", "t", "warning", "2026-11-02T00:00:00+00:00", "INV")
    assert evidence_digest((a, b)) == evidence_digest((b, a))
    for changed in (EvidenceMeta("E1", "t2", a.status, a.collected_at, a.investigation_id), EvidenceMeta("E1", "t", "warning", a.collected_at, a.investigation_id),
                    EvidenceMeta("E1", "t", a.status, "2026-11-02T00:00:01+00:00", a.investigation_id), EvidenceMeta("E1", "t", a.status, a.collected_at, "OTHER")):
        assert evidence_digest((changed, b)) != evidence_digest((a, b))


def test_the_digest_is_recorded_in_the_ledger_when_the_request_is_made(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    rec = env.store.get(out.request_id)
    entry = env.recorder.get_entry(rec.ledger_request_entry)
    assert entry.payload["evidence_digest"] == evidence_digest(env.store.get_evidence(out.request_id)[1])


def _tamper(env, request_id, column, change):
    """Rewrite one column of every stored evidence row with change(old) -> new, directly in the database."""
    import json
    _, meta = env.store.get_evidence(request_id)
    rows = [[m.evidence_id, m.tool, m.status, m.collected_at, m.investigation_id] for m in meta]
    for r in rows:
        r[column] = change(r[column])
    with sqlite3.connect(str(env.store.path)) as c:
        c.execute("UPDATE approval_evidence SET evidence_json=? WHERE request_id=?", (json.dumps(rows), request_id))
    return rows


def _minute_earlier(ts):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(ts) - timedelta(minutes=1)).isoformat()


@pytest.mark.parametrize("column,change", [
    (3, _minute_earlier),                              # still fresh: the evidence policy would accept it
    (1, lambda old: "detect_duplicate_records"),       # another known tool: the evidence policy would accept it
])
def test_altered_stored_evidence_that_would_still_pass_the_policy_blocks_execution(tmp_path, column, change):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)
    before = env.store.get_evidence(out.request_id)[1]
    rows = _tamper(env, out.request_id, column, change)
    assert [list(r) for r in rows] != [[m.evidence_id, m.tool, m.status, m.collected_at, m.investigation_id] for m in before]
    res = run(env.workflow.resume(out.request_id))
    assert res.status != "executed" and "evidence_integrity_failed" in res.reasons
    assert env.store.get(out.request_id).status != "consumed"
    assert "evidence_integrity_failed" in kinds(env)


def test_a_request_with_no_recorded_digest_fails_closed(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)
    with sqlite3.connect(str(env.store.path)) as c:
        c.execute("DELETE FROM approval_evidence WHERE request_id=?", (out.request_id,))
    res = run(env.workflow.resume(out.request_id))
    assert res.status != "executed"


# ---- claim audit trail
def test_claim_acquisition_and_settlement_are_recorded_in_the_ledger(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)
    run(env.workflow.resume(out.request_id))
    ks = kinds(env)
    assert "idempotency_claim_acquired" in ks and ks.index("idempotency_claim_acquired") < ks.index("recovery_succeeded")
    done = [e for e in env.recorder.ledger.store.all_entries() if e.payload.get("outcome") == "recovery_succeeded"][0]
    assert done.payload["idempotency_claim"] == "succeeded"


def test_a_denied_duplicate_is_recorded_with_its_key(tmp_path):
    env = make_env(tmp_path)
    a, b = start(env, prop(*INGEST)), start(env, prop(*INGEST))
    approve(env, a.request_id), approve(env, b.request_id)
    assert env.store.claim_execution(key_of(env, b.request_id), "other", "d", "t")
    run(env.workflow.resume(b.request_id))
    blocked = [e for e in env.recorder.ledger.store.all_entries() if e.payload.get("outcome") == "recovery_blocked"]
    assert any(e.payload.get("idempotency_key") == key_of(env, b.request_id) for e in blocked)
