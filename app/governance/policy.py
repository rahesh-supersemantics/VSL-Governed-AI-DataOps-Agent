"""VSL policy declarations for protected actions. Imports ``vsl_core`` (and the standard library) only.

What is governed
----------------
Only a *protected action*: a hypothetical write/recovery step proposed after an investigation. Phase 6 has no
real recovery action; the thing behind the gates is a no-op stub (``app.governance.actions``). The eight
read-only MCP tools and the investigation graph are NOT gated.

Constructs
----------
* Invariant ``no_execution_while_suspended``   - hard: nothing runs while a TerminalState is open.
* Invariant ``action_within_allowlist``        - hard: an action not on ``ALLOWED_ACTIONS`` is never allowed.
* PreNode  ``verified_report_and_approval``    - soft: a failed precondition routes to human review.
* TerminalState ``automation_suspended``       - entered on an invariant violation; cleared only by a
  human-authorised transition (see ``app.governance.ledger``).

Honesty notes
-------------
* The PreNode monitor is deterministic. VSL's PreNode contract needs a ``GammaEstimate``, so the monitor
  encodes "every precondition holds" as ``PASS_GAMMA`` and "at least one fails" as ``FAIL_GAMMA``. These are
  NOT calibrated confidences and not a measured Gamma, and model confidence is never an input.
* Assurance is derived from F1/F2 facts: the gates run before the action (F1 true) but do not modify any
  model energy function (F2 NONE), so every construct derives LOW. That is the honest level.
* ``ApprovalContext`` is a plain, unauthenticated value in Phase 6. Nothing issues or verifies approvals until
  the Phase 7 approval workflow, so by default no action is approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from vsl_core.constructs import Invariant, PreNode, TerminalState
from vsl_core.metrics import (
    ROBUST_GAMMA_DEFAULT_THRESHOLD,
    AssuranceBasis,
    F2Modification,
    GammaEstimate,
)

# Actions the governed boundary may ever run. Both are dry-run labels; neither touches a database.
ALLOWED_ACTIONS: frozenset[str] = frozenset({"rerun_validation_dry_run", "restart_stage_dry_run"})

# Report status that justifies proposing any action at all.
ACTIONABLE_REPORT_STATUS = "issue_identified"

# Binary encoding of "all preconditions hold" for the PreNode contract (see module docstring).
PASS_GAMMA = 2.0
FAIL_GAMMA = 0.0

TERMINAL_NAME = "automation_suspended"
PRE_NODE_NAME = "verified_report_and_approval"
INVARIANT_ALLOWLIST = "action_within_allowlist"
INVARIANT_SUSPENDED = "no_execution_while_suspended"

# F1: the gate completes before the action. F2 NONE: it does not touch a model's energy function.
GATE_BASIS = AssuranceBasis(f1_pre_commitment=True, f2_modification=F2Modification.NONE)


@dataclass(frozen=True)
class ApprovalContext:
    """Who approved which action. Unauthenticated in Phase 6 (see module docstring)."""

    approved_by: str
    approved_action: str
    reference: str = ""

    def is_valid_for(self, action: str) -> bool:
        return bool(self.approved_by.strip()) and self.approved_action == action


# ---- evidence provenance and freshness (evidence-policy-v1)
EVIDENCE_POLICY_VERSION = "evidence-policy-v1"
# Only these evidence statuses can support a recovery: a diagnostic observed a problem. "ok" (nothing found) and "error"
# (the check did not work) are facts, but they cannot justify changing anything.
SUPPORTING_EVIDENCE_STATUSES = frozenset({"issue_detected", "warning"})
CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class EvidenceMeta:
    """Explicit metadata of one evidence item. It describes a tool observation, never model text."""

    evidence_id: str
    tool: str                       # the diagnostic that produced it
    status: str                     # as reported by that diagnostic
    collected_at: str               # ISO-8601 with a UTC offset, stamped by code when the result arrived
    investigation_id: str = ""      # the investigation the item belongs to


@dataclass(frozen=True)
class EvidencePolicy:
    """What counts as acceptable evidence for a protected action. Documented in docs/governance-evidence-policy.md."""

    known_tools: frozenset[str]
    max_age: timedelta = timedelta(minutes=60)
    version: str = EVIDENCE_POLICY_VERSION


def _parse_utc(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None  # a time without an offset is unverifiable


def evidence_failures(cited: tuple[str, ...], meta: tuple[EvidenceMeta, ...], policy: EvidencePolicy, *,
                      now: datetime | None, investigation_id: str | None) -> tuple[str, ...]:
    """Reasons the cited evidence is not acceptable (empty = acceptable). Missing or unverifiable data is a failure, never a pass."""
    if now is None or now.tzinfo is None:
        return ("evidence_check_time_unavailable",)
    by_id = {m.evidence_id: m for m in meta}
    failed: set[str] = set()
    for ref in cited:
        item = by_id.get(ref)
        if item is None:
            failed.add("evidence_metadata_missing")
            continue
        if item.tool not in policy.known_tools:
            failed.add("evidence_source_unverified")
        if item.status not in SUPPORTING_EVIDENCE_STATUSES:
            failed.add("evidence_does_not_support_action")
        if investigation_id and item.investigation_id != investigation_id:
            failed.add("evidence_from_other_investigation")
        when = _parse_utc(item.collected_at)
        if when is None:
            failed.add("evidence_timestamp_invalid")
        elif when - now.astimezone(timezone.utc) > CLOCK_SKEW:
            failed.add("evidence_timestamp_in_future")
        elif now.astimezone(timezone.utc) - when > policy.max_age:
            failed.add("evidence_stale")
    return tuple(sorted(failed))


@dataclass(frozen=True)
class GateInput:
    """Everything a gate may look at. All of it is verifiable data; none of it is model confidence."""

    action: str
    report_status: str
    cited_evidence_ids: tuple[str, ...] = ()
    known_evidence_ids: tuple[str, ...] = ()
    approval: ApprovalContext | None = None
    suspended: bool = False
    # Opaque, caller-built data handed to the action runner (never read by a gate). Phase 7 puts the
    # validated request id and parameters here.
    context: object | None = None
    # Evidence provenance/freshness. When ``evidence_policy`` is set (every recovery path sets it) the cited evidence must
    # also satisfy it. ``evidence_policy=None`` keeps the original Phase 6 behaviour for callers that do not use evidence metadata.
    evidence: tuple[EvidenceMeta, ...] = ()
    evidence_policy: EvidencePolicy | None = None
    investigation_id: str | None = None
    evaluated_at: datetime | None = None


def failed_preconditions(candidate: GateInput) -> tuple[str, ...]:
    """Names of the PreNode preconditions that do not hold (empty = all hold)."""
    failed: list[str] = []
    if candidate.report_status != ACTIONABLE_REPORT_STATUS:
        failed.append("report_status_not_actionable")
    known = set(candidate.known_evidence_ids)
    cited = candidate.cited_evidence_ids
    if not cited or not all(item in known for item in cited):
        failed.append("evidence_not_cited_or_unknown")
    if candidate.evidence_policy is not None:
        failed.extend(evidence_failures(candidate.cited_evidence_ids, candidate.evidence, candidate.evidence_policy,
                                        now=candidate.evaluated_at, investigation_id=candidate.investigation_id))
    if candidate.approval is None or not candidate.approval.is_valid_for(candidate.action):
        failed.append("approval_missing_or_mismatched")
    return tuple(failed)


async def _preconditions_monitor(candidate: GateInput) -> GammaEstimate:
    return GammaEstimate(gamma_hat=FAIL_GAMMA if failed_preconditions(candidate) else PASS_GAMMA)


async def _not_suspended_rule(candidate: GateInput) -> bool:
    return not candidate.suspended


@dataclass(frozen=True)
class Policy:
    pre_node: PreNode
    allowlist_invariant: Invariant
    suspension_invariant: Invariant
    terminal: TerminalState
    allowed_actions: frozenset[str] = field(default=ALLOWED_ACTIONS)


def build_policy(allowed_actions: frozenset[str] = ALLOWED_ACTIONS) -> Policy:
    allowed = frozenset(allowed_actions)

    async def _allowlist_rule(candidate: GateInput) -> bool:
        return candidate.action in allowed

    terminal = TerminalState(
        name=TERMINAL_NAME,
        description="Protected actions are suspended. Cleared only by a human-authorised transition.",
        entry_conditions=("an invariant was violated",),
        requires_human_authorised_transition=True,
    )
    return Policy(
        pre_node=PreNode(
            name=PRE_NODE_NAME,
            description="Actionable report, cited known evidence and a matching approval are required.",
            monitor=_preconditions_monitor,
            assurance_basis=GATE_BASIS,
            gamma_threshold=ROBUST_GAMMA_DEFAULT_THRESHOLD,
        ),
        allowlist_invariant=Invariant(
            name=INVARIANT_ALLOWLIST,
            description="Only allowlisted protected actions may ever run.",
            rule=_allowlist_rule,
            assurance_basis=GATE_BASIS,
            on_violation=terminal,
        ),
        suspension_invariant=Invariant(
            name=INVARIANT_SUSPENDED,
            description="No protected action runs while a terminal state is open.",
            rule=_not_suspended_rule,
            assurance_basis=GATE_BASIS,
            on_violation=terminal,
        ),
        terminal=terminal,
        allowed_actions=allowed,
    )
