"""Idempotency: a stable, derived key per operation and an atomic execution claim. Offline."""
import threading
import time

import pytest

from app.recovery import ApprovalDenied
from app.recovery.approvals import idempotency_key
from tests.agent_support import apply_scenario
from tests.recovery_support import TOKENS, approve, kinds, make_env, prop, run, start

REPROCESS = ("remove_injected_condition", {"scenario_key": "duplicate-records"})
INGEST = ("rerun_pipeline", {"pipeline_id": "order_ingestion"})


def key_of(env, request_id):
    r = env.store.get(request_id)
    return idempotency_key(r.incident_id, r.action_type, r.parameters, r.target)


# ---- the key
def test_the_key_is_stable_and_bound_to_incident_action_parameters_and_target():
    base = idempotency_key("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"}, "pipeline:order_ingestion")
    assert base == idempotency_key("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"}, "pipeline:order_ingestion")
    assert base.startswith("idem-") and len(base) == 45
    for variant in (idempotency_key("INV-2", "rerun_pipeline", {"pipeline_id": "order_ingestion"}, "pipeline:order_ingestion"),
                    idempotency_key("INV-1", "remove_injected_condition", {"pipeline_id": "order_ingestion"}, "pipeline:order_ingestion"),
                    idempotency_key("INV-1", "rerun_pipeline", {"pipeline_id": "order_validation"}, "pipeline:order_ingestion"),
                    idempotency_key("INV-1", "rerun_pipeline", {"pipeline_id": "order_ingestion"}, "pipeline:other")):
        assert variant != base


def test_the_key_ignores_request_id_evidence_and_parameter_order(tmp_path):
    env = make_env(tmp_path)
    a = start(env, prop(*INGEST))
    b = start(env, prop(*INGEST, refs=("E2",), reason="different text"))
    assert a.request_id != b.request_id and key_of(env, a.request_id) == key_of(env, b.request_id)
    assert idempotency_key("I", "a", {"x": 1, "y": 2}, "t") == idempotency_key("I", "a", {"y": 2, "x": 1}, "t")


