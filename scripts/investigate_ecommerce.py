"""Investigate a data-quality concern in the Olist e-commerce demonstration database (read-only, counts only).

    python -m scripts.investigate_ecommerce "Some delivered orders have no delivery date" --offline
    python -m scripts.investigate_ecommerce "..." --offline --json
    python -m scripts.investigate_ecommerce "..."          # live: uses OpenAI (needs OPENAI_API_KEY; billable)
    python -m scripts.investigate_ecommerce --check-config # key/config status; no network, no MCP, no OpenAI

The agent reaches data only through the dedicated Ecommerce MCP server (``python -m app.mcp_server.ecommerce``), started as a
subprocess under the SAME Windows account as this command. That server refuses to read anything unless the account is the
verified read-only reader for ``dataops_ecommerce_demo``. Run this command as that account. ``--offline`` makes no model
or network call.

Exit codes: 0 no issue / warnings only; 1 issue identified or inconclusive; 2 failed, or the target/account was not confirmed;
3 invalid input or live call refused.
"""

from __future__ import annotations

import argparse
import sys

from app.agent import (
    InvalidIncident,
    LLMError,
    MissingApiKey,
    OfflineHeuristicModel,
    OpenAIChatModel,
    investigate_sync,
    load_agent_config,
    render_text,
)
from app.agent.profiles import ECOMMERCE_PROFILE
from app.ecommerce.verification import target_confirmation
from scripts._common import EXIT_ENV, EXIT_REFUSED, load_settings, run_cli

EXIT_BY_STATUS = {"no_issue_detected": 0, "warnings_only": 0, "issue_identified": 1, "inconclusive": 1, "failed": 2}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.investigate_ecommerce", description=__doc__.splitlines()[0])
    p.add_argument("incident", nargs="?", help="what looks wrong, in plain words (quote it)")
    p.add_argument("--offline", action="store_true", help="deterministic rule-based mode: no OpenAI, no key, no network")
    p.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    p.add_argument("--no-evidence", action="store_true", help="omit the per-tool evidence section from the text report")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--check-config", action="store_true", help="show configuration and key status, then exit (no network)")
    p.add_argument("--model", help="OpenAI model for this run (default: OPENAI_MODEL)")
    p.add_argument("--max-rounds", type=int, help="planning rounds (default 3, max 6)")
    p.add_argument("--max-tool-calls", type=int, help="total MCP tool calls (default 12, max 20)")
    return p


def main() -> int:
    args = _parser().parse_args()
    load_settings()  # prints a safe message and exits 2 if .env is invalid
    overrides = {"model": args.model, "max_rounds": args.max_rounds, "max_tool_calls": args.max_tool_calls}
    try:
        config = load_agent_config(**overrides)
    except ValueError as exc:
        print(f"Invalid option: {exc.__class__.__name__} (check --max-rounds 1-6 and --max-tool-calls 1-20)")
        return EXIT_REFUSED
    if args.check_config:
        print("Ecommerce agent configuration (no network calls made)")
        print(f"  OPENAI_API_KEY : {config.key_state}")  # never the value
        print(f"  model          : {config.model}")
        print("  target         : fixed by the Ecommerce MCP server (not DB_NAME)")
        print("  offline mode   : always available (use --offline)")
        return 0 if config.key_state == "configured" or args.offline else EXIT_ENV
    if args.incident is None:
        print("Give a concern, e.g.  python -m scripts.investigate_ecommerce \"Some orders have no delivery date\" --offline")
        return EXIT_REFUSED
    try:
        llm = OfflineHeuristicModel(ECOMMERCE_PROFILE) if args.offline else OpenAIChatModel(config)
    except MissingApiKey as exc:
        print(f"Cannot start a live investigation: {exc}")
        return EXIT_ENV
    progress = None if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    if progress:
        progress(f"Mode: {llm.mode} ({llm.name})" + ("" if args.offline else " - this run calls OpenAI and is billable"))
    try:
        report = investigate_sync(args.incident, config=config, llm=llm, progress=progress, profile=ECOMMERCE_PROFILE)
    except InvalidIncident as exc:
        print(f"Invalid incident: {exc}")
        return EXIT_REFUSED
    except LLMError as exc:
        print(f"Model error: {exc}")
        return EXIT_ENV

    print(report.model_dump_json(indent=2) if args.json else render_text(report, show_evidence=not args.no_evidence))
    confirmed, why = target_confirmation(report.model_dump(mode="json"))
    print(f"Target verification: {'CONFIRMED' if confirmed else 'NOT CONFIRMED'} - {why}", file=sys.stderr)
    if not confirmed:
        return 2
    return EXIT_BY_STATUS[report.status]


if __name__ == "__main__":
    run_cli(main)
