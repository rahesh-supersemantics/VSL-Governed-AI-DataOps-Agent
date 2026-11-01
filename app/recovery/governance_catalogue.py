"""The named PreNodes (PN-01..PN-08) and invariants (INV-01..INV-22), tied to the code that enforces them.

This is data, not behaviour. Each entry names the enforcement points as ``module:Qualified.name`` and the tests as
``tests/file.py::test_name``; ``tests/test_governance_catalogue.py`` imports every enforcement symbol and checks every test
exists, so an entry cannot point at something that is not there.

``status``: enforced (code stops a violation and a test proves it), partial (enforced in part; see limitation),
not_enforced (not implemented; see limitation). ``layer`` says WHERE the check runs: ``vsl_gate`` means a compiled
vsl_langgraph invariant/PreNode, ``code`` means ordinary application code outside the VSL gates. The VSL layer today has two
invariants and one PreNode; the rest of the PreNodes are named stages implemented in code, and are labelled so.
"""

from __future__ import annotations

from dataclasses import dataclass

T = "tests/test_governance_acceptance.py::"


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    layer: str
    status: str
    enforcement: tuple[str, ...]
    tests: tuple[str, ...]
    failure: str
    limitation: str = ""


PRENODES: tuple[Entry, ...] = (
    Entry("PN-01", "Incident context and access validation", "code", "partial",
          ("app.recovery.approvals:ApprovalService._authorise", "app.recovery.workflow:RecoveryWorkflow.start"),
          (T + "test_t11_roles_without_the_approval_permission_cannot_approve_or_reject",),
          "caller denied, nothing recorded as approved",
          "Caller permissions are checked on approvals; incident existence/environment association is not re-validated inside the workflow."),
    Entry("PN-02", "Evidence integrity and sufficiency", "vsl_gate", "partial",
          ("app.governance.policy:failed_preconditions", "app.governance.policy:GateInput"),
          (T + "test_t09_missing_untraceable_or_non_actionable_evidence_never_reaches_authorisation",),
          "no approval request is created",
          "Checks that evidence is cited, known and the report actionable. Freshness, provenance and fact/hypothesis labelling are NOT checked."),
    Entry("PN-03", "Recovery action validation", "code", "partial",
          ("app.recovery.actions:validate_action", "app.recovery.risk_policy:classify"),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",),
          "action_rejected; no approval request",
          "No separate idempotency key or compensation-readiness gate; both are served by single-use approval and the plan."),
    Entry("PN-04", "Recovery target validation", "code", "enforced",
          ("app.recovery.actions:assert_recovery_target", "app.recovery.actions:assert_live_target",
           "app.recovery.actions:RecoveryExecutor.check_precondition"),
          (T + "test_t05_a_test_database_that_is_not_allowlisted_is_denied_with_no_writes",
           T + "test_t06_configured_and_actual_target_mismatch_is_denied_before_any_write"),
          "UnsafeRecoveryTarget or PreconditionFailed; no write", "Verified only against the simulated/SQL-like repo offline; real SQL Server not run."),
    Entry("PN-05", "Risk and approval validation", "vsl_gate", "enforced",
          ("app.governance.policy:build_policy", "app.recovery.approvals:ApprovalService.check", "app.recovery.risk_policy:RiskPolicy"),
          (T + "test_t03_high_risk_reprocess_needs_a_persisted_approval_and_runs_only_after_all_checks",),
          "approval_invalid / expired / approval_already_used",
          "Only the evidence+approval rule is a compiled VSL PreNode; risk and policy evaluation are code."),
    Entry("PN-06", "Pre-execution validation", "code", "enforced",
          ("app.recovery.runner:RecoveryRunner.execute", "app.recovery.actions:RecoveryExecutor.check_precondition"),
          (T + "test_t13_a_precondition_that_changed_after_approval_prevents_execution_and_keeps_the_approval_unspent",),
          "blocked/failed; approval stays unspent", ""),
    Entry("PN-07", "Controlled execution and idempotency", "code", "partial",
          ("app.recovery.approvals:ApprovalService.consume", "app.services.recoveries:RecoveryService.resume"),
          (T + "test_t14_concurrent_resumes_execute_the_operation_exactly_once",
           T + "test_t18_only_the_runner_calls_the_executors_execute_method"),
          "second claimant gets approval_already_used",
          "Idempotency is the single-use approval bound to the digest; there is no separate idempotency key."),
    Entry("PN-08", "Outcome verification and audit", "code", "enforced",
          ("app.recovery.runner:RecoveryRunner.execute", "app.recovery.runner:RecoveryRunner._escalate"),
          (T + "test_t15_a_confirmed_failure_is_recorded_accurately_and_compensation_is_evaluated",
           T + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried"),
          "verification_failed / uncertain, escalated, never success", "Compensation is design-only; nothing is rolled back."),
)

INVARIANTS: tuple[Entry, ...] = (
    Entry("INV-01", "Approved target only", "code", "enforced", ("app.recovery.actions:assert_recovery_target",),
          (T + "test_t05_a_test_database_that_is_not_allowlisted_is_denied_with_no_writes",), "UnsafeRecoveryTarget"),
    Entry("INV-02", "Simulator isolation", "code", "enforced",
          ("app.recovery.actions:PROTECTED_DATABASES", "app.recovery.actions:assert_live_target"),
          (T + "test_t04_the_simulator_and_system_databases_are_never_recovery_targets_even_if_allowlisted",), "UnsafeRecoveryTarget"),
    Entry("INV-03", "Least privilege", "code", "partial", ("app.db.readonly_preflight:verify_read_only_principal",),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "read-only identity refuses writes",
          "The read-only identity is verified against SQL Server only; not run in this offline work."),
    Entry("INV-04", "No arbitrary execution", "code", "enforced", ("app.recovery.actions:validate_action",),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "ActionRejected"),
    Entry("INV-05", "Bounded impact", "code", "partial", ("app.recovery.actions:RerunPipelineParams", "app.recovery.actions:RemoveInjectedConditionParams"),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "ActionRejected",
          "Parameters are strictly typed and enumerated; row-count limits are not enforced."),
    Entry("INV-06", "Authoritative VSL enforcement", "vsl_gate", "partial",
          ("app.governance.gates:compile_gates", "app.governance.workflow:GovernedActionService"),
          (T + "test_t18_the_governed_action_service_denies_without_a_valid_approval",), "governance_error",
          "VSL enforces the allowlist, suspension and evidence+approval; target and idempotency checks run in code beside it."),
    Entry("INV-07", "Fail closed", "code", "enforced", ("app.recovery.runner:RecoveryRunner.execute", "app.recovery.workflow:RecoveryWorkflow.resume"),
          (T + "test_t07_failed_ledger_verification_denies_execution", T + "test_t07_an_error_inside_the_governance_gates_denies_execution"),
          "no execution; approval unspent"),
    Entry("INV-08", "High-risk approval", "vsl_gate", "enforced", ("app.recovery.approvals:ApprovalService.check",),
          (T + "test_t03_high_risk_reprocess_needs_a_persisted_approval_and_runs_only_after_all_checks",), "awaiting_approval"),
    Entry("INV-09", "Explicit low-risk authorization", "code", "enforced",
          ("app.recovery.risk_policy:RiskPolicy", "app.recovery.approvals:ApprovalService.authorise_by_policy"),
          (T + "test_t09_the_low_risk_policy_cannot_authorise_without_evidence",), "waits for a person",
          "Disabled by default (RECOVERY_AUTO_AUTHORIZE_LOW_RISK); bounded retry only."),
    Entry("INV-10", "Authenticated reviewer", "code", "enforced", ("app.recovery.approvals:ApprovalService._authorise",),
          (T + "test_t11_unauthenticated_callers_cannot_approve",), "ApprovalDenied"),
    Entry("INV-11", "Approval binding", "code", "enforced", ("app.recovery.approvals:binding_digest", "app.recovery.approvals:ApprovalService.check"),
          (T + "test_t10_an_approval_for_something_else_is_rejected",), "invalid; never consumed",
          "The policy context is bound via the governance-state reference, not a policy-version field."),
    Entry("INV-12", "No approval replay", "code", "enforced", ("app.recovery.approvals:ApprovalService.consume",),
          (T + "test_t12_a_consumed_approval_cannot_authorise_a_second_operation",), "approval_already_used"),
    Entry("INV-13", "Evidence-backed decisions", "vsl_gate", "enforced", ("app.governance.policy:failed_preconditions",),
          (T + "test_t09_missing_untraceable_or_non_actionable_evidence_never_reaches_authorisation",), "no approval request"),
    Entry("INV-14", "Fact/hypothesis distinction", "code", "not_enforced", (), (), "none",
          "Reports carry evidence ids, but no check separates hypotheses from facts."),
    Entry("INV-15", "Fresh preconditions", "code", "enforced", ("app.recovery.actions:RecoveryExecutor.check_precondition",),
          (T + "test_t13_a_precondition_that_changed_after_approval_prevents_execution_and_keeps_the_approval_unspent",), "failed; unspent",
          "Evidence age is not checked, only current system state."),
    Entry("INV-16", "Valid lifecycle transitions", "code", "partial", ("app.recovery.workflow:RecoveryWorkflow._build", "app.recovery.lifecycle:logical_state"),
          (T + "test_t12_a_consumed_approval_cannot_authorise_a_second_operation",), "approval_already_used",
          "Transitions are enforced by the workflow graph and approval status, not by a central state machine."),
    Entry("INV-17", "Single execution claim", "code", "enforced", ("app.recovery.approvals:ApprovalService.consume",),
          (T + "test_t14_concurrent_resumes_execute_the_operation_exactly_once",), "approval_already_used"),
    Entry("INV-18", "Verified outcome", "code", "enforced", ("app.recovery.actions:RecoveryExecutor.verify", "app.recovery.runner:RecoveryRunner.execute"),
          (T + "test_t15_a_confirmed_failure_is_recorded_accurately_and_compensation_is_evaluated",), "verification_failed"),
    Entry("INV-19", "Recovery preparedness", "code", "partial", ("app.recovery.compensation:plan_for",),
          (T + "test_t16_when_compensation_is_not_offered_automation_stops_and_escalates_without_a_second_attempt",), "plan only",
          "Plans are design-only and not required before execution."),
    Entry("INV-20", "Governed compensation", "code", "not_enforced", ("app.recovery.compensation:CompensationPlan",),
          (T + "test_t16_when_compensation_is_not_offered_automation_stops_and_escalates_without_a_second_attempt",), "no compensating step exists",
          "By decision, no compensating write is implemented; there is nothing to govern. The plan has executable=False."),
    Entry("INV-21", "Auditable history", "code", "partial", ("app.governance.ledger:GovernanceRecorder",),
          (T + "test_t07_failed_ledger_verification_denies_execution",), "execution denied if the chain does not verify",
          "Hash-chained and tamper-evident, not immutable: someone with file access can rewrite the whole chain."),
    Entry("INV-22", "No silent failure", "code", "enforced", ("app.recovery.runner:RecoveryRunner._escalate", "app.services.status:recovery_phase"),
          (T + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried",), "explicit status plus safe reason",
          "If the escalation entry cannot be written, escalation_record_failed is reported rather than hidden."),
)
