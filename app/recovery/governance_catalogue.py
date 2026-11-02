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
    Entry("PN-02", "Evidence integrity and sufficiency", "vsl_gate", "enforced",
          ("app.governance.policy:failed_preconditions", "app.governance.policy:evidence_failures", "app.governance.policy:EvidencePolicy"),
          (T + "test_t09_missing_untraceable_or_non_actionable_evidence_never_reaches_authorisation",
           "tests/test_governance_evidence.py::test_evidence_that_went_stale_while_waiting_blocks_execution_and_leaves_the_approval_unspent"),
          "no approval request is created; at execution the approval stays unspent",
          "Evidence-policy-v1: the metadata (tool, status, collected_at, investigation) is stamped by our own code on receipt and is not cryptographically signed. Its digest is written into the hash-chained ledger and re-checked before execution, so later edits to the stored rows are detected (evidence_integrity_failed); this does not prove the original timestamps were truthful. The monitor is Python encoded as a gamma estimate inside the compiled PreNode."),
    Entry("PN-03", "Recovery action validation", "code", "partial",
          ("app.recovery.actions:validate_action", "app.recovery.risk_policy:classify"),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",),
          "action_rejected; no approval request",
          "No compensation-readiness gate; the plan is design-only. The idempotency key is checked at execution (PN-07)."),
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
    Entry("PN-07", "Controlled execution and idempotency", "code", "enforced",
          ("app.recovery.approvals:ApprovalService.consume", "app.recovery.approvals:idempotency_key", "app.recovery.approvals:SqliteApprovalStore.claim_execution", "app.services.recoveries:RecoveryService.resume"),
          (T + "test_t14_concurrent_resumes_execute_the_operation_exactly_once",
           T + "test_t18_only_the_runner_calls_the_executors_execute_method",
           "tests/test_governance_idempotency.py::test_two_approvals_for_the_same_operation_run_it_exactly_once_when_resumed_concurrently"),
          "second claimant gets approval_already_used or recovery_blocked (duplicate_or_unreconciled_operation)",
          "Key = hash of incident, action, parameters and target; one open claim per key; acquisition, denial, settlement and reconciliation are each ledger entries. An uncertain claim blocks reruns until reconciled by an approver through a service method (no HTTP or CLI route yet). Plain Python, not a compiled gate."),
    Entry("PN-08", "Outcome verification and audit", "code", "enforced",
          ("app.recovery.runner:RecoveryRunner.execute", "app.recovery.runner:RecoveryRunner._escalate"),
          (T + "test_t15_a_confirmed_failure_is_recorded_accurately_and_compensation_is_evaluated",
           T + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried"),
          "verification_failed / uncertain, escalated, never success", "Compensation is design-only; nothing is rolled back."),
)

