"""Start the Ecommerce MCP server over stdio:  python -m app.mcp_server.ecommerce

stdout carries the MCP protocol, so nothing here may print to it; logs go to stderr. The process runs as whatever Windows
account started it. Start it as the dedicated read-only account: every tool call verifies that, and refuses otherwise.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    os.chdir(PROJECT_ROOT)  # .env is only read; settings are relative to the project root

    from app.core.logging import configure_logging
    from app.mcp_server.ecommerce import create_ecommerce_server

    try:
        from app.config import get_settings

        configure_logging(get_settings().log_level)
    except Exception:  # a bad .env must not stop the server from starting; tools report the refusal
        configure_logging("INFO")
    create_ecommerce_server().run("stdio")


if __name__ == "__main__":
    main()
