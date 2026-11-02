"""One read-only view of what governance did for a recovery, built only from stored state and ledger outcomes.

Nothing here executes, retries or writes. It keeps four distinctions apart, because conflating them is how a UI overstates safety:
authorisation by POLICY vs approval by a PERSON; a compensation PLAN (proposed) vs compensation that ran (never, by design);
an execution that was ATTEMPTED vs an outcome that was independently VERIFIED; and a recorded ESCALATION.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.recovery.compensation import plan_for
from app.recovery.expiry import is_expired
from app.recovery.lifecycle import logical_state
from app.recovery.prenode_verdicts import derive_prenode_verdicts
from app.recovery.risk_policy import POLICY_PRINCIPAL


def build_governance_view(recovery: dict[str, Any], approval: dict[str, Any] | None, events: list[dict[str, Any]],
                          investigation: dict[str, Any] | None = None, payloads: list[dict[str, Any]] | None = None,
                          now: datetime | None = None) -> dict[str, Any]:
    outcomes = [str(e.get("outcome")) for e in events]
    decided_by = (approval or {}).get("decided_by")
    status = (approval or {}).get("status")
    if not approval or not decided_by or status in ("pending", "expired"):
        kind = "none"
    elif status == "rejected":
        kind = "rejected_by_person" if not str(decided_by).startswith("policy:") else "none"
    elif str(decided_by) == POLICY_PRINCIPAL or "approval_granted_by_policy" in outcomes:
        kind = "policy"
    else:
        kind = "human"
    escalated = "recovery_escalated" in outcomes
    attempted = "recovery_attempt" in outcomes
    verified = recovery.get("status") == "executed" and "recovery_succeeded" in outcomes
    plan = plan_for(recovery.get("action_type"), recovery.get("parameters")).as_dict() if attempted else None
    report = (investigation or {}).get("report") or {}
    mode = (investigation or {}).get("mode")
    analysis = {"mode": mode if mode in ("offline", "live", "scripted") else None,
                "note": {"offline": "Offline deterministic analysis: no model was called.", "live": "Live model analysis.",
                         "scripted": "Scripted test model."}.get(mode, "Analysis mode not recorded."),
                "observed_findings": len(report.get("observed_findings") or []) if report else None,
                "hypotheses": len(report.get("hypotheses") or []) if report else None,
                "evidence_refs": list((investigation or {}).get("evidence_refs") or [])}
    request = None if not approval else {k: approval.get(k) for k in ("risk", "target", "evidence_refs", "report_status", "expires_at", "status")}
    if request is not None:
        request["expired"] = bool(now is not None and is_expired(approval, now))  # presentation only; the stored status is untouched
    return {
        "recovery_id": recovery.get("recovery_id"),
        "analysis": analysis, "request": request,
        "prenodes": derive_prenode_verdicts(payloads if payloads is not None else [{"outcome": o} for o in outcomes]),
        "timestamps": {k: recovery.get(k) for k in ("created_at", "started_at", "completed_at")},
        "lifecycle": logical_state(recovery, approval, escalated=escalated, now=now),
        "authorisation": {"kind": kind, "by": decided_by if kind in ("policy", "human", "rejected_by_person") else None,
                          "note": {"policy": "Authorised by a versioned policy. No person approved this.",
                                   "human": "Approved by a person.", "rejected_by_person": "A person rejected this request.",
                                   "none": "Not authorised."}[kind]},
        "execution": {"attempted": attempted, "verified_success": verified,
                      "note": ("Independently verified: the postcondition was re-checked." if verified else
                               "Attempted, but the result is NOT verified as success." if attempted else "Not attempted.")},
        "compensation": {"plan": plan, "executed": False,
                         "note": "A plan was proposed for a person to consider. No compensating step has run or can run automatically."
                         if plan else "No plan: nothing was attempted."},
        "escalated": escalated,
        "ledger_outcomes": outcomes,
    }
