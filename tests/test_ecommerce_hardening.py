"""Stage 4.4 hardening: login-timeout coupling (A1), connection cleanup (A2), worker lifecycle and concurrency (A3), and the
interaction of every timeout layer with the agent (A4).

Deterministic and local: fakes and events only. These tests prove the code's own behaviour; they do NOT prove that pyodbc
honours its login or statement timeouts (that needs the live validation in docs/ecommerce-agent.md).
"""

from __future__ import annotations

import ast
import asyncio
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp import Client

from app.agent.config import AgentConfig
from app.agent.profiles import ECOMMERCE_PROFILE
from app.agent.report import claims_clean
from app.ecommerce import diagnostics as dx
from app.ecommerce.guard import (
    IDENTITY_SQL,
    GuardRefusal,
    TimeBudgetExceeded,
    verified_ecommerce_connection,
)
from app.ecommerce.service import CLEANUP_NOTE, EcommerceDiagnosticsService
from app.ecommerce.target import (
    HARD_LIMIT_MARGIN_SECONDS,
    MAX_IN_FLIGHT_WORKERS,
    MAX_LOGIN_TIMEOUT_SECONDS,
    MIN_AGENT_TOOL_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
    TOOL_BUDGET_SECONDS,
    validate_login_timeout,
)
from app.mcp_server.ecommerce.server import WorkerGate, create_ecommerce_server
from tests.agent_support import FakeToolbox, ScriptedModel, draft, plan, result_dict, tc
from tests.ecommerce_support import FakeClock, FakeEngine, FixtureSettings, SqliteRaw, make_clean_db, make_db, sqlite_connect
from tests.test_ecommerce_agent import offline
from tests.test_ecommerce_incomplete_and_workers import CLEAN_CLAIMS, ECOM_TOOLS, misleading, run_ecom_graph

ROOT = Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs" / "ecommerce-agent.md").read_text(encoding="utf-8")


class Settings(FixtureSettings):
    def __init__(self, login=5):
        self.db_login_timeout_seconds = login


def open_connection(settings=None, *, raw=None, engine=None, factory=None, **kwargs):
    raw = raw or SqliteRaw(make_db())
    engine = engine or FakeEngine(raw)
    built = []

    def default_factory(settings_, **kw):
        built.append(kw)
        return engine

    manager = verified_ecommerce_connection(settings or Settings(), engine_factory=factory or default_factory, **kwargs)
    return raw, engine, built, manager


# ===================================================================================== A1: login timeout vs deadline
@pytest.mark.parametrize("login,budget,ok", [
    (5, 45, True), (1, 45, True), (15, 45, True), (15.0, 45, True), (15.01, 45, False), (16, 45, False), (60, 45, False),
    (10, 30, True), (11, 30, False), (0.99, 45, False), (0, 45, False), (-1, 45, False),
    (math.nan, 45, False), (math.inf, 45, False), (-math.inf, 45, False), (True, 45, False), ("5", 45, False), (None, 45, False),
    (5, 3, False),   # a third of a 3 s budget is 1 s: a 5 s login cannot fit
    (1, 3, True),
])
def test_login_timeout_boundaries(login, budget, ok):
    if ok:
        validate_login_timeout(login, budget)
    else:
        with pytest.raises(ValueError):
            validate_login_timeout(login, budget)


def test_defaults_are_consistent_and_documented():
    assert Settings().db_login_timeout_seconds == 5 <= MAX_LOGIN_TIMEOUT_SECONDS == TOOL_BUDGET_SECONDS / 3
    assert (QUERY_TIMEOUT_SECONDS, TOOL_BUDGET_SECONDS, TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS) == (30, 45, 51)
    assert "MAX_LOGIN_TIMEOUT_SECONDS" in DOCS and "DB_LOGIN_TIMEOUT_SECONDS" in DOCS


def test_the_default_and_a_valid_custom_login_timeout_connect():
    for login in (5, 12, 15):
        clock = FakeClock()
        raw, engine, built, manager = open_connection(Settings(login), deadline=clock() + TOOL_BUDGET_SECONDS, monotonic=clock)
        with manager as conn:
            assert conn.verification["verified"] is True
        assert built == [{"database": "dataops_ecommerce_demo", "admin": False}] and raw.closed and engine.disposed


@pytest.mark.parametrize("login", [16, 30, 60, 0, -3, math.nan, math.inf, "5", True, None, 5.5 * 10])
def test_a_dangerous_or_invalid_login_timeout_is_refused_before_any_engine_exists(login):
    clock = FakeClock()
    raw, engine, built, manager = open_connection(Settings(login), deadline=clock() + TOOL_BUDGET_SECONDS, monotonic=clock)
    with pytest.raises(GuardRefusal) as caught:
        with manager:
            raise AssertionError("must not yield")
    assert caught.value.kind == "login_timeout_invalid" and "no connection was attempted" in str(caught.value)
    assert built == [] and raw.executed == [] and not raw.closed


