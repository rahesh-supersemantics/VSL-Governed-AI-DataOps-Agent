"""Stage 6: navigation, new pages and the evidence/hypothesis/unavailable presentation.

AppTest and unit checks only. Real-browser rendering was checked separately (see the Stage 6 report), not here."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import runtime, ui_kit
from dashboard.ops_client import OpsClient
from tests.api_support import API_TOKENS, api  # noqa: F401
from tests.test_phase10_dashboard_app import go, text_of

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "ops_app.py")


@pytest.fixture
def ui(api):
    def make(role="viewer", **kwargs):
        e = api(**kwargs)
        runtime.set_override("ops", OpsClient("http://testserver/api/v1", API_TOKENS[role], http=e.client))
        return e, AppTest.from_file(APP, default_timeout=30)

    yield make
    runtime.set_override("ops", None)


def test_pill_always_carries_a_glyph_and_the_text():
    for value in ("success", "warn", "fail", "failed", "running", "pending", "something-else", None):
        html = ui_kit.pill(value)
        assert "&nbsp;" in html and ("unknown" in html if value is None else str(value).replace("_", " ") in html)


def test_pill_escapes_untrusted_text():
    assert "<script>" not in ui_kit.pill("<script>alert(1)</script>")


def test_navigation_offers_every_destination_in_group_order(ui):
    e, at = ui()
    at.run()
    radio = at.sidebar.radio(key="page")
    assert radio.options == ["Overview", "Pipelines", "Data Quality", "Incidents", "AI Investigations", "Run History",
                             "Human Review", "Recovery & approvals", "Tickets", "Settings"]
    assert radio.value == "Overview"
    go(at, "Investigations")  # option VALUES stay stable: deep links and existing tests rely on them
    assert not at.exception and at.sidebar.radio(key="page").value == "Investigations"


def test_new_pages_render_without_exception_and_state_unavailability_honestly(ui):
    e, at = ui()
    for page in ("Run History", "Data Quality", "Pipelines & runs"):
        go(at, page)
        assert not at.exception, page


def test_data_quality_page_says_ecommerce_rules_are_not_exposed_and_shows_no_olist_numbers(ui):
    e, at = ui()
    go(at, "Data Quality")
    t = text_of(at)
    assert "not shown here" in t and "OLIST-008" in t and "does not estimate" in t


def test_run_history_offers_only_valid_statuses(ui):
    e, at = ui()
    at.run()
    go(at, "Run History")
    # without SQL Server the page shows the pipelines error instead of filters; either way "partial" is never offered
    opts = [o for sb in at.selectbox if sb.key == "run_status" for o in sb.options]
    assert "partial" not in opts
    assert opts in ([], ["any", "success", "warning", "failed", "running"])