def test_the_key_is_recorded_in_the_ledger_with_the_request(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    entry = next(e for e in env.recorder.ledger.store.all_entries() if e.payload.get("outcome") == "approval_requested")
    assert entry.payload["idempotency_key"] == key_of(env, out.request_id)


# ---- concurrent duplicates
def hold_until_released(env, entered, gate):
    """Make the executor block, inside the claimed section, until the test releases it (no timing assumptions)."""
    real = env.executor.execute

    def execute(action, facts):
        entered.append(threading.get_ident())
        assert gate.wait(30), "the test never released the executor"
        return real(action, facts)
    env.executor.execute = execute


def test_two_approvals_for_the_same_operation_run_it_exactly_once_when_resumed_concurrently(tmp_path):
    """Deterministic overlap: whichever resume wins the claim is held INSIDE the executor until the other resume has finished,
    so the two can never run one after the other by luck. (A second approved request that arrives AFTER the first has settled
    is a deliberate rerun and is allowed: test_a_success_settles_the_claim_and_a_later_deliberate_rerun_is_allowed.)"""
    env = make_env(tmp_path)
    first, second = start(env, prop(*INGEST)), start(env, prop(*INGEST))
    approve(env, first.request_id), approve(env, second.request_id)
    twins = [make_env(tmp_path, name="a", sim=env.sim) for _ in range(2)]      # separate objects, same ledger and approval database
    entered, gate = [], threading.Event()
    for t in twins:
        hold_until_released(t, entered, gate)
    barrier, results = threading.Barrier(2), {}

    def go(i, w, rid):
        barrier.wait()
        results[i] = run(w.workflow.resume(rid))

    threads = [threading.Thread(target=go, args=(i, w, r), daemon=True)
               for i, (w, r) in enumerate(zip(twins, (first.request_id, second.request_id)))]
    [t.start() for t in threads]
    try:
        deadline = time.monotonic() + 30
        # Wait until one resume has finished (the refused one) while the other is still held inside the executor, or until a
        # second execution has started (the defect), whichever happens first.
        while time.monotonic() < deadline and len(entered) < 2 and sum(not t.is_alive() for t in threads) < 1:
            time.sleep(0.01)
        assert len(entered) <= 1, "both resumes reached the executor"
    finally:
        gate.set()
        [t.join(30) for t in threads]
    assert sum(w.executor.count for w in twins) == 1
    winner = [r for r in results.values() if r.executed]
    loser = [r for r in results.values() if not r.executed]
    assert len(winner) == 1 and len(loser) == 1
    assert "duplicate_or_unreconciled_operation" in loser[0].reasons
    spent = sorted(env.store.get(r).status for r in (first.request_id, second.request_id))
    assert spent == ["approved", "consumed"]                         # the loser's approval was NOT spent
    assert sorted(env.store.claim_state(r) or "none" for r in (first.request_id, second.request_id)) == ["none", "succeeded"]


def test_a_resume_that_loses_the_race_can_be_retried_after_the_winner_settles_only_as_a_new_approved_rerun(tmp_path):
    """Sequential replays: the spent approval is refused; the unspent one is a separate deliberate request."""
    env = make_env(tmp_path)
    one, two = start(env, prop(*INGEST)), start(env, prop(*INGEST))
    approve(env, one.request_id), approve(env, two.request_id)
    assert run(env.workflow.resume(one.request_id)).executed
    again = run(env.workflow.resume(one.request_id))
    assert not again.executed and env.executor.count == 1              # replaying a spent approval never re-executes
    assert env.store.get(two.request_id).status == "approved"           # still valid and unspent; it is its own rerun decision


def test_different_operations_do_not_block_each_other(tmp_path):
    env = make_env(tmp_path)
    a = start(env, prop(*INGEST, incident="INV-A"))
    b = start(env, prop(*INGEST, incident="INV-B"))
    c = start(env, prop("rerun_pipeline", {"pipeline_id": "order_validation"}, incident="INV-A"))
    for r in (a, b, c):
        approve(env, r.request_id)
        assert run(env.workflow.resume(r.request_id)).executed
    assert env.executor.count == 3


# ---- claim lifecycle
def test_a_success_settles_the_claim_and_a_later_deliberate_rerun_is_allowed(tmp_path):
    env = make_env(tmp_path)
    one = start(env, prop(*INGEST))
    approve(env, one.request_id)
    assert run(env.workflow.resume(one.request_id)).executed and env.store.claim_state(one.request_id) == "succeeded"
    two = start(env, prop(*INGEST))                                   # a new request needs its own human approval
    approve(env, two.request_id)
    assert run(env.workflow.resume(two.request_id)).executed and env.executor.count == 2


def test_a_precondition_failure_takes_no_claim_and_a_failed_verification_does_not_block(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*REPROCESS))                                 # scenario not active: precondition fails
    approve(env, out.request_id)
    assert not run(env.workflow.resume(out.request_id)).executed and env.store.claim_state(out.request_id) is None   # refused before any claim
    apply_scenario(env.sim, "duplicate-records")
    env.executor.skip_effect = True
    bad = start(env, prop(*REPROCESS))
    approve(env, bad.request_id)
    assert run(env.workflow.resume(bad.request_id)).execution_status == "verification_failed"
    assert env.store.claim_state(bad.request_id) == "failed"
    env.executor.skip_effect = False
    again = start(env, prop(*REPROCESS))
    approve(env, again.request_id)
    assert run(env.workflow.resume(again.request_id)).executed


