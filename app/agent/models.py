"""Typed shapes used by the agent: tool specs and outcomes, evidence, and the investigation report."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Outcome = Literal["ok", "diagnostic_error", "tool_error", "invalid_arguments", "timeout", "malformed_response",
                  "connection_error", "not_approved", "budget_exceeded", "duplicate_skipped", "skipped"]
Confidence = Literal["low", "medium", "high"]
InvestigationStatus = Literal["issue_identified", "warnings_only", "no_issue_detected", "inconclusive", "failed"]
RootCauseStatus = Literal["supported", "undetermined"]


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class ToolOutcome(BaseModel):
    outcome: Outcome
    structured: dict[str, Any] | None = None
    detail: str | None = None  # short, redacted, for errors only


class PlannedCall(BaseModel):
    tool: str
    arguments: dict[str, Any] | None = None  # None: the model's arguments_json could not be parsed
    parse_error: str | None = None


class ToolRun(BaseModel):
    """One attempted tool call (successful or not), in the order it happened."""
    call_id: str
    round: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome
    detail: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """What one diagnostic returned, reduced to a bounded digest. Facts only; no interpretation."""
    evidence_id: str
    call_id: str
    tool: str  # the diagnostic that produced it (run_all_diagnostics is split into its parts)
    status: str  # ok | warning | issue_detected | error  (as reported by the diagnostic)
    severity: str
    summary: str
    error_code: str | None = None
    recommended_next_step: str | None = None
    affected_records: list[str] = Field(default_factory=list)
    digest: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    collected_at: str | None = None  # ISO-8601 UTC, stamped by code when the tool result arrived (never by a model)


_CERTAIN = re.compile(r"\b(confirmed|definitely|certainly|proven|proved|undoubtedly|without doubt|for sure|conclusively)\b", re.I)
_HEDGED = re.compile(r"\b(might|may|possibly|perhaps|probably|likely|could be|suggests?|appears?|seems?)\b", re.I)


def review_reasons(kind: str, statement: str, evidence_statuses: list[str]) -> list[str]:
    """Deterministic screen only. Keyword checks cannot establish that wording is semantically right; they flag the obvious cases
    for a person instead of silently trusting the model's phrasing."""
    reasons: list[str] = []
    if kind == "hypothesis" and _CERTAIN.search(statement or ""):
        reasons.append("hypothesis_uses_certainty_language")
    if kind == "observed" and _HEDGED.search(statement or ""):
        reasons.append("observed_uses_hedged_language")
    if kind == "observed" and evidence_statuses and all(s == "error" for s in evidence_statuses):
        reasons.append("observed_cites_only_failed_tool_evidence")
    return reasons


class Finding(BaseModel):
    """``kind`` separates what a diagnostic observed from what a model proposed. It is set from where the finding is
    placed in the report, never from the wording of the statement."""
    statement: str
    evidence_ids: list[str]
    kind: Literal["observed", "hypothesis"] | None = None
    # Deterministic wording/evidence screen (INV-14). It can only ADD a review flag; it never promotes a hypothesis to a fact and
    # never clears a finding. Absence of a flag is not a statement that the wording is correct.
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class RootCause(BaseModel):
    status: RootCauseStatus
    statement: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    explanation: str


class ReportError(BaseModel):
    kind: str
    message: str


class InvestigationReport(BaseModel):
    """The machine-readable result of one investigation (also rendered as text by the CLI)."""
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    incident: str
    status: InvestigationStatus
    mode: Literal["live", "offline", "scripted"]
    model: str
    generated_at: str
    summary: str
    observed_findings: list[Finding]
    hypotheses: list[Finding]
    likely_root_cause: RootCause
    impact: Finding | None = None
    confidence: Confidence
    confidence_explanation: str
    recommended_next_steps: list[str]
    tools_executed: list[ToolRun]
    evidence: list[EvidenceItem]
    limitations: list[str]
    security_notes: list[str] = Field(default_factory=list)
    rounds_used: int
    tool_calls_used: int
    stop_reason: str
    error: ReportError | None = None
    approved_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _facts_and_hypotheses_stay_separate(self) -> "InvestigationReport":
        """INV-14. Every finding is labelled by the list it sits in, and every finding must cite evidence that exists in this
        report. A fact cannot be listed as a hypothesis or the reverse, and a statement can never certify itself."""
        known = {e.evidence_id for e in self.evidence}
        status_by_id = {e.evidence_id: e.status for e in self.evidence}
        for label, findings in (("observed", self.observed_findings), ("hypothesis", self.hypotheses)):
            for f in findings:
                if f.kind not in (None, label):
                    raise ValueError(f"a {f.kind} finding was placed in the {label} list")
                if not f.evidence_ids or not set(f.evidence_ids) <= known:
                    raise ValueError(f"a {label} finding cites evidence that is not in the report")
                f.kind = label  # type: ignore[assignment]
                f.review_reasons = review_reasons(label, f.statement, [status_by_id[i] for i in f.evidence_ids])
                f.review_required = bool(f.review_reasons)
        if self.likely_root_cause.status == "supported" and not set(self.likely_root_cause.evidence_ids) <= known:
            raise ValueError("a supported root cause cites evidence that is not in the report")
        return self


class PlanCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=100)
    arguments_json: str = Field(max_length=4000)


class PlanDecision(BaseModel):
    """What the planning model must return."""
    model_config = ConfigDict(extra="forbid")
    action: Literal["call_tools", "finish"]
    tool_calls: list[PlanCall] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=2000)


class DraftFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class DraftRootCause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str | None = Field(default=None, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    explanation: str = Field(default="", max_length=1500)


class DraftImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str | None = Field(default=None, max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class ReportDraft(BaseModel):
    """What the report-writing model must return (interpretation only; facts come from the tools)."""
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=2000)
    hypotheses: list[DraftFinding] = Field(default_factory=list, max_length=10)
    root_cause: DraftRootCause
    impact: DraftImpact
    confidence: Confidence
    confidence_explanation: str = Field(default="", max_length=1500)
    recommended_next_steps: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)
