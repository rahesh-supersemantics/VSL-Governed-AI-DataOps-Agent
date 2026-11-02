"""Invalid lifecycle transitions are rejected by the real persistence layer, and the documented table agrees with it."""
import pytest

from app.recovery.lifecycle import ACTIVE_STATUSES, FINAL_STATUSES, transition_allowed
from tests.test_api_persistence import incident, make_recovery, store  # noqa: F401  (store is a fixture)


@pytest.mark.parametrize("final", FINAL_STATUSES)
def test_a_final_recovery_is_never_reopened_rewritten_claimed_or_released(store, final):
    rec = make_recovery(store, incident(store)["incident_id"])
    rid = rec["recovery_id"]
    assert store.record_recovery_outcome(rid, status=final, execution_status="uncertain" if "uncertain" in final else "not_run")
    before = store.get_recovery(rid)
    assert store.record_recovery_outcome(rid, status="executed", execution_status="executed", verified_outcome="verified") is False
    assert store.record_recovery_outcome(rid, status="awaiting_approval") is False
    assert store.claim_recovery_resume(rid) is False and store.release_recovery_claim(rid) is False
    after = store.get_recovery(rid)
    assert (after["status"], after["execution_status"], after["verified_outcome"]) == (before["status"], before["execution_status"], before["verified_outcome"])
    assert transition_allowed(final, "executed") is False and transition_allowed(final, "resuming") is False


def test_an_unknown_outcome_cannot_later_be_rewritten_as_success(store):
    rid = make_recovery(store, incident(store)["incident_id"])["recovery_id"]
    assert store.claim_recovery_resume(rid)
    assert store.record_recovery_outcome(rid, status="recovery_uncertain", execution_status="uncertain", verified_outcome="unknown", started=True)
    assert store.record_recovery_outcome(rid, status="executed", execution_status="executed", verified_outcome="verified") is False
    assert store.get_recovery(rid)["status"] == "recovery_uncertain"


def test_resuming_is_entered_only_by_one_atomic_claim_from_awaiting_approval(store):
    rid = make_recovery(store, incident(store)["incident_id"])["recovery_id"]
    assert store.release_recovery_claim(rid) is False          # nothing to release yet
    assert store.claim_recovery_resume(rid) is True and store.claim_recovery_resume(rid) is False
    assert store.release_recovery_claim(rid) is True and store.get_recovery(rid)["status"] == "awaiting_approval"


def test_the_documented_table_agrees_with_the_store(store):
    assert transition_allowed("awaiting_approval", "resuming") and not transition_allowed("resuming", "resuming")
    assert transition_allowed("resuming", "awaiting_approval") and transition_allowed("resuming", "executed")
    assert all(not transition_allowed(f, n) for f in FINAL_STATUSES for n in (*ACTIVE_STATUSES, *FINAL_STATUSES))
