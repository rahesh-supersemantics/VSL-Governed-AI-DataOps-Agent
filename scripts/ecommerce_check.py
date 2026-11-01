"""Inspect the Ecommerce MCP server.

    python -m scripts.ecommerce_check --list     # list the tools; never touches a database
    python -m scripts.ecommerce_check --verify   # LIVE: call check_ecommerce_target through the server (run as the reader account)

``--verify`` starts ``python -m app.mcp_server.ecommerce`` under the current Windows account. The server verifies the
database, the account and its read-only permissions before reading anything and refuses otherwise. It only reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import Client

from app.agent.mcp_client import default_server_params
from app.ecommerce.target import SERVER_NAME
from scripts._common import EXIT_CHECK_FAILED, EXIT_OK, run_cli

MODULE = "app.mcp_server.ecommerce"


def _list_only() -> int:
    """Print the catalogue from source. Starts no server, loads no settings and opens no connection."""
    from app.mcp_server.ecommerce.server import TOOL_CATALOGUE

    print(f"{SERVER_NAME}: {len(TOOL_CATALOGUE)} tool(s), all read-only (list only; no database connection made)")
    for name, description in sorted(TOOL_CATALOGUE.items()):
        print(f"  {name}  [read-only]  {description}")
    return EXIT_OK


async def _run(verify: bool) -> int:
    async with Client(default_server_params(MODULE), read_timeout_seconds=60) as client:
        tools = (await client.list_tools()).tools
        print(f"{SERVER_NAME}: {len(tools)} tool(s)")
        for tool in sorted(tools, key=lambda t: t.name):
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            print(f"  {tool.name}  [{'read-only' if read_only else 'NOT READ-ONLY'}]")
        if not verify:
            return EXIT_OK
        result = await client.call_tool("check_ecommerce_target", {}, read_timeout_seconds=60)
        data = result.structured_content or {}
        print(json.dumps({k: data.get(k) for k in ("status", "severity", "summary", "error_code", "observed_value")}, indent=2, default=str))
        return EXIT_OK if data.get("status") == "ok" else EXIT_CHECK_FAILED


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ecommerce_check", description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list tools only (no database access)")
    group.add_argument("--verify", action="store_true", help="LIVE read-only target verification")
    args = parser.parse_args()
    if args.list:
        return _list_only()
    return asyncio.run(_run(True))


if __name__ == "__main__":
    run_cli(main)
