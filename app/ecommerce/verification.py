"""Check that an investigation report really contains a verified Ecommerce target (used by the CLI)."""

from __future__ import annotations

from typing import Any

from app.ecommerce.target import ECOMMERCE_DATABASE

TARGET_TOOL = "check_ecommerce_target"


def target_confirmation(report: dict[str, Any]) -> tuple[bool, str]:
    """``(True, ...)`` only if the report holds an ``ok`` target check for the fixed database with ``read_only_verified``."""
    for item in report.get("evidence") or []:
        if item.get("tool") != TARGET_TOOL:
            continue
        if item.get("status") != "ok":
            return False, f"the target check reported {item.get('status')}" + (
                f" ({item.get('error_code')})" if item.get("error_code") else "")
        observed = (item.get("digest") or {}).get("observed_value") or {}
        if observed.get("database") != ECOMMERCE_DATABASE:
            return False, "the target check did not report the expected database"
        if observed.get("read_only_verified") is not True:
            return False, "the target check did not report a verified read-only account"
        return True, f"{ECOMMERCE_DATABASE} reached as a verified read-only account"
    return False, "no target check ran, so the database and account were not confirmed"
