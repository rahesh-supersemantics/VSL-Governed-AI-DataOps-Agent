"""Whole-call time budget, shared snapshot, server hard limit, partial completion, and OLIST-008 interpretations.

Time is simulated with ``FakeClock``: every statement advances it, and a statement slower than the driver timeout is cut off
at that timeout, like pyodbc. Nothing sleeps and no database or network is used.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest
from jsonschema import Draft202012Validator
from mcp import Client

from app.agent.config import AgentConfig
from app.agent.profiles import ECOMMERCE_PROFILE
from app.ecommerce import diagnostics as dx
from app.ecommerce.guard import CATALOG_SQL, IDENTITY_SQL, TimeBudgetExceeded
from app.ecommerce.service import EcommerceDiagnosticsService
from app.ecommerce.target import (
    HARD_LIMIT_MARGIN_SECONDS,
    MIN_AGENT_TOOL_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    TOOL_BUDGET_SECONDS,
)
from app.mcp_server.ecommerce.server import create_ecommerce_server
from tests.test_ecommerce_agent import offline
from tests.ecommerce_support import FakeClock, make_clean_db, make_db, sqlite_connect

# statements before the first diagnostic: identity + permission + one catalogue read
SETUP_STATEMENTS = 3


def timed_service(db=None, *, budget, cost=1.0, slow=None, record=None):
    clock = FakeClock()
    connect = sqlite_connect(db if db is not None else make_db(), clock=clock, cost=cost, slow=slow, record=record)
    return EcommerceDiagnosticsService(connect=connect, monotonic=clock, budget_seconds=budget), clock


def sweep(service):
    return service.run_all().to_dict()


def by_name(report):
    return {r["diagnostic_name"]: r for r in report["results"]}


# ------------------------------------------------------------------------------------------- the budget itself
def test_budget_constants_fit_inside_the_agent_timeout():
    default = AgentConfig().tool_timeout_seconds
    assert QUERY_TIMEOUT_SECONDS < TOOL_BUDGET_SECONDS < TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS < default
    assert MIN_AGENT_TOOL_TIMEOUT_SECONDS == ECOMMERCE_PROFILE.min_tool_timeout_seconds
    assert TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS <= MIN_AGENT_TOOL_TIMEOUT_SECONDS <= default


def test_a_fast_sweep_completes_inside_the_budget():
    service, clock = timed_service(budget=45, cost=0.01)
    report = sweep(service)
    summary = report["ecommerce_summary"]
    assert summary["execution"] == "complete" and summary["time_budget_exceeded"] is False
    assert summary["checks_not_completed"] == [] and report["overall_status"] == "issue_detected"
    assert clock.now - 1000.0 < 45


def test_an_exhausted_budget_gives_an_incomplete_sweep_never_a_clean_one():
    record = []
    service, _ = timed_service(budget=20, record=record)  # each statement "takes" 1 s
    report = sweep(service)
    summary, results = report["ecommerce_summary"], by_name(report)
    # setup 3 + target 9 + integrity 6 + completeness 2 = 20 statements fill the 20 s exactly
    assert [n for n in dx.CHECK_NAMES if results[n]["status"] != "error"] == list(dx.CHECK_NAMES[:3])
    for name in dx.CHECK_NAMES[3:]:
        assert results[name]["status"] == "error" and results[name]["error_code"] == "ECOM-TIMEOUT"
        assert results[name]["evidence"][0]["completed"] is False
    assert len(record[0].executed) == SETUP_STATEMENTS + 9 + 6 + 2  # nothing was sent after the deadline
    assert report["overall_status"] == "error" and summary["execution"] == "incomplete"
    assert summary["time_budget_exceeded"] is True and summary["checks_not_completed"] == list(dx.CHECK_NAMES[3:])
    assert summary["checks"]["unassessed"] == 4 and summary["checks"]["failed"] >= 1  # finished checks are kept


def test_budget_exhausted_during_setup_marks_the_whole_sweep_timed_out():
    service, _ = timed_service(budget=1)
    report = sweep(service)
    assert report["overall_status"] == "error" and report["ecommerce_summary"]["time_budget_exceeded"] is True
    assert report["ecommerce_summary"]["target_verified"] is False
    assert all(r["status"] == "error" for r in report["results"])


def test_a_single_tool_call_has_the_same_budget():
    service, _ = timed_service(budget=5)
    result = service.check_missing_values()  # dozens of statements
    assert result.status == "error" and result.error_code == "ECOM-TIMEOUT"
    assert "incomplete" in result.summary.lower()


def test_the_statement_timeout_shrinks_with_the_remaining_budget_and_never_grows():
    record = []
    service, _ = timed_service(budget=20, record=record)
    sweep(service)
    seen = record[0].timeouts_seen
    assert seen[0] == 20 < QUERY_TIMEOUT_SECONDS and max(seen) <= QUERY_TIMEOUT_SECONDS  # a 20 s budget caps even the first
    assert min(seen) == 1 and seen[-1] == 1                    # the last statements had 1 s left
    assert seen == sorted(seen, reverse=True)                  # monotonically non-increasing
    assert record[0].connection.driver_connection.timeout == 1


def test_a_budget_longer_than_the_statement_timeout_never_raises_the_statement_timeout():
    record = []
    service, _ = timed_service(budget=500, cost=0.01, record=record)
    sweep(service)
    assert set(record[0].timeouts_seen) == {QUERY_TIMEOUT_SECONDS}


def test_if_a_shorter_timeout_cannot_be_applied_the_statement_is_refused():
    from app.ecommerce.guard import GuardedConnection, GuardRefusal
    from tests.ecommerce_support import SqliteRaw

    clock = FakeClock()
    raw = SqliteRaw(make_db(), clock=clock)
    guarded = GuardedConnection(raw, deadline=clock.now + 10, monotonic=clock)
    clock.now += 5
    raw.connection.driver_connection = type("Stuck", (), {"timeout": 30, "__slots__": ()})()  # cannot be changed
    with pytest.raises(GuardRefusal) as caught:
        guarded.scalar("SELECT COUNT_BIG(*) FROM dbo.[ecom_orders]")
    assert caught.value.kind == "timeout_not_applied" and raw.executed == []


def test_no_statement_is_sent_once_the_deadline_has_passed():
    from app.ecommerce.guard import GuardedConnection
    from tests.ecommerce_support import SqliteRaw

    clock = FakeClock()
    raw = SqliteRaw(make_db(), clock=clock)
    guarded = GuardedConnection(raw, deadline=clock.now + 3, monotonic=clock)
    clock.now += 3
    with pytest.raises(TimeBudgetExceeded):
        guarded.scalar("SELECT COUNT_BIG(*) FROM dbo.[ecom_orders]")
    assert raw.executed == []


# --------------------------------------------------------------------------------- slow and blocked statements
def test_a_blocked_statement_is_cut_at_the_remaining_budget_and_the_sweep_is_incomplete():
    # only the reviews->orders orphan query is stuck (it is the sixth integrity statement)
    service, clock = timed_service(make_clean_db(), budget=20, cost=0.1, slow={"FROM dbo.[ecom_order_reviews] AS t": 100})
    report = sweep(service)
    results = by_name(report)
    integrity_rules = {e["rule_id"]: e for e in results["check_referential_integrity"]["evidence"] if e["type"] == "rule_result"}
    assert integrity_rules["ECOM-FK-REVIEWS-ORDERS"]["status"] == "error"          # the blocked statement
    assert integrity_rules["ECOM-FK-REVIEWS-ORDERS"]["violations"] is None         # unknown, not zero
    assert results["check_referential_integrity"]["status"] == "error"             # clean data elsewhere, yet not "ok"
    assert report["overall_status"] == "error" and report["ecommerce_summary"]["execution"] == "incomplete"
    assert clock.now - 1000.0 <= 20 + 1.0                                          # bounded by the budget (+ rounding up to whole seconds), not 100 s
    assert all(results[n]["error_code"] == "ECOM-TIMEOUT" for n in dx.CHECK_NAMES[2:])
    assert "SECRET-HOST" not in json.dumps(report)


def test_a_clean_database_with_one_failing_check_is_still_not_a_clean_sweep():
    service, _ = timed_service(make_clean_db(), budget=1000, cost=0.0, slow={"FROM dbo.[ecom_order_reviews] AS t": 100})
    report = sweep(service)
    assert by_name(report)["check_referential_integrity"]["status"] == "error"   # the stuck statement cut at 30 s
    assert by_name(report)["check_delivery_completeness"]["status"] == "ok"      # unaffected checks still finish
    assert report["overall_status"] == "error"
    # three rules query "ecom_order_reviews AS t": the orphan check, OLIST-010 and the order_id not-null check
    assert report["ecommerce_summary"]["rules"]["unassessed"] == 3
    assert report["ecommerce_summary"]["execution"] == "incomplete"


# ------------------------------------------------------------------------------------------- shared snapshot
def test_the_sweep_reads_the_rule_file_and_the_catalogue_once(monkeypatch):
    reads = []
    real = dx._read_config
    monkeypatch.setattr(dx, "_read_config", lambda path: reads.append(path) or real(path))
    record = []
    EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)).run_all()
    executed = record[0].executed
    assert len(reads) == 1 and executed.count(CATALOG_SQL) == 1 and executed.count(IDENTITY_SQL) == 1
    assert len(executed) == len(set(executed)), "an identical statement ran twice in one sweep"
    assert len(executed) < 64  # was 64 before the shared snapshot


def test_a_failing_snapshot_is_reported_by_every_check_without_retrying(monkeypatch):
    def broken(path):
        raise dx.DataQualityConfigError("bad")

    monkeypatch.setattr(dx, "_read_config", broken)
    record = []
    report = EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)).run_all().to_dict()
    assert {r["error_code"] for r in report["results"]} == {"ECOM-RULE-CONFIG"}
    assert report["overall_status"] == "error" and report["ecommerce_summary"]["execution"] == "incomplete"
    assert CATALOG_SQL not in record[0].executed


def test_individual_tools_keep_their_own_verified_connection_and_snapshot():
    record = []
    service = EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record))
    assert service.check_delivery_completeness().status == "issue_detected"
    assert service.check_delivery_timeline().status == "issue_detected"
    assert len(record) == 2 and all(r.executed.count(IDENTITY_SQL) == 1 and r.executed.count(CATALOG_SQL) == 1 for r in record)


# ----------------------------------------------------------------------------------------- server hard limit
def test_the_server_answers_with_an_explicit_timeout_when_a_worker_is_stuck():
    release = threading.Event()

    @contextmanager
    def stuck(deadline=None):
        release.wait(10)
        raise RuntimeError("released")
        yield  # pragma: no cover

    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=stuck, budget_seconds=0.2),
                                     hard_limit_seconds=0.3, safety_margin_seconds=0.1)

    async def go():
        async with Client(server) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            one = await client.call_tool("check_delivery_timeline", {})
            roll = await client.call_tool("run_ecommerce_diagnostics", {})
            release.set()
            return listed, one, roll

    try:
        listed, one, roll = asyncio.run(go())
    finally:
        release.set()
    assert one.is_error is True and one.structured_content["error_code"] == "ECOM-TIMEOUT"
    assert one.structured_content["status"] == "error"
    assert one.structured_content["evidence"][0]["worker_cancelled"] is False  # the worker is not claimed to be stopped
    Draft202012Validator(listed["check_delivery_timeline"].output_schema).validate(one.structured_content)
    assert roll.is_error is True and roll.structured_content["overall_status"] == "error"
    assert roll.structured_content["ecommerce_summary"]["execution"] == "incomplete"
    assert roll.structured_content["ecommerce_summary"]["time_budget_exceeded"] is True
    Draft202012Validator(listed["run_ecommerce_diagnostics"].output_schema).validate(roll.structured_content)


def test_a_service_timeout_comes_back_through_the_server_as_an_error_result():
    service, _ = timed_service(budget=20)
    server = create_ecommerce_server(service)

    async def go():
        async with Client(server) as client:
            return await client.call_tool("run_ecommerce_diagnostics", {})

    result = asyncio.run(go())
    assert result.is_error is True and result.structured_content["overall_status"] == "error"
    assert result.structured_content["ecommerce_summary"]["time_budget_exceeded"] is True


# -------------------------------------------------------------------------------------------------- the agent
def test_an_incomplete_sweep_never_lets_the_agent_report_no_issue():
    service, _ = timed_service(make_clean_db(), budget=20)  # clean data, but the sweep runs out of time
    report = offline("is the ecommerce database healthy?", create_ecommerce_server(service))
    assert report.status in {"inconclusive", "failed"}
    assert report.status != "no_issue_detected"
    assert any(e.status == "error" for e in report.evidence)


def test_a_short_agent_timeout_is_reported_as_a_limitation():
    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_db())))
    short = offline("check deliveries", server, tool_timeout_seconds=30)
    normal = offline("check deliveries", server)
    assert any("tool timeout" in text for text in short.limitations)
    assert not any("tool timeout" in text for text in normal.limitations)


# ----------------------------------------------------------------- OLIST-008: timestamp vs calendar-date meaning
def late_db():
    db = make_clean_db()
    db.execute("delete from ecom_orders")
    rows = [  # id, status, estimated, delivered
        ("ORD-SECRET-A", "delivered", "2020-01-10 00:00:00", "2020-01-10 14:00:00"),   # same day, after midnight
        ("ORD-SECRET-B", "delivered", "2020-01-10 00:00:00", "2020-01-11 08:00:00"),   # next day
        ("ORD-SECRET-C", "delivered", "2020-01-10 00:00:00", "2020-01-10 00:00:00"),   # exactly the estimate
        ("ORD-SECRET-D", "delivered", "2020-01-10 00:00:00", "2020-01-09 23:00:00"),   # early
        ("ORD-SECRET-E", "shipped", "2020-01-10 00:00:00", "2020-01-20 00:00:00"),     # not delivered: out of scope
    ]
    db.executemany("insert into ecom_orders(order_id, customer_id, order_status, order_purchase_timestamp, "
                   "order_estimated_delivery_date, order_delivered_customer_date) values(?,?,?,?,?,?)",
                   [(i, "CUST-SECRET-1", st, "2020-01-01 00:00:00", est, dlv) for i, st, est, dlv in rows])
    return db


def test_olist_008_compares_full_timestamps_not_calendar_dates():
    db = late_db()
    by_timestamp = db.execute("select count(*) from ecom_orders where order_status='delivered' "
                              "and not (order_delivered_customer_date <= order_estimated_delivery_date)").fetchone()[0]
    by_calendar_date = db.execute("select count(*) from ecom_orders where order_status='delivered' "
                                  "and not (date(order_delivered_customer_date) <= date(order_estimated_delivery_date))").fetchone()[0]
    assert (by_timestamp, by_calendar_date) == (2, 1)  # the two readings differ by the same-day, after-midnight delivery
    late = by_rule(svc_for(db).check_delivery_timeline())["OLIST-008"]
    assert late["violations"] == by_timestamp != by_calendar_date  # the current rule is the full-timestamp one
    assert late["classification"] == "business_signal"


def test_olist_008_is_a_business_signal_whichever_reading_is_chosen():
    result = svc_for(late_db()).check_delivery_timeline()
    assert result.status == "warning" and result.severity == "low"
    assert "business signal" in " ".join(result.notes).lower()


def svc_for(db):
    return EcommerceDiagnosticsService(connect=sqlite_connect(db))


def by_rule(result):
    return {e["rule_id"]: e for e in result.to_dict()["evidence"] if e["type"] == "rule_result"}
