"""The catalogue must point at real code and real tests, and the lifecycle/compensation modules must not overclaim."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from app.recovery.compensation import PLAN_STATUS, plan_for
from app.recovery.governance_catalogue import INVARIANTS, PRENODES
from app.recovery.lifecycle import LOGICAL_STATES, STATE_MAP, logical_state

ROOT = Path(__file__).resolve().parents[1]
ALL = PRENODES + INVARIANTS


def test_catalogue_is_complete_and_ids_are_unique():
    assert [e.id for e in PRENODES] == [f"PN-0{i}" for i in range(1, 9)]
    assert [e.id for e in INVARIANTS] == [f"INV-{i:02d}" for i in range(1, 23)]
    assert len({e.id for e in ALL}) == 30
    assert all(e.status in ("enforced", "partial", "not_enforced") and e.layer in ("vsl_gate", "code") for e in ALL)


@pytest.mark.parametrize("entry", ALL, ids=lambda e: e.id)
def test_every_enforcement_symbol_resolves(entry):
    for ref in entry.enforcement:
        module, _, qual = ref.partition(":")
        obj = importlib.import_module(module)
        for part in qual.split("."):
            obj = getattr(obj, part)  # AttributeError = the catalogue points at nothing


@pytest.mark.parametrize("entry", ALL, ids=lambda e: e.id)
def test_every_referenced_test_exists_and_enforced_entries_have_one(entry):
    for ref in entry.tests:
        path, _, name = ref.partition("::")
        assert re.search(rf"^def {re.escape(name)}\(", (ROOT / path).read_text(encoding="utf-8"), re.M), ref
    if entry.status == "enforced":
        assert entry.enforcement and entry.tests, f"{entry.id} claims enforced without code and a test"
    if entry.status != "enforced":
        assert entry.limitation, f"{entry.id} must state its limitation"


def test_unimplemented_requirements_are_not_claimed():
    status = {e.id: e.status for e in ALL}
    assert status["INV-20"] == "not_enforced" and status["INV-14"] == "partial"  # INV-14 is structural only; wording quality is unverified


def test_every_acceptance_test_t03_to_t21_is_present():
    text = (ROOT / "tests/test_governance_acceptance.py").read_text(encoding="utf-8")
    for n in range(3, 22):
        assert re.search(rf"^def test_t{n:02d}_", text, re.M), f"T-{n:02d} missing"


# ---- compensation: design only
@pytest.mark.parametrize("action,params", [("rerun_pipeline", {"pipeline_id": "order_ingestion"}),
                                           ("remove_injected_condition", {"scenario_key": "duplicate-records"}),
                                           ("nonsense", {}), (None, None)])
def test_a_compensation_plan_is_never_executable_automatic_or_executed(action, params):
    plan = plan_for(action, params)
    assert plan.status == PLAN_STATUS == "proposed_not_executed"
    assert plan.executable is False and plan.automatic is False
    with pytest.raises(Exception):
        plan.executable = True  # frozen


def test_compensation_module_has_no_way_to_write():
    src = (ROOT / "app/recovery/compensation.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(import (sqlalchemy|sqlite3)|\.execute\(|\.commit\()", src)


# ---- lifecycle: honest mapping
def test_every_logical_state_is_mapped_and_unrepresentable_ones_say_so():
    assert set(STATE_MAP) == set(LOGICAL_STATES)
    assert {s for s, v in STATE_MAP.items() if v[0] == "not_stored"} == {
        "Investigating", "Proposed", "ReadyToExecute", "Compensating", "Compensated"}


@pytest.mark.parametrize("status,expected", [("awaiting_approval", "PendingReview"), ("resuming", "Executing"), ("executed", "Succeeded"),
                                              ("recovery_uncertain", "OutcomeUnknown"), ("recovery_failed", "Failed"),
                                              ("recovery_verification_failed", "Failed"), ("expired", "Expired"),
                                              ("denied_human_review", "Rejected"), ("approval_already_used", "Rejected")])
def test_stored_statuses_map_to_the_expected_logical_state(status, expected):
    assert logical_state({"status": status}, None)["state"] == expected


def test_an_approved_unspent_request_is_authorised_and_nothing_else_is():
    assert logical_state({"status": "awaiting_approval"}, {"status": "approved"})["state"] == "Authorized"
    assert logical_state({"status": "awaiting_approval"}, {"status": "pending"})["state"] == "PendingReview"


@pytest.mark.parametrize("status", ["", "weird", "pending", "succeeded", "ok"])
def test_an_unknown_status_is_never_reported_as_success(status):
    assert logical_state({"status": status}, None)["state"] not in ("Succeeded", "Authorized")


def test_escalation_is_derived_only_for_failed_or_unknown_outcomes():
    assert logical_state({"status": "recovery_uncertain"}, None, escalated=True)["state"] == "Escalated"
    assert logical_state({"status": "recovery_verification_failed"}, None, escalated=True)["state"] == "Escalated"
    assert logical_state({"status": "executed"}, None, escalated=True)["state"] == "Succeeded"


# ---- latest brief: T-01..T-22 map, traceability, PreNode spec
def _test_exists(ref):
    path, _, name = ref.partition("::")
    return re.search(rf"^def {re.escape(name)}\(", (ROOT / path).read_text(encoding="utf-8"), re.M) is not None


def test_every_latest_t_number_is_mapped_to_tests_that_exist():
    from tests.acceptance_map import MAP
    assert list(MAP) == [f"T-{i:02d}" for i in range(1, 23)]
    for tid, (title, refs) in MAP.items():
        assert refs, tid
        for ref in refs:
            assert _test_exists(ref), (tid, ref)


def test_t20_is_pending_and_its_real_call_is_skipped_not_passed():
    from tests.acceptance_map import MAP
    assert "PENDING" in MAP["T-20"][0]
    src = (ROOT / "tests/test_governance_acceptance.py").read_text(encoding="utf-8")
    assert re.search(r'@pytest\.mark\.skip\(reason="NOT RUN[^\n]*\)\ndef test_t20_live_mode_invokes_the_real_configured_model', src)


def test_traceability_covers_every_invariant_with_real_prenodes_and_ledger_outcomes():
    from app.recovery.governance_catalogue import PRENODE_SPEC, TRACE
    assert set(TRACE) == {e.id for e in INVARIANTS} and set(PRENODE_SPEC) == {e.id for e in PRENODES}
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").rglob("*.py"))
    for inv, (nodes, outcomes, dashboard) in TRACE.items():
        assert set(nodes) <= set(PRENODE_SPEC), inv
        for name in outcomes:
            assert f'"{name}"' in source, (inv, name)       # the ledger outcome is really written somewhere
        assert dashboard
    for pn, (_, _, _, events) in PRENODE_SPEC.items():
        for name in (n.strip() for n in events.split(",")):
            assert f'"{name}"' in source, (pn, name)


def test_the_generated_doc_names_every_id_and_t_number():
    doc = (ROOT / "docs/governance-catalogue.md").read_text(encoding="utf-8")
    for e in PRENODES + INVARIANTS:
        assert e.id in doc
    acc = (ROOT / "docs/governance-acceptance-tests.md").read_text(encoding="utf-8")
    for i in range(1, 23):
        assert f"| T-{i:02d} |" in acc


def test_generated_docs_are_up_to_date_with_the_code():
    from scripts.generate_governance_docs import acceptance, catalogue
    assert (ROOT / "docs/governance-catalogue.md").read_text(encoding="utf-8") == catalogue()
    assert (ROOT / "docs/governance-acceptance-tests.md").read_text(encoding="utf-8") == acceptance()
