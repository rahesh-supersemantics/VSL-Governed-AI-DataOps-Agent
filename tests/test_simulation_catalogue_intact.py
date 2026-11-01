"""Regression: the original 11-tool simulation MCP server is unchanged by the Ecommerce work.

``tests/data/sim_mcp_catalogue.json`` was generated from the unmodified code before the Ecommerce changes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client

from app.mcp_server import ServiceProvider, create_server

SNAPSHOT = Path(__file__).parent / "data" / "sim_mcp_catalogue.json"


def _live():
    def no_service():
        raise AssertionError("listing tools must not build a service")

    server = create_server(ServiceProvider(no_service))

    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def test_the_eleven_simulation_tools_are_unchanged():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    tools = _live()
    assert expected["server_name"] == "dataops-diagnostics" and len(expected["tools"]) == 11 == len(tools)
    live = {
        t.name: {"description": t.description, "destructive_hint": t.annotations.destructive_hint,
                 "read_only_hint": t.annotations.read_only_hint, "input_schema": t.input_schema,
                 "output_schema": t.output_schema}
        for t in tools
    }
    snapshot = expected["tools"] if isinstance(expected["tools"], dict) else {t["name"]: t for t in expected["tools"]}
    assert set(live) == set(snapshot)
    for name in snapshot:
        for key in ("description", "read_only_hint", "destructive_hint", "input_schema", "output_schema"):
            assert json.loads(json.dumps(live[name][key])) == snapshot[name][key], (name, key)


def test_the_simulation_server_still_reads_the_shared_database_setting_only():
    import inspect

    from app.mcp_server import provider

    source = inspect.getsource(provider)
    assert "make_engine" in source and "dataops_ecommerce_demo" not in source
