"""The Ecommerce MCP server (``python -m app.mcp_server.ecommerce``). Separate from the simulation server."""

from app.mcp_server.ecommerce.server import TOOL_CATALOGUE, create_ecommerce_server

__all__ = ["TOOL_CATALOGUE", "create_ecommerce_server"]