INVARIANTS: tuple[Entry, ...] = (
    Entry("INV-01", "Validated recovery targets only", "code", "enforced", ("app.recovery.actions:assert_recovery_target",),
          (T + "test_t05_a_test_database_that_is_not_allowlisted_is_denied_with_no_writes",), "UnsafeRecoveryTarget"),
    Entry("INV-02", "Isolation of simulator and test environments", "code", "enforced",
          ("app.recovery.actions:PROTECTED_DATABASES", "app.recovery.actions:assert_live_target"),
          (T + "test_t04_the_simulator_and_system_databases_are_never_recovery_targets_even_if_allowlisted",), "UnsafeRecoveryTarget"),
    Entry("INV-03", "Least-privilege access", "code", "partial", ("app.db.readonly_preflight:verify_read_only_principal",),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "read-only identity refuses writes",
          "The read-only identity is verified against SQL Server only; not run in this offline work."),
    Entry("INV-04", "No arbitrary command or SQL execution", "code", "enforced", ("app.recovery.actions:validate_action",),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "ActionRejected"),
    Entry("INV-05", "Bounded impact", "code", "partial", ("app.recovery.actions:RerunPipelineParams", "app.recovery.actions:RemoveInjectedConditionParams"),
          (T + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected",), "ActionRejected",
          "Parameters are strictly typed and enumerated; row-count limits are not enforced."),
    Entry("INV-06", "Authoritative VSL governance", "vsl_gate", "partial",
          ("app.governance.gates:compile_gates", "app.governance.workflow:GovernedActionService"),
          (T + "test_t18_the_governed_action_service_denies_without_a_valid_approval",), "governance_error",
          "VSL enforces the allowlist, suspension and evidence+approval; target and idempotency checks run in code beside it."),
    Entry("INV-07", "Fail-closed behavior", "code", "enforced", ("app.recovery.runner:RecoveryRunner.execute", "app.recovery.workflow:RecoveryWorkflow.resume"),
          (T + "test_t07_failed_ledger_verification_denies_execution", T + "test_t07_an_error_inside_the_governance_gates_denies_execution"),
          "no execution; approval unspent"),
    Entry("INV-08", "Human approval for high-risk operations", "vsl_gate", "enforced", ("app.governance.policy:failed_preconditions", "app.recovery.approvals:ApprovalService.check"),
          (T + "test_t03_high_risk_reprocess_needs_a_persisted_approval_and_runs_only_after_all_checks",), "awaiting_approval"),
    Entry("INV-09", "Explicit policy authorization for permitted automatic actions", "code", "enforced",
          ("app.recovery.risk_policy:RiskPolicy", "app.recovery.approvals:ApprovalService.authorise_by_policy"),
          (T + "test_t09_the_low_risk_policy_cannot_authorise_without_evidence",), "waits for a person",
          "Disabled by default (RECOVERY_AUTO_AUTHORIZE_LOW_RISK); bounded retry only."),
    Entry("INV-10", "Authenticated reviewer identity", "code", "enforced", ("app.recovery.approvals:ApprovalService._authorise",),
          (T + "test_t11_unauthenticated_callers_cannot_approve",), "ApprovalDenied"),
    Entry("INV-11", "Approval bound to the exact action, parameters, target and relevant evidence", "code", "enforced", ("app.recovery.approvals:binding_digest", "app.recovery.approvals:ApprovalService.check"),
          (T + "test_t10_an_approval_for_something_else_is_rejected",), "invalid; never consumed",
          "The policy context is bound via the governance-state reference, not a policy-version field."),
    Entry("INV-12", "Replay and duplicate-execution prevention", "code", "enforced", ("app.recovery.approvals:ApprovalService.consume", "app.persistence.repositories:OperationsStore.claim_recovery_resume", "app.recovery.approvals:SqliteApprovalStore.claim_execution"),
          (T + "test_t12_a_consumed_approval_cannot_authorise_a_second_operation", T + "test_t14_concurrent_resumes_execute_the_operation_exactly_once"),
          "approval_already_used"),
    Entry("INV-13", "Traceable evidence", "vsl_gate", "enforced", ("app.governance.policy:failed_preconditions",),
          (T + "test_t09_missing_untraceable_or_non_actionable_evidence_never_reaches_authorisation",), "no approval request"),
    Entry("INV-14", "Clear separation of observed facts and hypotheses", "code", "partial",
          ("app.agent.models:InvestigationReport", "app.governance.policy:evidence_failures"),
          ("tests/test_governance_evidence.py::test_confident_model_text_does_not_change_the_evidence_decision",),
          "report rejected; or evidence does not support the action",
          "Findings are labelled observed or hypothesis by the list they sit in, must cite evidence, and only a supported root cause citing tool-observed issue/warning evidence supports an action (hypotheses never do). A deterministic keyword screen flags certain-sounding hypotheses, hedged facts and facts resting only on failed tools for human review; it cannot establish that wording is semantically correct and never promotes or clears a finding."),
    Entry("INV-15", "Fresh preconditions immediately before execution", "code", "enforced", ("app.recovery.actions:RecoveryExecutor.check_precondition", "app.governance.policy:evidence_failures"),
          (T + "test_t13_a_precondition_that_changed_after_approval_prevents_execution_and_keeps_the_approval_unspent",), "failed; unspent",
          "Evidence age is re-checked at execution by the PN-02 evidence policy as well as current system state."),
    Entry("INV-16", "Valid lifecycle transitions", "code", "partial", ("app.recovery.lifecycle:transition_allowed", "app.persistence.repositories:OperationsStore.record_recovery_outcome", "app.recovery.workflow:RecoveryWorkflow._build"),
          ("tests/test_governance_lifecycle.py::test_a_final_recovery_is_never_reopened_rewritten_claimed_or_released",
           "tests/test_governance_lifecycle.py::test_an_unknown_outcome_cannot_later_be_rewritten_as_success"), "write refused; row unchanged",
          "Final rows are never reopened and resuming is entered by one atomic claim. The 14 logical states are a read-only mapping, not a stored state machine, so Proposed and ReadyToExecute are not checkpoints."),
    Entry("INV-17", "No unsupported execution-success claims", "code", "enforced", ("app.recovery.governance_view:build_governance_view", "app.services.status:recovery_phase"),
          ("tests/test_governance_view_api.py::test_a_success_label_needs_both_the_stored_status_and_the_ledger_entry",
           T + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried"), "shown as not verified / uncertain"),
    Entry("INV-18", "Independent outcome verification", "code", "enforced", ("app.recovery.actions:RecoveryExecutor.verify", "app.recovery.runner:RecoveryRunner.execute"),
          (T + "test_t15_a_confirmed_failure_is_recorded_accurately_and_compensation_is_evaluated",), "verification_failed"),
    Entry("INV-19", "Explicit representation of compensation needs", "code", "partial", ("app.recovery.compensation:plan_for",),
          (T + "test_t16_when_compensation_is_not_offered_automation_stops_and_escalates_without_a_second_attempt",), "plan only",
          "Plans are design-only and not required before execution."),
    Entry("INV-20", "Governance of any future compensating action", "code", "not_enforced", ("app.recovery.compensation:CompensationPlan",),
          (T + "test_t16_when_compensation_is_not_offered_automation_stops_and_escalates_without_a_second_attempt",), "no compensating step exists",
          "By decision, no compensating write is implemented; there is nothing to govern. The plan has executable=False."),
    Entry("INV-21", "Auditable decisions and execution history", "code", "partial", ("app.governance.ledger:GovernanceRecorder",),
          (T + "test_t07_failed_ledger_verification_denies_execution",), "execution denied if the chain does not verify",
          "Hash-chained and tamper-evident, not immutable: someone with file access can rewrite the whole chain."),
    Entry("INV-22", "No silent failure", "code", "enforced", ("app.recovery.runner:RecoveryRunner._escalate", "app.services.status:recovery_phase"),
          (T + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried",), "explicit status plus safe reason",
          "If the escalation entry cannot be written, escalation_record_failed is reported rather than hidden."),
)


# ---- traceability: invariant -> PreNode(s), ledger outcomes it produces, and where the dashboard shows it
# (outcome names are the ones the ledger actually records; tests/test_governance_catalogue.py checks they occur in the source)
TRACE: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "INV-01": (("PN-04",), ("recovery_failed",), "Recovery unavailable reason (recovery_unavailable_info)"),
    "INV-02": (("PN-04",), ("recovery_failed",), "Recovery unavailable reason"),
    "INV-03": (("PN-04",), (), "not shown (read-only identity is checked by the preflight script)"),
    "INV-04": (("PN-03",), ("recovery_action_rejected",), "Recovery list: status action_rejected"),
    "INV-05": (("PN-03",), ("recovery_action_rejected",), "not shown"),
    "INV-06": (("PN-05", "PN-07"), ("authorised", "refused_suspended", "terminal_suspended"), "Governance panel: ledger outcomes"),
    "INV-07": (("PN-06", "PN-07"), ("governance_error", "recovery_blocked"), "Governance panel: lifecycle (Failed/Rejected) and ledger outcomes"),
    "INV-08": (("PN-05",), ("approval_requested", "approval_granted"), "Governance panel: Authorisation (By a PERSON / NOT authorised)"),
    "INV-09": (("PN-05",), ("approval_granted_by_policy",), "Governance panel: Authorisation (By POLICY)"),
    "INV-10": (("PN-01", "PN-05"), ("approval_attempt_refused",), "Approval request: decided by"),
    "INV-11": (("PN-05", "PN-06"), ("approval_not_usable",), "not shown (binding digest is in the approval summary)"),
    "INV-12": (("PN-07",), ("approval_not_usable",), "Recovery list: status approval_already_used"),
    "INV-13": (("PN-02",), ("recovery_proposed",), "Approval request: evidence refs"),
    "INV-14": (("PN-02",), ("preconditions_failed",), "Governance panel: Analysis (observed vs hypothesis)"),
    "INV-15": (("PN-06",), ("recovery_failed",), "Governance panel: lifecycle note"),
    "INV-16": (("PN-06", "PN-07"), (), "Governance panel: lifecycle state and exactness"),
    "INV-17": (("PN-08",), ("recovery_succeeded", "recovery_verification_failed", "recovery_uncertain"), "Governance panel: Execution (VERIFIED vs NOT verified)"),
    "INV-18": (("PN-08",), ("recovery_succeeded", "recovery_verification_failed"), "Governance panel: Execution"),
    "INV-19": (("PN-03", "PN-08"), ("compensation_plan_proposed",), "Governance panel: Compensation (PROPOSED only)"),
    "INV-20": (("PN-08",), (), "Governance panel: Compensation executed is always false"),
    "INV-21": (("PN-08",), ("recovery_attempt",), "Governance panel: ledger outcomes; VerbaLedger Explorer"),
    "INV-22": (("PN-08",), ("recovery_escalated",), "Governance panel: Escalation"),
}

# ---- PreNode specification: purpose, inputs, outputs, audit events
PRENODE_SPEC: dict[str, tuple[str, str, str, str]] = {
    "PN-01": ("Confirm who is asking and what the request covers", "credential, request id, incident id", "an authenticated identity or a refusal", "approval_attempt_refused"),
    "PN-02": ("Refuse proposals without traceable, known evidence", "report status, cited evidence ids, stored evidence metadata (tool, status, collected_at, investigation), evidence policy", "pass, or no approval request is created", "recovery_proposed, preconditions_failed, evidence_integrity_failed"),
    "PN-03": ("Allow only schema-valid, allowlisted actions and classify risk deterministically", "action name, parameters", "ValidatedAction and a risk class, or ActionRejected", "recovery_action_rejected"),
    "PN-04": ("Check the target is the allowlisted _test database and that the live connection really is that database", "engine, allowlist, DB_NAME()", "pass, or UnsafeRecoveryTarget / PreconditionFailed", "recovery_failed"),
    "PN-05": ("Require a valid, bound, unexpired, unspent approval (person) or a valid policy authorisation (low risk, opt-in)", "approval record, binding digest, governance state", "approved_valid or a refusal code", "approval_requested, approval_granted, approval_granted_by_policy"),
    "PN-06": ("Re-check target, parameters, authorisation and preconditions immediately before acting", "approval id, digest, current system state", "pass, or blocked/failed with the approval unspent", "recovery_blocked, recovery_failed"),
    "PN-07": ("Claim the approval atomically and let only the runner call the executor", "approval id, digest, idempotency key", "one execution claim, or approval_already_used / duplicate_or_unreconciled_operation", "recovery_attempt, idempotency_claim_acquired, recovery_blocked, idempotency_claim_reconciled"),
    "PN-08": ("Verify the result independently and record success, failure or unknown; escalate when not success", "execution result, fresh read of system state", "executed / verification_failed / uncertain plus escalation", "recovery_succeeded, recovery_verification_failed, recovery_uncertain, compensation_plan_proposed, recovery_escalated"),
}