def test_a_missing_login_timeout_setting_is_refused_not_defaulted():
    del_attr = type("S", (), {"db_auth_mode": "windows", "db_server": "localhost\\SQLEXPRESS03", "db_name": "dataops_sim"})()
    raw, engine, built, manager = open_connection(del_attr)
    with pytest.raises(GuardRefusal) as caught:
        with manager:
            pass
    assert caught.value.kind == "login_timeout_invalid" and built == []


def test_the_login_timeout_limit_shrinks_with_the_time_actually_left():
    clock = FakeClock()
    raw, engine, built, manager = open_connection(Settings(12), deadline=clock() + 30, monotonic=clock)  # 30 s left -> max 10 s
    with pytest.raises(GuardRefusal) as caught:
        with manager:
            pass
    assert caught.value.kind == "login_timeout_invalid" and built == []
    raw, engine, built, manager = open_connection(Settings(10), deadline=clock() + 30, monotonic=clock)
    with manager as conn:
        assert conn.verification["verified"] is True


def test_a_login_that_fails_after_using_the_whole_budget_is_a_timeout_and_runs_no_sql():
    clock = FakeClock()
    raw = SqliteRaw(make_db())
    engine = FakeEngine(raw, connect_error=RuntimeError("Login timeout expired Server=SECRET-HOST"))

    def slow_login(settings, **kw):
        clock.now += 45  # the login consumed the entire budget, then failed
        return engine

    with pytest.raises(TimeBudgetExceeded):
        with verified_ecommerce_connection(Settings(5), engine_factory=slow_login, deadline=clock() + 45, monotonic=clock):
            pass
    assert raw.executed == []


def test_a_login_that_fails_within_the_budget_is_a_plain_connection_refusal_without_driver_text():
    clock = FakeClock()
    engine = FakeEngine(SqliteRaw(make_db()), connect_error=RuntimeError("Login failed Server=SECRET-HOST"))
    with pytest.raises(GuardRefusal) as caught:
        with verified_ecommerce_connection(Settings(5), engine_factory=lambda s, **k: engine, deadline=clock() + 45, monotonic=clock):
            pass
    assert caught.value.kind == "connection_failed" and "SECRET" not in str(caught.value)


def test_a_slow_but_successful_login_leaves_no_time_so_no_diagnostic_sql_starts():
    clock = FakeClock()
    raw = SqliteRaw(make_db(), clock=clock)
    engine = FakeEngine(raw)

    def slow_login(settings, **kw):
        clock.now += 46  # login "succeeds" but the deadline (45 s) is already gone
        return engine

    with pytest.raises(TimeBudgetExceeded):
        with verified_ecommerce_connection(Settings(5), engine_factory=slow_login, deadline=1000.0 + 45, monotonic=clock):
            raise AssertionError("must not yield")
    assert raw.executed == [] and raw.closed and engine.disposed  # not even the identity query was sent


def test_through_the_service_a_login_that_eats_the_budget_is_ecom_timeout_and_nothing_ran():
    clock = FakeClock()
    raw = SqliteRaw(make_db(), clock=clock)

    def slow_login(settings, **kw):
        clock.now += 45
        return FakeEngine(raw)

    def connect(deadline=None):
        return verified_ecommerce_connection(Settings(5), engine_factory=slow_login, deadline=deadline, monotonic=clock)

    service = EcommerceDiagnosticsService(connect=connect, monotonic=clock, budget_seconds=45)
    result = service.check_delivery_timeline()
    sweep = service.run_all().to_dict()
    assert result.status == "error" and result.error_code == dx.ECOM_TIMEOUT
    assert sweep["ecommerce_summary"]["execution"] == "incomplete" and sweep["ecommerce_summary"]["time_budget_exceeded"] is True
    assert raw.executed == []


# ========================================================================================== A2: cleanup and exceptions
def test_cleanup_runs_on_success_refusal_body_error_and_cancellation():
    for body in (None, RuntimeError("body"), KeyboardInterrupt(), asyncio.CancelledError()):
        raw, engine, built, manager = open_connection()
        try:
            with manager:
                if body is not None:
                    raise body
        except BaseException as exc:  # noqa: BLE001
            assert exc is body
        assert raw.closed and engine.disposed, body

    raw = SqliteRaw(make_db(), permissions=None)  # verification fails -> refusal, still cleaned
    raw, engine, built, manager = open_connection(raw=raw)
    with pytest.raises(GuardRefusal):
        with manager:
            pass
    assert raw.closed and engine.disposed


