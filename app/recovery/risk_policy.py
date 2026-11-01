"""Versioned risk policy: the ONLY place that can authorise a recovery action without a person.

Deterministic and code-only. Model output, model confidence and free text are never inputs; only the validated
action, its parameters, the persisted request and counters read from the approval store.

Policy ``risk-policy-v1``
-------------------------
* Classification (by action and parameters alone):
  - ``rerun_pipeline`` for ``order_validation`` or ``reporting_prep``: LOW. These pipelines read business tables
    and write only a run record, its validation rows, a log file, a simulator incident when the run fails, and
    advance the simulation clock. They do not write business rows.
  - ``rerun_pipeline`` for ``order_ingestion``: HIGH. It inserts orders; it skips known source event ids but its
    effect on a batch is not proven idempotent here, so a person must approve it.
  - ``remove_injected_condition``: HIGH (deletes rows or shifts the clock).
  - Anything else: UNKNOWN, which is denied.
* Automatic authorisation is OFF unless ``RECOVERY_AUTO_AUTHORIZE_LOW_RISK`` is true, applies to LOW only, requires
  an actionable report with cited evidence, and is bounded to ONE automatic authorisation per incident, action
  and target. A second identical request goes to a person.
* HIGH and UNKNOWN never run without a persisted human approval. Approval by a person never changes the class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "risk-policy-v1"
POLICY_PRINCIPAL_PREFIX = "policy:"
POLICY_PRINCIPAL = POLICY_PRINCIPAL_PREFIX + POLICY_VERSION
LOW, HIGH, UNKNOWN = "low", "high", "unknown"
LOW_RISK_PIPELINES = frozenset({"order_validation", "reporting_prep"})
MAX_AUTOMATIC_PER_INCIDENT_ACTION_TARGET = 1


def classify(action_type: object, parameters: object) -> tuple[str, str]:
    """(risk class, rule id). Unrecognised input is UNKNOWN."""
    if not isinstance(parameters, dict):
        return UNKNOWN, "parameters_not_an_object"
    if action_type == "rerun_pipeline":
        pipeline = parameters.get("pipeline_id")
        if pipeline in LOW_RISK_PIPELINES:
            return LOW, "R1_rerun_read_mostly_pipeline"
        if pipeline == "order_ingestion":
            return HIGH, "R2_rerun_ingestion_writes_orders"
        return UNKNOWN, "unknown_pipeline"
    if action_type == "remove_injected_condition":
        return HIGH, "R3_remove_injected_condition"
    return UNKNOWN, "unknown_action"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk: str
    rule: str
    reasons: tuple[str, ...] = ()
    version: str = POLICY_VERSION


@dataclass(frozen=True)
class RiskPolicy:
    enabled: bool = False

    def still_permits(self, record: Any) -> bool:
        """Re-check an existing policy authorisation at use time (policy turned off or class changed = invalid)."""
        risk, _ = classify(record.action_type, record.parameters)
        return self.enabled and risk == LOW and bool(record.evidence_refs) and record.report_status == "issue_identified"

    def evaluate(self, record: Any, *, prior_automatic: int) -> PolicyDecision:
        risk, rule = classify(record.action_type, record.parameters)
        if risk != LOW:
            return PolicyDecision(False, risk, rule, ("risk_requires_human_approval" if risk == HIGH else "risk_unknown_denied",))
        reasons: list[str] = []
        if not self.enabled:
            reasons.append("automatic_authorisation_disabled")
        if record.report_status != "issue_identified":
            reasons.append("report_not_actionable")
        if not record.evidence_refs:
            reasons.append("no_cited_evidence")
        if prior_automatic >= MAX_AUTOMATIC_PER_INCIDENT_ACTION_TARGET:
            reasons.append("automatic_limit_reached_needs_human")
        return PolicyDecision(not reasons, risk, rule, tuple(reasons))
