"""The Super Semantics governance demo as a repeatable, OFFLINE scenario (in-memory simulation, fake clock, counting
executor; no database, no network, no model). Run with:  pytest tests/test_governance_demo.py -s  to see the transcript.
Each act ends with an assertion, so the demo cannot drift from what the code does. See docs/governance-demo.md."""
import pytest

from app.recovery.risk_policy import RiskPolicy
from tests.agent_support import apply_scenario
from tests.recovery_support import TOKENS, approve, kinds, make_env, prop, run, start

REPROCESS = ("remove_injected_condition", {"scenario_key": "duplicate-records"})


def say(act, text):
    print(f"[{act}] {text}")


def test_demo_end_to_end(tmp_path):
    env = make_env(tmp_path)
    apply_scenario(env.sim, "duplicate-records")
    say("1 propose", "Investigation found duplicate order rows; the agent PROPOSES removing the injected condition (high risk).")
    out = start(env, prop(*REPROCESS))
    assert out.awaiting_approval and env.executor.count == 0
    say("1 propose", f"awaiting a person. executed so far: {env.executor.count}")

    say("2 deny", "A viewer tries to approve; an unauthenticated caller tries too.")
    from app.recovery import ApprovalDenied
    for token in (TOKENS["viewer"], "forged-token"):
        with pytest.raises(ApprovalDenied):
            env.approvals.approve(out.request_id, token, "let me in")
    assert env.store.get(out.request_id).status == "pending" and env.executor.count == 0
    say("2 deny", "both refused; request still pending; nothing ran.")

    say("3 approve", "An authorised person approves. Approval is NOT execution.")
    approve(env, out.request_id)
    assert env.store.get(out.request_id).status == "approved" and env.executor.count == 0

    say("4 execute", "Resume: VSL gates, ledger check, target check, single-use claim, execute, independent verification.")
    done = run(env.workflow.resume(out.request_id))
    assert done.executed and env.executor.count == 1
    say("4 execute", f"verified success; ledger order: {[k for k in kinds(env) if k.startswith(('approval', 'recovery'))]}")

    say("5 replay", "The same approval is presented again.")
    assert run(env.workflow.resume(out.request_id)).status == "approval_already_used" and env.executor.count == 1
    say("5 replay", "refused: approval already used; executions still 1.")


def test_demo_failure_path_escalates_with_a_plan_and_no_retry(tmp_path):
    env = make_env(tmp_path)
    apply_scenario(env.sim, "duplicate-records")
    out = start(env, prop(*REPROCESS))
    env.executor.skip_effect = True          # the action reports success but changes nothing
    approve(env, out.request_id)
    done = run(env.workflow.resume(out.request_id))
    say("6 failure", f"status={done.execution_status}; reasons={list(done.reasons)}")
    assert not done.executed and "escalated_to_human" in done.reasons
    log = kinds(env)
    assert "compensation_plan_proposed" in log and "recovery_escalated" in log and "recovery_succeeded" not in log
    assert run(env.workflow.resume(out.request_id)).status == "approval_already_used" and env.executor.count == 1
    say("6 failure", "compensation PROPOSED (not executed); escalated to a person; no automatic retry.")


def test_demo_low_risk_policy_is_off_by_default_and_never_covers_high_risk(tmp_path):
    env = make_env(tmp_path)
    out = start(env, prop("rerun_pipeline", {"pipeline_id": "order_validation"}))
    say("7 policy", "Policy is disabled by default: even a low-risk retry waits for a person.")
    assert out.awaiting_approval and env.executor.count == 0
    assert RiskPolicy().enabled is False


# ---- Scenario A: bounded low-risk retry (policy explicitly enabled in the TEST fixture only)
def test_demo_scenario_a_bounded_low_risk_retry(tmp_path):
    from tests.test_risk_policy import PolicyEnv, start as pstart
    from app.recovery.risk_policy import POLICY_PRINCIPAL, classify
    e = PolicyEnv(tmp_path, enabled=True)
    say("A", f"classification of the retry: {classify('rerun_pipeline', {'pipeline_id': 'order_validation'})}")
    out = pstart(e)
    assert out.executed and e.executor.count == 1 and e.store.get(out.request_id).decided_by == POLICY_PRINCIPAL
    say("A", "authorised by POLICY (no person), gates passed, executed, independently verified.")
    again = pstart(e)
    assert again.awaiting_approval and e.executor.count == 1
    say("A", "a second automatic authorisation for the same incident/action/target is refused: a person must review.")
    assert [k for k in kinds_of(e) if k.startswith(("approval", "recovery"))].count("approval_granted_by_policy") == 1


def kinds_of(e):
    return [x.payload.get("outcome") for x in e.recorder.ledger.store.all_entries()]


# ---- Scenario D: unsafe targets and governance failure fail closed
@pytest.mark.parametrize("database,allowed", [("dataops_sim", {"dataops_sim"}), ("master", {"master"}), ("dataops_demo_test", None),
                                              ("dataops_demo_test", {"other_test"})])
def test_demo_scenario_d_unsafe_targets_are_refused_before_anything_runs(database, allowed):
    from app.recovery.actions import RecoveryExecutor, UnsafeRecoveryTarget
    from app.simulation.runlog import RunLogStore  # noqa: F401
    from tests.test_governance_acceptance import SqlLikeRepo
    import tempfile
    from pathlib import Path
    repo = SqlLikeRepo(database)
    sim = __import__("app.simulation.service", fromlist=["SimulationService"]).SimulationService(repo, RunLogStore(Path(tempfile.mkdtemp())))
    with pytest.raises(UnsafeRecoveryTarget):
        RecoveryExecutor(sim, allowed)
    say("D", f"refused: {database} with allowlist {allowed}")
    assert repo.writes == 0