def test_an_uncertain_outcome_blocks_the_same_operation_until_a_person_reconciles_it(tmp_path):
    env = make_env(tmp_path)
    apply_scenario(env.sim, "duplicate-records")
    env.executor.fail_with = RuntimeError("boom")
    first = start(env, prop(*REPROCESS))
    approve(env, first.request_id)
    assert run(env.workflow.resume(first.request_id)).execution_status == "uncertain"
    assert env.store.claim_state(first.request_id) == "uncertain"
    env.executor.fail_with = None
    apply_scenario(env.sim, "duplicate-records") if "duplicate-records" not in env.sim.active_scenario_ids() else None
    retry = start(env, prop(*REPROCESS))
    approve(env, retry.request_id)
    blocked = run(env.workflow.resume(retry.request_id))
    assert not blocked.executed and "duplicate_or_unreconciled_operation" in blocked.reasons
    assert env.store.get(retry.request_id).status == "approved"       # unspent
    count_before = env.executor.count
    # who may reconcile
    with pytest.raises(ApprovalDenied) as viewer:
        env.approvals.reconcile_uncertain(first.request_id, TOKENS["viewer"], "looked")
    assert viewer.value.code == "not_permitted"
    with pytest.raises(ApprovalDenied) as nobody:
        env.approvals.reconcile_uncertain(first.request_id, "forged", "looked")
    assert nobody.value.code == "unauthenticated"
    env.approvals.reconcile_uncertain(first.request_id, TOKENS["admin"], "I checked the real state")
    assert "idempotency_claim_reconciled" in kinds(env)
    with pytest.raises(ApprovalDenied) as twice:
        env.approvals.reconcile_uncertain(first.request_id, TOKENS["admin"], "again")
    assert twice.value.code == "not_uncertain"
    assert run(env.workflow.resume(retry.request_id)).executed and env.executor.count == count_before + 1


def test_a_claim_left_open_by_a_crash_is_marked_uncertain_by_reconcile(tmp_path):
    from app.services.recoveries import RecoveryService
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)
    env.approvals.consume(out.request_id, "dec-x", env.store.get(out.request_id).binding_digest)
    assert env.store.claim_execution(key_of(env, out.request_id), out.request_id, "dec-x", "2026-11-02T00:00:00+00:00")
    done = []

    class Store:
        def record_recovery_outcome(self, rid, **kw):
            done.append(kw)

        def get_recovery(self, rid):
            return {"recovery_id": rid}

    svc = RecoveryService.__new__(RecoveryService)
    svc.approvals, svc.recorder, svc.store, svc._inflight = env.approvals, env.recorder, Store(), set()
    svc.reconcile({"recovery_id": "R", "status": "resuming", "approval_request_id": out.request_id, "decision_id": "dec-x", "ledger_entry_ids": []})
    assert done[0]["status"] == "recovery_uncertain" and env.store.claim_state(out.request_id) == "uncertain"


def test_the_claim_store_refuses_a_second_open_claim_and_unknown_states(tmp_path):
    env = make_env(tmp_path)
    assert env.store.claim_execution("idem-k", "r1", "d1", "t")
    assert not env.store.claim_execution("idem-k", "r2", "d2", "t")          # open: refused
    assert env.store.finish_claim("r1", "succeeded", "t") and not env.store.finish_claim("r1", "failed", "t")   # only an open claim changes
    assert env.store.claim_execution("idem-k", "r3", "d3", "t")              # settled: a new claim is allowed
    with pytest.raises(ValueError):
        env.store.finish_claim("r3", "bogus", "t")


def test_an_approval_that_cannot_be_spent_releases_the_claim_so_nothing_is_left_open(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop(*INGEST))
    approve(env, out.request_id)

    def refuse(*a, **k):
        raise ApprovalDenied("expired")

    env.approvals.consume = refuse
    done = run(env.workflow.resume(out.request_id))
    assert not done.executed and env.executor.count == 0
    assert env.store.claim_state(out.request_id) == "released" and env.store.open_claim_for_key(key_of(env, out.request_id)) is None