def test_a_cleanup_failure_is_recorded_and_both_resources_are_still_attempted():
    class BadClose(SqliteRaw):
        def close(self):
            super().close()
            raise RuntimeError("close failed Server=SECRET-HOST")

    class BadDispose(FakeEngine):
        def dispose(self):
            super().dispose()
            raise OSError("dispose failed PWD=hunter2")

    raw = BadClose(make_db())
    engine = BadDispose(raw)
    _, _, _, manager = open_connection(raw=raw, engine=engine)
    with manager as conn:
        pass
    assert conn.cleanup_errors == ["close:RuntimeError", "dispose:OSError"]
    assert raw.closed and engine.disposed  # the failing close did not stop the dispose
    assert "SECRET" not in repr(conn.cleanup_errors) and "hunter2" not in repr(conn.cleanup_errors)


def test_the_service_does_not_present_a_cleanup_failure_as_a_fully_clean_run():
    class BadClose(SqliteRaw):
        def close(self):
            super().close()
            raise RuntimeError("close failed Server=SECRET-HOST")

    def connect(deadline=None):
        raw = BadClose(make_clean_db())
        _, _, _, manager = open_connection(raw=raw, deadline=deadline)
        return manager

    service = EcommerceDiagnosticsService(connect=connect)
    result = service.check_delivery_timeline()
    sweep = service.run_all().to_dict()
    assert result.status == "ok"  # the result was computed before cleanup, so its status is unchanged...
    assert any(n.startswith(CLEANUP_NOTE) and "close:RuntimeError" in n for n in result.notes)  # ...but the problem is visible
    assert sweep["ecommerce_summary"]["connection_cleanup_warning"] is True
    assert "SECRET" not in str(sweep)


def test_a_clean_cleanup_adds_no_warning():
    service = EcommerceDiagnosticsService(connect=sqlite_connect(make_clean_db()))
    assert service.run_all().to_dict()["ecommerce_summary"]["connection_cleanup_warning"] is False


def test_one_connection_serves_identity_target_permission_and_all_diagnostics():
    record = []
    EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)).run_all()
    assert len(record) == 1
    assert record[0].executed[0] == IDENTITY_SQL and record[0].executed[1].lstrip().startswith("SELECT")
    assert record[0].executed.count(IDENTITY_SQL) == 1 and record[0].closed


def test_no_retry_through_another_identity_or_database():
    calls = []

    def factory(settings, **kw):
        calls.append(kw)
        return FakeEngine(SqliteRaw(make_db()), connect_error=RuntimeError("boom"))

    with pytest.raises(GuardRefusal):
        with verified_ecommerce_connection(Settings(), engine_factory=factory):
            pass
    assert calls == [{"database": "dataops_ecommerce_demo", "admin": False}]  # one attempt, fixed target, not admin

    calls.clear()
    raw = SqliteRaw(make_db(), identity={"database_name": "dataops_sim", "effective_login": "X\\dq_reader",
                                         "original_login": "X\\dq_reader", "machine_name": "X"})
    with pytest.raises(GuardRefusal):
        with verified_ecommerce_connection(Settings(), engine_factory=lambda s, **k: calls.append(k) or FakeEngine(raw)):
            pass
    assert len(calls) == 1 and raw.closed


def test_a_failed_mandatory_verification_never_yields_a_successful_diagnostic():
    for override in ({"permissions": None}, {"identity": None}, {"permissions": {"verified": False}}):
        service = EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), **override))
        result = service.check_delivery_completeness()
        assert result.status == "error" and result.error_code == dx.ECOM_GUARD_REFUSED, override
        assert service.run_all().to_dict()["ecommerce_summary"]["execution"] == "incomplete"


# ============================================================================== A3: worker lifecycle and concurrency
def test_the_gate_never_exceeds_capacity_under_concurrent_acquires():
    gate = WorkerGate(4)
    start, wins, lock = threading.Barrier(40), [], threading.Lock()

    def grab():
        start.wait()
        if gate.try_acquire():
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=grab) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 4 == gate.in_flight


def blocking_connect(blockers, running, peak):
    """connect() whose first workers block until released; tracks how many are inside connect at once."""
    lock, starts = threading.Lock(), []

    @contextmanager
    def connect(deadline=None):
        with lock:
            n = len(starts)
            starts.append(n)
            running.append(n)
            peak.append(len(running))
        try:
            if n < len(blockers):
                blockers[n].wait(10)
                raise RuntimeError("released late")
            with sqlite_connect(make_clean_db())(deadline=deadline) as conn:
                yield conn
        finally:
            with lock:
                running.remove(n)

    return connect, starts


