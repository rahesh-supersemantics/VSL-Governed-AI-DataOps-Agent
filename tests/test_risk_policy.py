"""Risk policy v1: low-risk automatic authorisation is narrow, off by default, bounded, revalidated and never applies to
high or unknown risk. Offline: in-memory simulation, fake clock, counting executor, no network, no SQL Server."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.governance import GovernanceRecorder, GovernedActionService, open_ledger
from app.governance.policy import build_policy
from app.recovery import RECOVERY_ACTIONS, ApprovalDenied, ApprovalService, ApprovalSigner, RecoveryRunner, RecoveryWorkflow, RiskPolicy, SqliteApprovalStore
from app.recovery.risk_policy import HIGH, LOW, POLICY_PRINCIPAL, UNKNOWN, PolicyDecision, classify
from tests.recovery_support import SIGNING_KEY, TOKENS, Clock, CountingExecutor, kinds, make_auth, prop, run
from app.simulation.runlog import RunLogStore
from app.simulation.service import SimulationService
from tests.fakes import InMemoryRepo

LOW_PARAMS = {"pipeline_id": "order_validation"}


class PolicyEnv:
    def __init__(self, tmp_path, *, enabled: bool):
        self.clock = Clock()
        sim = SimulationService(InMemoryRepo(), RunLogStore(tmp_path / "logs"))
        sim.seed()
        sim.run_pipeline("order_ingestion")  # a healthy baseline: the retry's verification demands a run that finishes 'success'
        self.sim = sim
        self.recorder = GovernanceRecorder(open_ledger(tmp_path / "l.jsonl", fsync=False))
        self.store = SqliteApprovalStore(tmp_path / "a.sqlite3")
        self.policy = RiskPolicy(enabled=enabled)
        self.approvals = ApprovalService(self.store, ApprovalSigner(SIGNING_KEY), make_auth(), self.recorder, clock=self.clock,
                                         policy_checker=self.policy.still_permits)
        self.executor = CountingExecutor(sim)
        governed = GovernedActionService(self.recorder, RecoveryRunner(self.executor, self.approvals, self.recorder),
                                         policy=build_policy(RECOVERY_ACTIONS))
        self.workflow = RecoveryWorkflow(self.approvals, governed, self.recorder, risk_policy=self.policy)


@pytest.fixture
def env(tmp_path):
    return PolicyEnv(tmp_path, enabled=True)


def start(e, **kw):
    return run(e.workflow.start(prop("rerun_pipeline", kw.pop("params", LOW_PARAMS), **kw)))


# ---- classification is deterministic and by action alone
@pytest.mark.parametrize("action,params,risk", [
    ("rerun_pipeline", {"pipeline_id": "order_validation"}, LOW), ("rerun_pipeline", {"pipeline_id": "reporting_prep"}, LOW),
    ("rerun_pipeline", {"pipeline_id": "order_ingestion"}, HIGH), ("remove_injected_condition", {"scenario_key": "data-quality"}, HIGH),
    ("rerun_pipeline", {"pipeline_id": "x"}, UNKNOWN), ("drop_table", {}, UNKNOWN), ("rerun_pipeline", None, UNKNOWN)])
def test_classification(action, params, risk):
    assert classify(action, params)[0] == risk


# ---- T-01: a bounded low-risk retry is authorised by policy, executed and verified
def test_low_risk_retry_is_authorised_by_policy_executed_and_verified(env):
    out = start(env)
    assert out.executed and out.execution_status == "executed" and env.executor.count == 1
    record = env.store.get(out.request_id)
    assert record.decided_by == POLICY_PRINCIPAL and record.status == "consumed"
    log = kinds(env)
    assert "approval_granted_by_policy" in log and "approval_granted" not in log and "recovery_succeeded" in log
    assert env.recorder.ledger.verify_integrity()


# ---- off by default
def test_policy_disabled_by_default_leaves_the_request_waiting_for_a_person(tmp_path):
    e = PolicyEnv(tmp_path, enabled=False)
    out = start(e)
    assert out.awaiting_approval and e.executor.count == 0
    assert "approval_granted_by_policy" not in kinds(e)


# ---- T-02: high risk and unknown risk never run without a person
@pytest.mark.parametrize("action,params", [("rerun_pipeline", {"pipeline_id": "order_ingestion"}),
                                           ("remove_injected_condition", {"scenario_key": "duplicate-records"})])
def test_high_risk_waits_for_a_person_even_when_policy_is_on(env, action, params):
    out = run(env.workflow.start(prop(action, params)))
    assert out.awaiting_approval and env.executor.count == 0
    assert env.store.get(out.request_id).decided_by is None


def test_a_person_approval_of_high_risk_runs_only_after_that_approval(env):
    out = run(env.workflow.start(prop("rerun_pipeline", {"pipeline_id": "order_ingestion"})))
    env.approvals.approve(out.request_id, TOKENS["admin"], "ok")
    assert env.executor.count == 0  # approval is not execution
    done = run(env.workflow.resume(out.request_id))
    assert done.executed and env.executor.count == 1 and env.store.get(out.request_id).decided_by == "admin.user"


def test_unknown_risk_decision_is_denied_by_the_approval_service(env):
    out = run(env.workflow.start(prop("rerun_pipeline", {"pipeline_id": "order_ingestion"})))
    with pytest.raises(ApprovalDenied):
        env.approvals.authorise_by_policy(out.request_id, PolicyDecision(True, UNKNOWN, "x"))
    with pytest.raises(ApprovalDenied):  # a forged "allowed" decision for a HIGH request is refused by the live policy check
        env.approvals.authorise_by_policy(out.request_id, PolicyDecision(True, LOW, "forged"))
    assert env.store.get(out.request_id).status == "pending" and env.executor.count == 0


# ---- evidence is still required
def test_no_cited_evidence_or_non_actionable_report_never_auto_authorises(env):
    out = start(env, refs=())
    assert not out.executed and env.executor.count == 0
    out = start(env, status="no_issue_found")
    assert not out.executed and env.executor.count == 0


# ---- bounded: second identical retry for the same incident goes to a person
def test_second_automatic_retry_for_the_same_incident_needs_a_person(env):
    assert start(env).executed
    second = start(env)
    assert second.awaiting_approval and env.executor.count == 1
    other_incident = start(env, incident="INV-OTHER")
    assert other_incident.executed and env.executor.count == 2


# ---- revalidation at use: turning the policy off after authorisation blocks execution (T-13 style)
def test_policy_authorisation_is_revalidated_when_used(env):
    out = run(env.workflow.start(prop("rerun_pipeline", LOW_PARAMS)))  # already executed by policy; make a fresh pending one
    assert out.executed
    pending = run(env.workflow.start(prop("rerun_pipeline", LOW_PARAMS)))  # limit reached: pending for a person
    assert pending.awaiting_approval
    decision = PolicyDecision(True, LOW, "R1_rerun_read_mostly_pipeline")
    env.approvals.authorise_by_policy(pending.request_id, decision)  # force a policy authorisation past the bound
    env.policy = None
    env.approvals.policy_checker = RiskPolicy(enabled=False).still_permits  # policy switched off in the meantime
    blocked = run(env.workflow.resume(pending.request_id))
    assert not blocked.executed and env.executor.count == 1
    assert env.approvals.check(pending.request_id).reason == "policy_no_longer_permits"


# ---- a person cannot impersonate the policy principal
def test_a_person_cannot_present_as_the_policy_principal(tmp_path):
    from app.recovery.auth import ApprovalConfigError, Identity, StaticTokenAuthenticator, token_digest

    with pytest.raises(ApprovalConfigError):  # first line of defence: the subject syntax cannot contain ':'
        StaticTokenAuthenticator([{"subject": POLICY_PRINCIPAL, "token_sha256": token_digest("t" * 32), "roles": ["recovery_admin"]}])

    class Spoof:  # second line: even an authenticator that claims the principal is refused by the approval service
        def authenticate(self, credential):
            return Identity(POLICY_PRINCIPAL, frozenset({"recovery_admin"}))

        def current_roles(self, subject):
            return frozenset({"recovery_admin"})

    e = PolicyEnv(tmp_path, enabled=False)
    e.approvals.auth = Spoof()
    out = start(e)
    with pytest.raises(ApprovalDenied) as exc:
        e.approvals.approve(out.request_id, "anything", "x")
    assert exc.value.code == "not_permitted" and e.executor.count == 0


# ---- a tampered stored policy approval fails the seal check
def test_tampered_policy_approval_is_refused(env):
    out = run(env.workflow.start(prop("rerun_pipeline", LOW_PARAMS)))
    assert out.executed
    pending = run(env.workflow.start(prop("rerun_pipeline", LOW_PARAMS)))
    env.approvals.authorise_by_policy(pending.request_id, PolicyDecision(True, LOW, "R1_rerun_read_mostly_pipeline"))
    env.store.update_where(pending.request_id, {"decided_by": "policy:risk-policy-v9"}, "status = ?", ("approved",))
    assert env.approvals.check(pending.request_id).state == "invalid"


# ---- T-22: a policy-authorised retry whose run does not finish 'success' is never reported as success
def test_unverified_retry_outcome_is_not_reported_as_success(tmp_path):
    from tests.agent_support import apply_scenario

    e = PolicyEnv(tmp_path, enabled=True)
    apply_scenario(e.sim, "duplicate-records")  # the real condition the retry cannot fix: validation keeps failing
    out = run(e.workflow.start(prop("rerun_pipeline", LOW_PARAMS, incident="INV-FAIL")))
    assert e.executor.count == 1  # it did run (authorised by policy) ...
    assert not out.executed and out.execution_status == "verification_failed"  # ... and is NOT reported as success
    assert "recovery_succeeded" not in kinds(e) and "recovery_verification_failed" in kinds(e)
