"""Per-PreNode verdicts, derived ONLY from what the ledger recorded for one recovery.

Four values: ``passed``, ``failed``, ``unknown`` (the outcome could not be established) and ``not_recorded`` (the ledger holds
nothing that settles this stage, which is NOT a pass). ``basis`` says whether the verdict was written directly
(``recorded``) or follows from the fixed order of the stages (``implied_by_later_step``: for example the target check always
runs before the effect, so a recorded effect implies it passed). Nothing is inferred from the absence of an entry.
"""

from __future__ import annotations

from typing import Any

PRENODES = ("PN-01", "PN-02", "PN-03", "PN-04", "PN-05", "PN-06", "PN-07", "PN-08")
EFFECT_OUTCOMES = ("recovery_succeeded", "recovery_verification_failed", "recovery_uncertain")
EVIDENCE_REASONS = ("report_status_not_actionable", "evidence_")  # prefixes of PN-02 failure reasons
SAFE_KEYS = ("outcome", "failed_check", "failed", "state", "code", "idempotency_key")


def _v(verdict: str, basis: str, outcomes: list[str], note: str = "") -> dict[str, Any]:
    return {"verdict": verdict, "basis": basis, "outcomes": outcomes, "note": note}


def derive_prenode_verdicts(payloads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    outcomes = [str(p.get("outcome")) for p in payloads]
    has = lambda *names: [o for o in outcomes if o in names]  # noqa: E731
    effect = has(*EFFECT_OUTCOMES)
    out: dict[str, dict[str, Any]] = {pn: _v("not_recorded", "none", []) for pn in PRENODES}

    granted = has("approval_granted", "approval_granted_by_policy")
    if granted:
        out["PN-01"] = _v("passed", "recorded", granted, "an authorised identity decided")
    elif has("approval_attempt_refused"):
        out["PN-01"] = _v("failed", "recorded", has("approval_attempt_refused"), "a caller was refused")

    early_denied = [p for p in payloads if p.get("outcome") == "preconditions_failed"
                    and any(str(r).startswith(EVIDENCE_REASONS) for r in (p.get("failed") or []))]
    if early_denied:
        reasons = sorted({str(r) for p in early_denied for r in p.get("failed", []) if str(r).startswith(EVIDENCE_REASONS)})
        out["PN-02"] = _v("failed", "recorded", ["preconditions_failed"], ", ".join(reasons))
    elif has("approval_requested"):
        out["PN-02"] = _v("passed", "implied_by_later_step", ["approval_requested"], "a request exists only after the evidence check passed")

    if has("recovery_action_rejected"):
        out["PN-03"] = _v("failed", "recorded", ["recovery_action_rejected"])
    elif has("approval_requested"):
        out["PN-03"] = _v("passed", "implied_by_later_step", ["approval_requested"], "a request exists only after the action was validated")

    target_failed = [p for p in payloads if p.get("outcome") == "recovery_failed" and p.get("failed_check") == "target"]
    pre_failed = [p for p in payloads if p.get("outcome") == "recovery_failed" and p.get("failed_check") == "precondition"]
    if target_failed:
        out["PN-04"] = _v("failed", "recorded", ["recovery_failed"], "target check refused")
    elif effect:
        out["PN-04"] = _v("passed", "implied_by_later_step", effect, "the target check precedes the effect")

    if has("approval_not_usable"):
        out["PN-05"] = _v("failed", "recorded", ["approval_not_usable"])
    elif has("authorised"):
        out["PN-05"] = _v("passed", "recorded", ["authorised"], "the compiled gates passed and wrote the authorisation")

    if pre_failed:
        out["PN-06"] = _v("failed", "recorded", ["recovery_failed"], "precondition refused")
    elif effect:
        out["PN-06"] = _v("passed", "implied_by_later_step", effect, "preconditions are checked before the effect")

    if any(p.get("outcome") == "recovery_blocked" and p.get("idempotency_key") for p in payloads):
        out["PN-07"] = _v("failed", "recorded", ["recovery_blocked"], "duplicate or unreconciled operation")
    elif effect:
        out["PN-07"] = _v("passed", "implied_by_later_step", effect, "the claim and the single-use approval precede the effect")

    if has("recovery_succeeded"):
        out["PN-08"] = _v("passed", "recorded", ["recovery_succeeded"], "postcondition independently verified")
    elif has("recovery_verification_failed"):
        out["PN-08"] = _v("failed", "recorded", ["recovery_verification_failed"], "postcondition not met")
    elif has("recovery_uncertain"):
        out["PN-08"] = _v("unknown", "recorded", ["recovery_uncertain"], "the outcome could not be established")
    return out