def test_ten_concurrent_calls_start_at_most_four_workers_and_refuse_the_rest_without_connecting():
    blockers = [threading.Event() for _ in range(10)]
    running, peak = [], []
    connect, starts = blocking_connect(blockers, running, peak)
    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=connect, budget_seconds=0.2),
                                     hard_limit_seconds=0.4, safety_margin_seconds=0.1, max_in_flight=4)
    gate = server.worker_gate

    async def go():
        async with Client(server) as client:
            results = await asyncio.gather(*(client.call_tool("check_delivery_timeline", {}) for _ in range(10)))
            held = gate.in_flight
            for b in blockers:
                b.set()
            await wait_zero(gate)
            return results, held

    try:
        results, held = asyncio.run(go())
    finally:
        for b in blockers:
            b.set()
    codes = sorted(r.structured_content["error_code"] for r in results)
    assert codes == ["ECOM-BUSY"] * 6 + ["ECOM-TIMEOUT"] * 4
    assert len(starts) == 4 and max(peak) <= 4  # a refused call started no worker and opened no connection
    assert held == 4 and gate.in_flight == 0


async def wait_zero(gate, seconds=5.0):
    end = time.monotonic() + seconds
    while gate.in_flight and time.monotonic() < end:
        await asyncio.sleep(0.01)


def test_a_late_result_cannot_change_the_result_already_returned():
    release, finished = threading.Event(), threading.Event()

    @contextmanager
    def connect(deadline=None):
        release.wait(10)
        try:
            with sqlite_connect(make_clean_db())(deadline=None) as conn:  # the worker eventually produces a CLEAN result
                yield conn
        finally:
            finished.set()

    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=connect, budget_seconds=0.2),
                                     hard_limit_seconds=0.3, safety_margin_seconds=0.1, max_in_flight=1)
    seen = {}

    async def go():
        async with Client(server) as client:
            seen["first"] = await client.call_tool("check_delivery_timeline", {})
            before = str(seen["first"].structured_content)
            release.set()
            assert await asyncio.get_running_loop().run_in_executor(None, finished.wait, 5)
            await wait_zero(server.worker_gate)
            seen["unchanged"] = before == str(seen["first"].structured_content)

    try:
        asyncio.run(go())
    finally:
        release.set()
    assert seen["first"].structured_content["error_code"] == "ECOM-TIMEOUT" and seen["first"].is_error is True
    assert seen["unchanged"] is True and server.worker_gate.in_flight == 0


def test_a_worker_that_ends_after_the_event_loop_closed_does_not_raise(monkeypatch):
    raised = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args))
    release = threading.Event()
    gate = WorkerGate(1)

    async def go():
        from app.mcp_server.ecommerce.server import run_in_worker

        import anyio

        assert gate.try_acquire()
        with pytest.raises(TimeoutError):
            with anyio.fail_after(0.1):
                await run_in_worker(lambda: release.wait(10), gate)

    asyncio.run(go())  # the loop is closed when this returns; the worker is still blocked
    release.set()
    end = time.monotonic() + 5
    while gate.in_flight and time.monotonic() < end:
        time.sleep(0.01)
    assert gate.in_flight == 0 and raised == []


def test_every_tool_goes_through_the_one_gated_run_helper():
    tree = ast.parse((ROOT / "app" / "mcp_server" / "ecommerce" / "server.py").read_text(encoding="utf-8"))
    factory = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_ecommerce_server")
    tools = [n for n in ast.walk(factory) if isinstance(n, ast.AsyncFunctionDef) and n.name != "run"]
    assert len(tools) == 8
    for fn in tools:
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "run" in names and "service" not in names and "to_thread" not in names, fn.name
    source = (ROOT / "app" / "mcp_server" / "ecommerce" / "server.py").read_text(encoding="utf-8")
    assert source.count("threading.Thread(") == 1 and "to_thread" not in source  # one thread factory, gated by WorkerGate


