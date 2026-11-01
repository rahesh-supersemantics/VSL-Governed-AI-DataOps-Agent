"""Mapping from the requested logical lifecycle to the states this application actually stores.

This module invents no transitions and persists nothing. It reads an existing recovery row, its approval summary and
whether an escalation entry exists in the ledger, and reports the closest logical state together with HOW exact the
mapping is. Where the application has no such state, ``STATE_MAP`` says so instead of pretending.

``direct``        the stored status IS this state.
``derived``       computed from stored data (for example an approval that is approved but not yet spent).
``approximate``   the closest logical state; the stored reason is shown next to it and differs in detail.
``not_stored``    the application does not hold this state (it is transient inside a step, or not implemented).
"""

from __future__ import annotations

from typing import Any

LOGICAL_STATES = ("Investigating", "Proposed", "PendingReview", "Authorized", "ReadyToExecute", "Executing", "Succeeded",
                  "Failed", "OutcomeUnknown", "Rejected", "Expired", "Compensating", "Compensated", "Escalated")

# logical state -> (how it is represented, where, note)
STATE_MAP: dict[str, tuple[str, str, str]] = {
    "Investigating": ("not_stored", "investigations.status (not a recovery state)",
                      "Investigation progress is stored on the investigation, before any recovery exists."),
    "Proposed": ("not_stored", "transient inside the workflow",
                 "A proposal is validated and an approval request is created in one step; no row is stored as 'proposed'."),
    "PendingReview": ("direct", "recovery.status = awaiting_approval with approval status pending", ""),
    "Authorized": ("derived", "approval status approved and not yet consumed",
                   "Set by a person's approval or by a policy authorisation; the approver identity shows which."),
    "ReadyToExecute": ("not_stored", "transient inside RecoveryRunner.execute",
                       "The pre-execution checks run inside the runner; nothing is stored between 'Authorized' and 'Executing'."),
    "Executing": ("direct", "recovery.status = resuming", "Held by an atomic claim so only one caller executes."),
    "Succeeded": ("direct", "recovery.status = executed", "Only after the independent postcondition check passed."),
    "Failed": ("approximate", "recovery_failed (before any effect) or recovery_verification_failed (effect ran, postcondition unmet)",
               "The two stored reasons differ and are shown separately."),
    "OutcomeUnknown": ("direct", "recovery.status = recovery_uncertain", ""),
    "Rejected": ("approximate", "rejected, action_rejected, denied_human_review, cancelled, blocked, approval_invalid, "
                                "approval_already_used, refused_suspended, terminal_suspended",
                 "Several different stored reasons collapse into this logical state; the stored reason is always shown."),
    "Expired": ("direct", "approval/recovery status = expired", ""),
    "Compensating": ("not_stored", "not implemented", "By decision only a design-only compensation plan is produced; no compensating step runs."),
    "Compensated": ("not_stored", "not implemented", "Same as Compensating."),
    "Escalated": ("derived", "a 'recovery_escalated' ledger entry exists",
                  "Written when the outcome is verification_failed or uncertain; it hands over to a person and stops automation."),
}

_REJECTED = {"rejected", "action_rejected", "denied_human_review", "cancelled", "blocked", "recovery_blocked",
             "approval_invalid", "approval_already_used", "refused_suspended", "terminal_suspended"}


def logical_state(recovery: dict[str, Any], approval: dict[str, Any] | None, *, escalated: bool = False) -> dict[str, str]:
    """{'state', 'stored_status', 'exactness', 'note'} for one recovery."""
    status = str(recovery.get("status", ""))
    verified = str(recovery.get("verified_outcome", ""))
    base: tuple[str, str, str]
    if status == "awaiting_approval":
        approved = bool(approval) and approval.get("status") == "approved"
        base = ("Authorized", "derived", "approved but not yet spent") if approved else ("PendingReview", "direct", "")
    elif status == "resuming":
        base = ("Executing", "direct", "")
    elif status == "executed":
        base = ("Succeeded", "direct", "postcondition independently verified")
    elif status == "recovery_uncertain" or (status == "governance_error" and verified == "unknown"):
        base = ("OutcomeUnknown", "direct" if status == "recovery_uncertain" else "approximate",
                "" if status == "recovery_uncertain" else "a governance error followed a possible effect")
    elif status in ("recovery_failed", "recovery_verification_failed", "governance_error"):
        note = {"recovery_failed": "failed before any effect", "recovery_verification_failed": "ran, but the postcondition was not met",
                "governance_error": "a governance error stopped the workflow"}[status]
        base = ("Failed", "approximate", note)
    elif status == "expired":
        base = ("Expired", "direct", "")
    elif status in _REJECTED:
        base = ("Rejected", "approximate", status.replace("_", " "))
    else:
        base = ("Failed", "approximate", "unrecognised stored status; shown as Failed, never as success")
    state, exactness, note = base
    if escalated and state in ("Failed", "OutcomeUnknown"):
        return {"state": "Escalated", "stored_status": status, "exactness": "derived",
                "note": f"escalated to a person after: {state}" + (f" ({note})" if note else "")}
    return {"state": state, "stored_status": status, "exactness": exactness, "note": note}
