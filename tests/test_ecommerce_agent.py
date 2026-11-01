"""The Ecommerce agent profile: allow-list, offline investigation through the MCP tools, honest conclusions, no network."""

from __future__ import annotations

import ast
import asyncio
import json
import socket
from pathlib import Path

import pytest

from app.agent.config import AgentConfig
from app.agent.mcp_client import APPROVED_TOOLS, Toolbox
from app.agent.offline import OfflineHeuristicModel
from app.agent.profiles import ECOMMERCE_PROFILE
from app.agent.runner import investigate
from app.ecommerce.service import EcommerceDiagnosticsService
from app.ecommerce.target import QUERY_TIMEOUT_SECONDS
from app.ecommerce.verification import target_confirmation
from app.mcp_server.ecommerce.server import TOOL_CATALOGUE, create_ecommerce_server
from tests import offline_network_guard
from tests.agent_support import server_for
from tests.ecommerce_support import SECRET_VALUES, make_clean_db, make_db, permission_row, sqlite_connect

ROOT = Path(__file__).resolve().parents[1]


def ecommerce_server(db=None, **kw):
    return create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(db or make_db(), **kw)))


def offline(incident, server, **config):
    async def go():
        return await investigate(incident, config=AgentConfig(**config), llm=OfflineHeuristicModel(ECOMMERCE_PROFILE),
                                 mcp_target=server, profile=ECOMMERCE_PROFILE)

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Block real outbound connections but allow the local socket-pair every asyncio event loop needs (see the guard)."""
    offline_network_guard.install(monkeypatch)


def test_profile_tools_equal_the_server_catalogue_and_are_disjoint_from_the_simulation():
    assert set(ECOMMERCE_PROFILE.approved_tools) == set(TOOL_CATALOGUE)
    assert not set(ECOMMERCE_PROFILE.approved_tools) & set(APPROVED_TOOLS)
    assert ECOMMERCE_PROFILE.report_tools <= ECOMMERCE_PROFILE.approved_tools
    assert ECOMMERCE_PROFILE.core_diagnostics <= ECOMMERCE_PROFILE.approved_tools
    assert ECOMMERCE_PROFILE.claims_root_cause is False and ECOMMERCE_PROFILE.follows_run_references is False


def test_the_query_timeout_fits_inside_the_agent_tool_timeout():
    assert QUERY_TIMEOUT_SECONDS == 30 < AgentConfig().tool_timeout_seconds


def test_toolbox_with_the_profile_accepts_only_the_ecommerce_tools():
    async def go():
        async with Toolbox(ecommerce_server(), approved_tools=ECOMMERCE_PROFILE.approved_tools,
                           report_tools=ECOMMERCE_PROFILE.report_tools) as tb:
            return sorted(tb.tools), tb.missing_tools, tb.ignored_tools

    names, missing, ignored = asyncio.run(go())
    assert set(names) == set(TOOL_CATALOGUE) and not missing and not ignored


def test_the_default_agent_ignores_the_ecommerce_tools_and_the_profile_ignores_simulation_tools(service):
    async def default_agent():
        async with Toolbox(ecommerce_server()) as tb:
            return set(tb.tools), set(tb.ignored_tools)

    tools, ignored = asyncio.run(default_agent())
    assert tools == set() and ignored == set(TOOL_CATALOGUE)

    async def profile_agent():
        async with Toolbox(server_for(service), approved_tools=ECOMMERCE_PROFILE.approved_tools,
                           report_tools=ECOMMERCE_PROFILE.report_tools) as tb:
            return set(tb.tools)

    assert asyncio.run(profile_agent()) == set()


def test_offline_investigation_uses_the_tools_and_reports_facts_not_a_root_cause():
    report = offline("check the ecommerce database for data quality problems", ecommerce_server())
    called = [t.tool for t in report.tools_executed]
    assert called[0] == "check_ecommerce_target" and "run_ecommerce_diagnostics" in called
    assert set(called) <= set(TOOL_CATALOGUE) and report.mode == "offline"
    assert report.status == "issue_identified"  # the dirty fixture has integrity violations
    assert report.likely_root_cause.status == "undetermined" and report.likely_root_cause.statement is None
    ok, _reason = target_confirmation(json.loads(report.model_dump_json()))
    assert ok is True
    blob = report.model_dump_json()
    assert not any(secret in blob for secret in SECRET_VALUES + ["dq_reader", "FIXTUREPC"])


def test_offline_hypotheses_use_the_ecommerce_prefix_and_never_claim_causes():
    report = offline("late deliveries and orphan payments", ecommerce_server())
    assert report.hypotheses  # the dirty fixture produces findings to hypothesise about
    for h in report.hypotheses:
        assert h.statement.startswith(ECOMMERCE_PROFILE.hypothesis_prefix)
    text = report.model_dump_json().lower()
    assert "caused by" not in text and "the root cause is" not in text and "likely root cause is" not in text


def test_a_refused_guard_stops_the_investigation_and_never_reports_a_clean_database():
    report = offline("is the database healthy?", ecommerce_server(permissions=permission_row(can_delete=1)))
    assert report.status in {"failed", "inconclusive"}
    ok, reason = target_confirmation(json.loads(report.model_dump_json()))
    assert ok is False and reason
    assert len(report.tools_executed) <= 2  # infrastructure failure stops the loop instead of retrying every tool


def test_no_issue_is_only_possible_when_every_core_check_ran():
    from tests.agent_support import FINISH, ScriptedModel, draft, plan, tc

    def scripted(*plans):
        llm = ScriptedModel(list(plans) + [FINISH], report=draft())
        return asyncio.run(investigate("is it clean?", config=AgentConfig(), llm=llm, mcp_target=ecommerce_server(make_clean_db()),
                                       profile=ECOMMERCE_PROFILE))

    only_target = scripted(plan(tc("check_ecommerce_target")))
    assert only_target.status == "inconclusive"  # one passing check is not a clean bill of health
    swept = scripted(plan(tc("run_ecommerce_diagnostics")))
    assert swept.status == "no_issue_detected"
    assert {e.tool for e in swept.evidence} >= ECOMMERCE_PROFILE.core_diagnostics


def test_agent_package_stays_free_of_database_and_ecommerce_imports():
    banned = ("app.ecommerce", "app.db", "app.diagnostics", "sqlalchemy", "pyodbc")
    for path in (ROOT / "app" / "agent").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(m.startswith(banned) for m in mods), (path.name, mods)


# ---- the offline network guard itself (Windows asyncio regression) ------------------------------------------------------
def windows_style_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """The Python-level fallback Windows uses for ``socket.socketpair``: listen on loopback and connect to ourselves."""
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen()
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(family, type, proto)
        try:
            csock.setblocking(False)
            try:
                csock.connect((addr, port))
            except (BlockingIOError, InterruptedError):
                pass
            csock.setblocking(True)
            ssock, _ = lsock.accept()
        except BaseException:
            csock.close()
            raise
    finally:
        lsock.close()
    return ssock, csock


def test_the_old_refuse_everything_guard_reproduces_the_windows_failure(monkeypatch):
    """Documents the root cause: a blanket connect() refusal breaks the loopback socket pair behind every event loop."""
    monkeypatch.setattr(socket, "socketpair", windows_style_socketpair)

    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(AssertionError, match="network access attempted"):
        socket.socketpair()  # what every event loop does while it is created (checked in the next test)


def test_the_guard_allows_the_windows_style_local_socket_pair_so_asyncio_can_start(monkeypatch):
    monkeypatch.setattr(socket, "socketpair", windows_style_socketpair)
    offline_network_guard.install(monkeypatch)  # wraps the Windows-style pair, as on a Windows machine

    calls = []
    guarded_pair = socket.socketpair
    monkeypatch.setattr(socket, "socketpair", lambda *a, **k: calls.append(1) or guarded_pair(*a, **k))

    async def uses_the_loop():
        await asyncio.sleep(0)
        return "ran"

    assert asyncio.run(uses_the_loop()) == "ran"
    assert calls  # proof that creating the event loop really went through the guarded socket pair
    a, b = socket.socketpair()
    try:
        a.sendall(b"x")
        assert b.recv(1) == b"x"
    finally:
        a.close()
        b.close()


def test_the_guard_still_blocks_real_outbound_connections():
    # 192.0.2.0/24 and 198.51.100.0/24 are reserved documentation ranges: a connect would never be legitimate.
    for target in (("192.0.2.1", 80), ("198.51.100.7", 443)):
        s = socket.socket()
        try:
            with pytest.raises(AssertionError, match="network access attempted"):
                s.connect(target)
            with pytest.raises(AssertionError, match="network access attempted"):
                s.connect_ex(target)
        finally:
            s.close()


def test_the_guard_blocks_loopback_connections_made_outside_a_socket_pair():
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen()
        client = socket.socket()
        try:
            with pytest.raises(AssertionError, match="network access attempted"):
                client.connect(server.getsockname())  # a local database or service is not allowed either
        finally:
            client.close()
    finally:
        server.close()


def test_the_guard_does_not_trust_host_names_or_leak_the_exemption_across_calls(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, "socketpair", lambda *a, **k: seen.append(offline_network_guard._state.in_socketpair) or (None, None))
    offline_network_guard.install(monkeypatch)
    socket.socketpair()
    assert seen == [True] and not getattr(offline_network_guard._state, "in_socketpair", False)  # reset afterwards
    s = socket.socket()
    try:
        with pytest.raises(AssertionError):
            s.connect(("localhost", 80))  # a name would need DNS
    finally:
        s.close()


def test_the_guard_is_restored_when_its_monkeypatch_context_ends():
    before = (socket.socket.connect, socket.socket.connect_ex, socket.socketpair)
    with pytest.MonkeyPatch.context() as mp:
        offline_network_guard.install(mp)
        assert socket.socket.connect is not before[0] and socket.socketpair is not before[2]
    assert (socket.socket.connect, socket.socket.connect_ex, socket.socketpair) == before
