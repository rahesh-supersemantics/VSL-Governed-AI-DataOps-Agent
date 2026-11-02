"""STUBBED verification of the real Operations Dashboard governance panel (Streamlit AppTest, headless).

This drives the real dashboard code against canned '[STUB]' API responses (scripts/stub_governance_demo.py). It is NOT a browser
run and NOT live end-to-end: the data is invented, and the governance views come from the real builder fed synthetic ledger
outcomes. It proves what the panel RENDERS for each shape of response."""

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from dashboard import runtime
from dashboard.ops_client import OpsClient
from scripts import stub_governance_demo as stub
from tests.test_phase10_dashboard_app import APP, go, text_of


def transport(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"  # the stub never receives a write in these tests
    assert request.headers.get("authorization") == f"Bearer {stub.STUB_TOKEN}"
    status, body = stub.route(request.url.path, {})
    return httpx.Response(status, json=body)


@pytest.fixture
def shown():
    runtime.set_override("ops", OpsClient("http://stub/api/v1", stub.STUB_TOKEN, http=httpx.Client(transport=httpx.MockTransport(transport))))

    def render(sid):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        go(at, "Recovery & approvals")
        at.selectbox(key="rec_inc").set_value(sid)
        at.run()
        assert not at.exception, at.exception
        return text_of(at)

    yield render
    runtime.set_override("ops", None)


def rows(text):
    return {line.split(".")[0].strip("* ").strip(): line for line in text.split("\n") if line.strip().startswith("**")}


def test_every_prenode_row_is_shown_with_its_basis_for_a_verified_success(shown):
    t = shown("stub-1")
    for pn in (f"PN-0{i}" for i in range(1, 9)):
        assert f"**{pn}" in t
    assert t.count("PASSED") >= 8 and "NOT RECORDED" not in t
    assert "recorded" in t and "implied" in t.lower()  # recorded and implied-by-a-later-step are both labelled


def test_a_pending_request_shows_not_recorded_as_not_a_pass_and_nothing_authorised_or_attempted(shown):
    t = shown("stub-2")
    marked = {line for line in t.split("\n") if "NOT RECORDED (this is not a pass)" in line}  # each element is collected twice
    assert len(marked) == 6  # PN-01, 04, 05, 06, 07, 08
    assert "NOT authorised" in t and "Not attempted" in t
    assert "Executed, and the recorded verification" not in t and "Succeeded" not in t


def test_a_failed_verification_is_failed_not_passed_and_the_plan_is_only_proposed(shown):
    t = shown("stub-3")
    assert "FAILED" in t and "Compensation" in t and "PROPOSED" in t and "NOT executed" in t
    assert "Executed, and the recorded verification confirmed" not in t


def test_an_unknown_outcome_is_unknown_and_never_a_success(shown):
    t = shown("stub-4")
    assert "UNKNOWN" in t and "UNCERTAIN" in t.upper()
    assert "Executed, and the recorded verification confirmed" not in t


def test_an_overdue_pending_request_reads_as_expired_and_offers_no_decision(shown):
    t = shown("stub-5")
    assert "EXPIRED" in t and "Awaiting human approval" not in t
    assert "Approve" not in t and "Submit decision" not in t  # no decision control is offered for it


def test_a_pending_unexpired_request_still_reads_as_awaiting(shown):
    t = shown("stub-2")
    assert "Awaiting human approval. Nothing has been executed." in t
    assert "Submit decision" in t  # the control the expired case must NOT have (so that negative check is meaningful)


def test_an_empty_governance_response_shows_no_success_and_no_pass(shown):
    t = shown("stub-6")
    assert "PASSED" not in t and "Succeeded" not in t and "VERIFIED" not in t.replace("NOT verified", "")


def test_a_partial_governance_response_shows_only_what_was_reported(shown):
    t = shown("stub-7")
    assert "PendingReview" in t and "PASSED" not in t and "Succeeded" not in t


def test_a_blocked_duplicate_fails_pn07_and_is_not_shown_as_executed(shown):
    t = shown("stub-8")
    assert "FAILED" in t and "Executed, and the recorded verification confirmed" not in t
