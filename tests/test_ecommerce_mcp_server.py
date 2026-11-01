"""The Ecommerce MCP server: catalogue, schemas, read-only annotations, argument strictness, structured output, source rules."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters

from app.ecommerce import diagnostics as dx
from app.ecommerce.service import EcommerceDiagnosticsService
from app.ecommerce.target import ECOMMERCE_DATABASE, ECOMMERCE_TABLES, SERVER_NAME
from app.mcp_server import TOOL_CATALOGUE as SIM_CATALOGUE
from app.mcp_server.ecommerce.server import TOOL_CATALOGUE, create_ecommerce_server
from tests.ecommerce_support import SECRET_VALUES, make_db, permission_row, sqlite_connect

ROOT = Path(__file__).resolve().parents[1]
EIGHT = {"check_ecommerce_target", "check_referential_integrity", "check_delivery_completeness", "check_delivery_timeline",
         "check_missing_values", "check_duplicate_keys", "profile_ecommerce_timestamps", "run_ecommerce_diagnostics"}


def run(coro):
    return asyncio.run(coro)


def server(**kw):
    return create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), **kw)))


def tools(srv):
    async def go():
        async with Client(srv) as client:
            return (await client.list_tools()).tools

    return {t.name: t for t in run(go())}


def call(srv, name, arguments=None):
    async def go():
        async with Client(srv) as client:
            return await client.call_tool(name, arguments or {})

    return run(go())


def test_exactly_the_eight_ecommerce_tools_are_registered():
    listed = tools(server())
    assert set(listed) == EIGHT == set(TOOL_CATALOGUE) and len(listed) == 8
    assert not (set(listed) & set(SIM_CATALOGUE))  # no overlap with the simulation server
    assert SERVER_NAME != "dataops-diagnostics"


def test_every_tool_is_read_only_and_closed_world_free_of_destructive_hints():
    for name, tool in tools(server()).items():
        assert tool.annotations.read_only_hint is True, name
        assert not tool.annotations.destructive_hint, name
        assert tool.description and len(tool.description) > 20


def test_input_schemas_are_strict_and_expose_no_database_sql_or_free_text():
    listed = tools(server())
    for name, tool in listed.items():
        schema = tool.input_schema
        Draft202012Validator.check_schema(schema)
        props = set(schema.get("properties", {}))
        assert props <= {"relationship", "table"}, name
        assert not ({"sql", "query", "database", "db_name", "statement", "login"} & props)
    def enum_of(tool, argument):
        options = listed[tool].input_schema["properties"][argument]["anyOf"]
        enums = [o["enum"] for o in options if "enum" in o]
        assert len(enums) == 1 and {"type": "null"} in options  # one closed list, or omitted
        return enums[0]

    assert enum_of("check_referential_integrity", "relationship") == list(dx.RELATIONSHIPS)
    assert enum_of("check_missing_values", "table") == list(ECOMMERCE_TABLES)


@pytest.mark.parametrize("name", sorted(EIGHT))
def test_undeclared_arguments_are_rejected_before_anything_runs(name):
    record = []
    srv = create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)))
    result = call(srv, name, {"sql": "DELETE FROM ecom_orders"})
    assert result.is_error is True and "does not accept argument(s): sql" in result.content[0].text
    assert record == []  # no connection was even opened


@pytest.mark.parametrize("name,args", [
    ("check_referential_integrity", {"relationship": "orders; DROP TABLE x"}),
    ("check_missing_values", {"table": "sys.sql_logins"}),
    ("check_missing_values", {"table": "ecom_orders; DELETE"}),
])
def test_values_outside_the_allow_lists_are_rejected(name, args):
    record = []
    srv = create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)))
    assert call(srv, name, args).is_error is True and record == []


@pytest.mark.parametrize("name,args", [(n, {}) for n in sorted(EIGHT)] + [
    ("check_referential_integrity", {"relationship": "reviews_orders"}), ("check_missing_values", {"table": "ecom_orders"})])
def test_structured_output_validates_against_the_declared_schema(name, args):
    listed = tools(server())
    result = call(server(), name, args)
    assert result.structured_content is not None
    Draft202012Validator(listed[name].output_schema).validate(result.structured_content)
    blob = json.dumps(result.structured_content)
    assert not any(secret in blob for secret in SECRET_VALUES + ["dq_reader", "FIXTUREPC"])


def test_the_rollup_carries_verification_counts_and_per_check_statuses():
    data = call(server(), "run_ecommerce_diagnostics").structured_content
    summary = data["ecommerce_summary"]
    assert summary["target_verified"] is True and summary["execution"] == "complete"
    assert set(summary["checks"]) == {"passed", "failed", "flagged", "unassessed"}
    assert set(summary["rules"]) == {"passed", "failed", "flagged", "pending_investigation", "skipped", "unassessed"}
    assert sum(summary["checks"].values()) == 7 == len(summary["diagnostics"])
    assert summary["rules"]["pending_investigation"] == 1 and summary["rules"]["unassessed"] == 0
    assert [d["diagnostic"] for d in summary["diagnostics"]] == list(dx.CHECK_NAMES)


def test_a_refused_guard_is_an_error_everywhere_and_never_a_clean_result():
    srv = server(permissions=permission_row(is_sysadmin=1))
    for name in sorted(EIGHT):
        result = call(srv, name)
        data = result.structured_content
        assert result.is_error is True and (data.get("status") or data.get("overall_status")) == "error"
    rollup = call(srv, "run_ecommerce_diagnostics").structured_content
    assert rollup["overall_status"] == "error" and rollup["ecommerce_summary"]["target_verified"] is False
    assert rollup["ecommerce_summary"]["execution"] == "incomplete"


def test_an_unverifiable_database_cannot_produce_a_successful_target_check():
    srv = create_ecommerce_server(EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), permissions=None)))
    data = call(srv, "check_ecommerce_target").structured_content
    assert data["status"] == "error" and data["error_code"] == "ECOM-GUARD-REFUSED"


def test_starting_the_server_connects_to_nothing_and_verifies_nothing():
    """Creating the server must not call the connection factory; verification happens per tool call."""
    opened = []

    def connect():
        opened.append(1)
        raise AssertionError("must not connect at start-up")

    srv = create_ecommerce_server(EcommerceDiagnosticsService(connect=connect))
    assert set(tools(srv)) == EIGHT and opened == []


def test_list_only_cli_makes_no_connection_and_loads_no_settings(monkeypatch, capsys):
    import app.config
    import app.db.connection as connection
    from scripts import ecommerce_check

    def forbidden(*a, **k):
        raise AssertionError("list mode touched settings or the database")

    monkeypatch.setattr(connection, "make_engine", forbidden)
    monkeypatch.setattr(app.config, "get_settings", forbidden)
    monkeypatch.setattr(sys, "argv", ["ecommerce_check", "--list"])
    assert ecommerce_check.main() == 0
    out = capsys.readouterr().out
    assert all(name in out for name in EIGHT) and "no database connection made" in out


def test_real_entry_point_starts_and_lists_tools_without_a_database():
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server.ecommerce"], cwd=str(ROOT))

    async def go():
        async with Client(params) as client:
            return (await client.list_tools()).tools

    assert {t.name for t in run(go())} == EIGHT


# ----------------------------------------------------------------------------------------- source rules
PACKAGE_FILES = sorted((ROOT / "app" / "ecommerce").glob("*.py")) + sorted((ROOT / "app" / "mcp_server" / "ecommerce").glob("*.py"))
FORBIDDEN_CALLS = {"print", "open", "exec", "eval", "system", "popen", "Popen"}
FORBIDDEN_IMPORTS = {"subprocess", "shutil", "socket"}
WRITE_WORDS = ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "DROP ", "ALTER ", "CREATE TABLE", "TRUNCATE", "GRANT ")


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_ecommerce_sources_have_no_dangerous_calls_or_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            assert name not in FORBIDDEN_CALLS, f"{path.name}: {name}()"
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & FORBIDDEN_IMPORTS
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_IMPORTS
    source = path.read_text(encoding="utf-8")
    assert "allow_unverified_read_only" not in source and "openai" not in source.lower()
    if path.name != "guard.py":  # the guard lists the forbidden words in order to refuse them
        assert not any(w in source for w in WRITE_WORDS), path.name


def test_the_ecommerce_package_never_imports_governance_recovery_or_the_simulation():
    for path in PACKAGE_FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for mod in mods:
                assert not mod.startswith(("app.governance", "app.recovery", "app.simulation")), (path.name, mod)


def test_the_database_name_is_defined_once():
    new_code = [*(ROOT / "app" / "ecommerce").rglob("*.py"), *(ROOT / "app" / "mcp_server" / "ecommerce").rglob("*.py"),
                *(ROOT / "app" / "agent").rglob("*.py")]
    hits = [p for p in new_code if f'"{ECOMMERCE_DATABASE}"' in p.read_text(encoding="utf-8")]
    assert [p.relative_to(ROOT).as_posix() for p in hits] == ["app/ecommerce/target.py"]


def test_documentation_lists_every_tool_and_the_commands():
    doc = (ROOT / "docs" / "ecommerce-agent.md").read_text(encoding="utf-8")
    for name in EIGHT:
        assert f"`{name}`" in doc, name
    assert "python -m app.mcp_server.ecommerce" in doc and "--list" in doc and "pending investigation" in doc
    assert "No live SQL Server validation" in doc or "No live" in doc
