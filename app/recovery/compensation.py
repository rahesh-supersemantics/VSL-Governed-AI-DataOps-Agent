"""Design-only compensation planning. Nothing in this module writes, executes or schedules anything.

A plan states what a compensating step WOULD have to satisfy. It is never proof that compensation is possible:
``technically_possible`` is only ever ``unvalidated`` or ``not_offered``, ``executable`` is always False, and a
plan is always recorded as ``proposed_not_executed``. A compensating step would itself be a consequential action,
so it would need the same VSL gates, risk classification and human approval as any other; none is implemented.

When a recovery fails after it may have changed state, the runner records the plan and an explicit escalation to
a person (``recovery_escalated``). It never retries and never compensates automatically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PLAN_STATUS = "proposed_not_executed"


@dataclass(frozen=True)
class CompensationPlan:
    action_type: str
    strategy: str
    technically_possible: str            # "unvalidated" | "not_offered"
    scope: str
    preconditions: tuple[str, ...]
    idempotent: str                      # "unknown" until demonstrated
    expected_side_effects: str
    verification: str
    authorisation: str
    status: str = PLAN_STATUS
    executable: bool = False
    automatic: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["preconditions"] = list(self.preconditions)
        return out


_AUTH = "A compensating step is a consequential action: it needs its own VSL gates, risk classification and human approval."
_ESCALATE = "On failure or an unknown outcome: stop automated recovery, do not retry, and hand over to a person."


def plan_for(action_type: object, parameters: object) -> CompensationPlan:
    """Deterministic and code-only. Unknown input yields a 'not_offered' plan, never a made-up strategy."""
    params = parameters if isinstance(parameters, dict) else {}
    if action_type == "rerun_pipeline" and params.get("pipeline_id") in ("order_validation", "reporting_prep"):
        return CompensationPlan(
            "rerun_pipeline", "No undo is defined. The re-run reads business tables and appends bookkeeping only "
            "(a run record, validation rows, a log file, a simulator incident on failure) and advances the simulation clock.",
            "unvalidated", "run bookkeeping for one pipeline; no business rows",
            ("the run record identifies the run to review", "a person decides whether any bookkeeping needs correcting"),
            "unknown", "none beyond the bookkeeping already written",
            "Re-read the pipeline's latest run and compare it with the expected status.", _AUTH + " " + _ESCALATE)
    if action_type == "rerun_pipeline" and params.get("pipeline_id") == "order_ingestion":
        return CompensationPlan(
            "rerun_pipeline", "Not established. Ingestion inserts only orders whose source event id is not yet present, but the run "
            "record stores counts, not row identifiers, so the inserted rows cannot be named precisely from the record.",
            "unvalidated", "orders inserted by one run (not identifiable from the run record)",
            ("a way to identify exactly the rows this run inserted", "confirmation that none were used downstream"),
            "unknown", "removing orders could affect downstream reports",
            "Count orders by source event id before and after, with a person reviewing the difference.", _AUTH + " " + _ESCALATE)
    if action_type == "remove_injected_condition":
        return CompensationPlan(
            "remove_injected_condition", "Deliberately not offered. Reversing this means re-injecting the failure scenario, "
            "which is not a recovery.",
            "not_offered", "none", ("not applicable",), "unknown", "not applicable",
            "A person re-checks that the scenario is inactive and the injected rows are gone.", _AUTH + " " + _ESCALATE)
    return CompensationPlan(str(action_type)[:60], "No plan: the action is not recognised.", "not_offered", "none", ("not applicable",),
                            "unknown", "not applicable", "Not applicable.", _AUTH + " " + _ESCALATE)
