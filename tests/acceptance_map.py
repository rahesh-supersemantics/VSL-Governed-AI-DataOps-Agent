"""The latest brief's T-01..T-22, mapped to the tests that prove them. Test FUNCTION NAMES in tests/test_governance_acceptance.py
carry the earlier numbering (test_tNN_...); this map is the authority for the latest numbering. T-20 is pending approval."""
A = "tests/test_governance_acceptance.py::"
R = "tests/test_risk_policy.py::"
V = "tests/test_governance_view_api.py::"
L = "tests/test_governance_lifecycle.py::"
S = "tests/test_recovery_auth_actions.py::"

MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "T-01": ("Safe low-risk retry under explicit policy", (R + "test_low_risk_retry_is_authorised_by_policy_executed_and_verified", R + "test_policy_disabled_by_default_leaves_the_request_waiting_for_a_person", R + "test_second_automatic_retry_for_the_same_incident_needs_a_person")),
    "T-02": ("High-risk operation denied without approval", (R + "test_high_risk_waits_for_a_person_even_when_policy_is_on", A + "test_t18_the_governed_action_service_denies_without_a_valid_approval")),
    "T-03": ("Valid high-risk human approval", (A + "test_t03_high_risk_reprocess_needs_a_persisted_approval_and_runs_only_after_all_checks",)),
    "T-04": ("Unsafe target rejected", (A + "test_t04_the_simulator_and_system_databases_are_never_recovery_targets_even_if_allowlisted", A + "test_t05_a_test_database_that_is_not_allowlisted_is_denied_with_no_writes")),
    "T-05": ("Governance unavailable; fail closed", (A + "test_t07_failed_ledger_verification_denies_execution", A + "test_t07_an_unreadable_ledger_denies_execution", A + "test_t07_an_error_inside_the_governance_gates_denies_execution")),
    "T-06": ("Arbitrary SQL or command rejected", (A + "test_t08_arbitrary_sql_unsupported_actions_and_malformed_model_output_are_rejected", A + "test_t08_a_prompt_injection_in_the_reason_text_changes_nothing")),
    "T-07": ("Approval/evidence mismatch rejected", (A + "test_t10_an_approval_for_something_else_is_rejected", A + "test_t13_stale_evidence_changes_the_binding_and_invalidates_the_approval", A + "test_t09_missing_untraceable_or_non_actionable_evidence_never_reaches_authorisation")),
    "T-08": ("Unauthorized reviewer rejected", (A + "test_t11_roles_without_the_approval_permission_cannot_approve_or_reject", A + "test_t11_unauthenticated_callers_cannot_approve", A + "test_t11_a_revoked_reviewer_cannot_approve_and_a_revoked_approver_invalidates_a_granted_approval")),
    "T-09": ("Replay and duplicate execution prevented", (A + "test_t12_a_consumed_approval_cannot_authorise_a_second_operation", A + "test_t12_a_second_request_for_the_same_action_needs_its_own_approval")),
    "T-10": ("Stale preconditions rejected", (A + "test_t13_a_precondition_that_changed_after_approval_prevents_execution_and_keeps_the_approval_unspent", A + "test_t13_an_approval_that_expired_before_use_does_not_run", A + "test_t13_a_governance_suspension_after_approval_invalidates_it")),
    "T-11": ("Concurrent execution safely controlled", (A + "test_t14_concurrent_resumes_execute_the_operation_exactly_once", A + "test_t14_concurrent_decisions_on_one_request_record_exactly_one")),
    "T-12": ("Compensation need recorded and escalation triggered", (A + "test_t15_a_confirmed_failure_is_recorded_accurately_and_compensation_is_evaluated", A + "test_t16_when_compensation_is_not_offered_automation_stops_and_escalates_without_a_second_attempt", A + "test_t16_an_escalation_that_cannot_be_recorded_is_reported_and_still_not_success")),
    "T-13": ("Uncertain outcome never reported as success", (A + "test_t17_an_unknown_outcome_is_escalated_and_reconciled_never_blindly_retried", A + "test_t17_a_restart_after_the_approval_was_spent_is_reported_uncertain_and_escalated", L + "test_an_unknown_outcome_cannot_later_be_rewritten_as_success")),
    "T-14": ("Attempted bypass is rejected", (A + "test_t18_the_runner_refuses_to_act_without_a_recovery_context_or_an_authorisation_record", A + "test_t18_a_forged_binding_digest_in_the_context_is_refused", A + "test_t18_only_the_runner_calls_the_executors_execute_method", A + "test_t18_the_set_of_state_changing_http_routes_is_pinned_and_none_executes_recovery_except_resume")),
    "T-15": ("Offline mode makes no external model call", (A + "test_t19_offline_investigation_makes_no_external_connection_and_does_not_use_a_model_client", "tests/test_governance_offline_mode.py::test_an_offline_investigation_opens_no_network_connection_and_never_builds_the_live_client")),
    "T-16": ("Human approval is not equivalent to execution", (A + "test_t21_human_approval_does_not_imply_that_anything_executed", V + "test_human_approval_is_labelled_human_and_does_not_imply_execution")),
    "T-17": ("Policy approval is distinguishable from human approval", (V + "test_policy_authorisation_is_labelled_policy_and_never_human", R + "test_a_person_cannot_present_as_the_policy_principal", V + "test_dashboard_panel_labels_policy_plan_attempt_and_escalation_separately")),
    "T-18": ("Independent outcome verification is required", (R + "test_unverified_retry_outcome_is_not_reported_as_success", V + "test_a_success_label_needs_both_the_stored_status_and_the_ledger_entry", V + "test_verified_success_is_distinct_from_the_attempt")),
    "T-19": ("Invalid lifecycle transition rejected", (L + "test_a_final_recovery_is_never_reopened_rewritten_claimed_or_released", L + "test_resuming_is_entered_only_by_one_atomic_claim_from_awaiting_approval", L + "test_the_documented_table_agrees_with_the_store")),
    "T-20": ("Live LLM invocation (PENDING: needs explicit approval)", (A + "test_t20_live_mode_invokes_the_real_configured_model",)),
    "T-21": ("Audit history preserved and failures visible", (A + "test_t07_failed_ledger_verification_denies_execution", A + "test_t16_an_escalation_that_cannot_be_recorded_is_reported_and_still_not_success", V + "test_the_recovery_page_shows_backend_governance_and_executes_nothing")),
    "T-22": ("Target and server database identity verified before writes", (A + "test_t06_configured_and_actual_target_mismatch_is_denied_before_any_write", A + "test_t06_the_matching_actual_target_does_run", S + "test_live_target_must_match_the_validated_one_or_the_action_does_not_run")),
}