# ================================================================================ A4: every timeout layer and the agent
def test_the_documented_timeout_table_matches_the_enforced_values():
    from app.agent.mcp_client import Toolbox

    assert AgentConfig().tool_timeout_seconds == 60.0
    assert Toolbox.__init__.__defaults__ is not None
    values = {"QUERY_TIMEOUT_SECONDS": QUERY_TIMEOUT_SECONDS, "TOOL_BUDGET_SECONDS": TOOL_BUDGET_SECONDS,
              "HARD_LIMIT_MARGIN_SECONDS": HARD_LIMIT_MARGIN_SECONDS, "MAX_LOGIN_TIMEOUT_SECONDS": MAX_LOGIN_TIMEOUT_SECONDS,
              "MIN_AGENT_TOOL_TIMEOUT_SECONDS": MIN_AGENT_TOOL_TIMEOUT_SECONDS, "MAX_IN_FLIGHT_WORKERS": MAX_IN_FLIGHT_WORKERS}
    for name, value in values.items():
        assert f"`{name}`" in DOCS and f"| {value} " in DOCS.split("### Timeout table")[1].split("###")[0], name
    assert ECOMMERCE_PROFILE.min_tool_timeout_seconds == MIN_AGENT_TOOL_TIMEOUT_SECONDS
    # strict ordering of the layers
    assert 5 <= MAX_LOGIN_TIMEOUT_SECONDS < QUERY_TIMEOUT_SECONDS < TOOL_BUDGET_SECONDS
    assert TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS <= MIN_AGENT_TOOL_TIMEOUT_SECONDS <= AgentConfig().tool_timeout_seconds


@pytest.mark.parametrize("code,status,severity", [
    ("ECOM-TIMEOUT", "error", "unknown"), ("ECOM-BUSY", "error", "unknown"), ("ECOM-GUARD-REFUSED", "error", "unknown"),
    ("ECOM-DB-ERROR", "error", "unknown"), ("ECOM-SCHEMA-MISMATCH", "error", "unknown"), ("ECOM-RULE-CONFIG", "error", "unknown"),
    ("ECOM-PARTIAL", "warning", "low"),
])
def test_the_agent_never_reads_an_incomplete_code_as_clean(code, status, severity):
    results = {t: result_dict(t, "ok", "info") for t in ECOMMERCE_PROFILE.core_diagnostics if t != "check_delivery_timeline"}
    results["check_delivery_timeline"] = result_dict("check_delivery_timeline", status, severity, error_code=code)
    toolbox = FakeToolbox({"run_ecommerce_diagnostics": {"overall_status": status, "results": list(results.values())}}, names=ECOM_TOOLS)
    report = run_ecom_graph(ScriptedModel([plan(tc("run_ecommerce_diagnostics"))], misleading()), toolbox)
    assert report.status in {"inconclusive", "failed"}
    assert report.summary.startswith(("INCONCLUSIVE", "FAILED")) and not claims_clean(report.summary)
    assert "Everything is healthy" not in report.model_dump_json()


def test_the_codes_stay_distinguishable_in_the_evidence():
    seen = {}
    for code, text in (("ECOM-TIMEOUT", "time"), ("ECOM-BUSY", "worker"), ("ECOM-PARTIAL", "not assessed")):
        toolbox = FakeToolbox({"check_delivery_timeline": result_dict("check_delivery_timeline", "error" if code != "ECOM-PARTIAL" else "warning",
                                                                       "low", summary=text, error_code=code)}, names=ECOM_TOOLS)
        report = run_ecom_graph(ScriptedModel([plan(tc("check_delivery_timeline"))], draft()), toolbox)
        seen[code] = [e.error_code for e in report.evidence]
    assert seen == {"ECOM-TIMEOUT": ["ECOM-TIMEOUT"], "ECOM-BUSY": ["ECOM-BUSY"], "ECOM-PARTIAL": ["ECOM-PARTIAL"]}


def test_an_agent_timeout_shorter_than_the_server_is_a_failed_call_never_a_clean_one():
    release = threading.Event()

    @contextmanager
    def stuck(deadline=None):
        release.wait(10)
        raise RuntimeError("released")
        yield  # pragma: no cover

    server = create_ecommerce_server(EcommerceDiagnosticsService(connect=stuck))  # real defaults: 45 s budget, 51 s hard limit
    try:
        report = offline("check deliveries", server, tool_timeout_seconds=1)
    finally:
        release.set()
    assert report.status in {"failed", "inconclusive"} and report.status not in {"no_issue_detected", "warnings_only"}
    assert any(r.outcome == "timeout" for r in report.tools_executed)
    assert any("tool timeout" in n for n in report.limitations)


def test_the_simulation_profile_defaults_are_untouched_by_ecommerce_settings():
    import inspect

    from app.agent.runner import investigate
    from app.agent.report import finalize_report

    assert inspect.signature(investigate).parameters["profile"].default is None
    assert inspect.signature(finalize_report).parameters["incomplete_codes"].default is None
    assert inspect.signature(finalize_report).parameters["core_diagnostics"].default is None
