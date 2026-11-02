"""Expired-but-still-stored-as-pending approvals: presentation and actionability must say 'expired'. Server-side enforcement is
unchanged and nothing is written by display calculations. Offline."""
from datetime import datetime, timedelta, timezone

import pytest

from app.recovery.expiry import effective_status, is_expired, parse_expiry
from app.recovery.lifecycle import logical_state
from dashboard import ops_data as d
from tests.api_support import api  # noqa: F401  (fixture)
from tests.recovery_support import Clock
from tests.test_api_governance import decide, pending

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def ap(status="pending", expires="2026-11-02T11:59:59Z"):
    return {"status": status, "expires_at": expires}


# ---- the single predicate and its contract
@pytest.mark.parametrize("approval,expected", [
    (ap(expires="2026-11-02T11:59:59Z"), True),                 # past
    (ap(expires="2026-11-02T12:00:00Z"), True),                 # the boundary instant counts as expired
    (ap(expires="2026-11-02T12:00:01Z"), False),                # unexpired
    (ap(expires="2026-11-02T11:59:59.476354Z"), True),          # the stored format (microseconds, Z)
    (ap(expires="2026-11-03T00:59:59+13:00"), True),            # an offset is honoured: that is 11:59:59 UTC
    (ap(expires="2026-11-02T11:59:59"), True),                  # no zone: read as UTC (the Human Review contract)
    (ap(expires=None), False), (ap(expires=""), False), (ap(expires="not a date"), False), ({"status": "pending"}, False),
    (ap(status="approved"), False), (ap(status="rejected"), False), (ap(status="consumed"), False), (ap(status="expired"), False),
    (None, False), ({}, False),
])
def test_is_expired(approval, expected):
    assert is_expired(approval, NOW) is expected


def test_invalid_or_missing_expiry_is_not_reinterpreted():
    for bad in (None, "", "garbage", 12345):
        assert effective_status(ap(expires=bad), NOW) == "pending" and parse_expiry(bad) is None


def test_a_naive_now_is_read_as_utc_and_no_exception_is_raised():
    assert is_expired(ap(), NOW.replace(tzinfo=None)) is True


def test_effective_status_never_mutates_its_input():
    a = ap()
    assert effective_status(a, NOW) == "expired" and a == ap()


def test_logical_state():
    rec = {"status": "awaiting_approval"}
    assert logical_state(rec, ap(), now=NOW)["state"] == "Expired"
    assert logical_state(rec, ap(), now=NOW)["exactness"] == "derived" and logical_state(rec, ap(), now=NOW)["stored_status"] == "awaiting_approval"
    assert logical_state(rec, ap(expires="2026-11-02T12:30:00Z"), now=NOW)["state"] == "PendingReview"
    assert logical_state(rec, ap())["state"] == "PendingReview"  # no clock given: stored status, unchanged behaviour
    assert logical_state(rec, ap(status="approved"), now=NOW)["state"] == "Authorized"  # decided before expiry stays authorised


# ---- dashboard helpers (they only read the API's fields)
def rec_json(status="awaiting_approval", phase="awaiting_approval", approval_status="pending", effective="pending"):
    return {"status": status, "phase": phase, "approval_request_id": "apr-x", "execution_status": "not_run", "verified_outcome": "not_applicable",
            "approval": {"status": approval_status, "effective_status": effective}}


def test_dashboard_helpers():
    assert d.approval_actionable(rec_json()) is True
    assert d.approval_actionable(rec_json(effective="expired")) is False
    assert d.approval_actionable(rec_json(effective=None)) is True       # an older API without the field: stored status, as before
    s = d.recovery_summary(rec_json(phase="approval_expired", effective="expired"))
    assert "EXPIRED" in s["headline"] and s["tone"] == "warning" and "Awaiting" not in s["headline"]
    assert "Awaiting human approval" in d.recovery_summary(rec_json())["headline"]
    assert "approval_expired" in d.WORKFLOW_ORDER and "approval_expired" in d.RECOVERY_WORKFLOWS


def test_overview_metric_does_not_count_an_expired_request_as_awaiting():
    loaded = d.LoadedIncidents(items=[{"workflow_status": "awaiting_approval", "severity": "high"},
                                      {"workflow_status": "approval_expired", "severity": "high"}], total=2)
    m = {x.label: x.value for x in d.overview_metrics(loaded)}
    assert m["Awaiting approval"] == 1 and m["Open high/critical"] == 2  # still an unresolved incident, just not awaiting


# ---- the confirmed defect, reproduced end to end through the real API and fixed
def test_an_overdue_pending_request_is_presented_as_expired_everywhere_and_nothing_is_written(api):
    clock = Clock()
    e = api(clock=clock)
    iid, rec, req = pending(e)
    # before expiry: everything says pending
    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "awaiting_approval"
    assert e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()["approval"]["effective_status"] == "pending"
    clock.advance(minutes=31)  # the request lives 30 minutes
    stored_before = e.services.approvals.store.get(req).status
    assert stored_before == "pending"  # the confirmed condition: stored as pending although overdue

    assert e.get(f"/incidents/{iid}/status", "reader").json()["workflow_status"] == "approval_expired"
    incident = [i for i in e.get("/incidents", "reader").json()["items"] if i["incident_id"] == iid][0]
    assert incident["workflow_status"] == "approval_expired"
    got = e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["phase"] == "approval_expired" and got["status"] == "awaiting_approval"
    assert got["approval"]["status"] == "pending" and got["approval"]["effective_status"] == "expired"
    assert e.get(f"/approvals/{req}", "reader").json()["effective_status"] == "expired"
    assert d.approval_actionable(got) is False and "EXPIRED" in d.recovery_summary(got)["headline"]
    gov = e.get(f"/recoveries/{rec['recovery_id']}/governance", "reader").json()
    assert gov["lifecycle"]["state"] == "Expired" and gov["request"]["expired"] is True
    assert gov["execution"]["attempted"] is False and gov["authorisation"]["kind"] == "none"
    assert e.get("/reviews?state=pending", "reader").json()["total"] == 0  # Human Review already agreed
    # display calculations wrote nothing
    assert e.services.approvals.store.get(req).status == "pending"
    assert e.store.get_recovery(rec["recovery_id"])["status"] == "awaiting_approval"


def test_server_side_enforcement_is_unchanged_an_expired_request_cannot_be_approved_or_run(api):
    clock = Clock()
    e = api(clock=clock)
    _, rec, req = pending(e)
    clock.advance(minutes=31)
    r = decide(e, req)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "expired"
    assert e.env.executor.count == 0
    assert e.services.approvals.store.get(req).status == "expired"  # the existing lazy write, made by the decision attempt only


def test_an_unexpired_request_is_still_pending_and_actionable(api):
    clock = Clock()
    e = api(clock=clock)
    iid, rec, req = pending(e)
    clock.advance(minutes=29)
    got = e.get(f"/recoveries/{rec['recovery_id']}", "reader").json()
    assert got["phase"] == "awaiting_approval" and got["approval"]["effective_status"] == "pending" and d.approval_actionable(got) is True
    assert decide(e, req).status_code == 200
