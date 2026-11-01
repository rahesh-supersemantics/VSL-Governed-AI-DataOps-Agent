"""Wire-level shapes of the Ecommerce MCP tools (inputs and structured outputs)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ecommerce.diagnostics import RELATIONSHIPS
from app.ecommerce.target import ECOMMERCE_TABLES

Status = Literal["ok", "issue_detected", "warning", "error"]
Severity = Literal["info", "low", "medium", "high", "critical", "unknown"]

Relationship = Literal[tuple(RELATIONSHIPS)]  # type: ignore[valid-type]
EcommerceTable = Literal[ECOMMERCE_TABLES]  # type: ignore[valid-type]

RelationshipArg = Annotated[Relationship, Field(description="Check one relationship only; omit to check all six.")]
TableArg = Annotated[EcommerceTable, Field(description="Limit to one Ecommerce table; omit for all nine.")]


class EcommerceResultModel(BaseModel):
    """One diagnostic finding. Mirrors ``DiagnosticResult.to_dict()``."""

    model_config = ConfigDict(extra="forbid")

    diagnostic_name: str
    status: Status = Field(description="ok | issue_detected | warning | error. 'error' means the diagnostic could not "
                                       "run (or the read-only guard refused the connection); it is not a finding.")
    severity: Severity
    summary: str
    evidence: list[dict[str, Any]]
    affected_records: list[str] = Field(description="Table tags such as 'table:ecom_orders'. Never record identifiers.")
    observed_value: Any = None
    expected_value: Any = None
    error_code: str | None = None
    recommended_next_step: str | None = None
    parameters: dict[str, Any]
    notes: list[str]
    evaluated_at: str | None = Field(default=None, description="UTC time of the evaluation.")


class EcommerceReportModel(BaseModel):
    """Roll-up of several results. Mirrors ``DiagnosticReport.to_dict()``."""

    model_config = ConfigDict(extra="forbid")

    overall_status: Status
    highest_severity: Severity
    counts: dict[str, int]
    evaluated_at: str | None = None
    results: list[EcommerceResultModel]
    ecommerce_summary: dict[str, Any] = Field(description="Execution status, target verification, and passed/failed/flagged/"
                                              "skipped/unassessed counts per check and per rule.")
