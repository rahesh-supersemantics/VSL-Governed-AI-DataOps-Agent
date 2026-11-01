"""Fail-closed verification that data-quality tooling runs read-only against a local, expected database.

SELECT-only application SQL is NOT a database-level guarantee, so before a tool runs any rule query it asks SQL
Server what the connected principal can actually do (``IS_SRVROLEMEMBER``, ``IS_MEMBER``, ``HAS_PERMS_BY_NAME``).
Everything here only reads metadata and issues one ``SELECT``. Nothing is granted, denied, created or changed.

Rules:

* Only a definite "no" for every write/DDL capability counts as verified. Anything NULL, missing, unexpected or
  failing is *inconclusive*, and inconclusive is treated like a failure by callers (fail closed).
* The result never contains the login name. It carries a short SHA-256 fingerprint so two runs can be compared
  without recording who ran them.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from sqlalchemy import text

from app.db.connection import is_local_server
from app.db.safety import UnsafeTarget

# Capabilities that make a principal write- or DDL-capable. Every one must be a definite 0.
WRITE_CAPABLE_FLAGS: tuple[str, ...] = (
    "is_sysadmin",
    "has_control_server",
    "is_db_owner",
    "is_db_datawriter",
    "is_db_ddladmin",
    "is_db_securityadmin",
    "is_db_accessadmin",
    "has_database_control",
    "has_database_alter",
    "can_create_table",
    "can_create_view",
    "can_create_procedure",
    "can_execute",
    "can_insert",
    "can_update",
    "can_delete",
)

# One read-only statement. Each value is 1 (yes), 0 (no) or NULL (unknown; treated as inconclusive).
PERMISSION_SQL = """
SELECT
  DB_NAME() AS database_name,
  SUSER_SNAME() AS login_name,
  CAST(IS_SRVROLEMEMBER('sysadmin') AS int) AS is_sysadmin,
  CAST(HAS_PERMS_BY_NAME(NULL, NULL, 'CONTROL SERVER') AS int) AS has_control_server,
  CAST(IS_MEMBER('db_owner') AS int) AS is_db_owner,
  CAST(IS_MEMBER('db_datawriter') AS int) AS is_db_datawriter,
  CAST(IS_MEMBER('db_ddladmin') AS int) AS is_db_ddladmin,
  CAST(IS_MEMBER('db_securityadmin') AS int) AS is_db_securityadmin,
  CAST(IS_MEMBER('db_accessadmin') AS int) AS is_db_accessadmin,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL') AS int) AS has_database_control,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER') AS int) AS has_database_alter,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE') AS int) AS can_create_table,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE VIEW') AS int) AS can_create_view,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE PROCEDURE') AS int) AS can_create_procedure,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'EXECUTE') AS int) AS can_execute,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS int) AS can_insert,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE') AS int) AS can_update,
  CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE') AS int) AS can_delete,
  (SELECT COUNT_BIG(*) FROM sys.tables AS t
    WHERE HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name), N'OBJECT', N'INSERT') = 1
       OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name), N'OBJECT', N'UPDATE') = 1
       OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name), N'OBJECT', N'DELETE') = 1
       OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name), N'OBJECT', N'ALTER') = 1
       OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + N'.' + QUOTENAME(t.name), N'OBJECT', N'CONTROL') = 1
  ) AS writable_table_count,
  (SELECT COUNT_BIG(*) FROM sys.tables) AS table_count,
  CAST(HAS_DBACCESS('dataops_sim') AS int) AS can_access_dataops_sim
"""


def assert_local_target(settings: Any) -> None:
    """Refuse a server that is not demonstrably local, like ``app.db.safety.assert_safe_reset_target``."""
    server = getattr(settings, "db_server", None)
    if not isinstance(server, str) or not server.strip():
        raise UnsafeTarget("Refusing: DB_SERVER is not configured, so the target cannot be confirmed as a local server.")
    if not is_local_server(server):
        raise UnsafeTarget("Refusing: DB_SERVER is not a local server. Data-quality tooling only runs against a local SQL Server.")


def _is_flag(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evaluate_permissions(row: Any, expected_database: str) -> dict[str, Any]:
    """Turn the permission row into a verdict. Pure function; ``verified`` is True only for a definite read-only result."""
    result: dict[str, Any] = {
        "verified": False, "status": "inconclusive", "reasons": [], "checks": {},
        "writable_table_count": None, "table_count": None, "database": None,
        "principal_fingerprint": None, "principal_is_windows_login": None, "dataops_sim_accessible": None,
    }
    if not isinstance(row, Mapping):
        result["reasons"].append("permission_row_missing")
        return result

    write_capable: list[str] = []
    unknown: list[str] = []
    wrong_db = False

    database = row.get("database_name")
    if isinstance(database, str):
        result["database"] = database
    if not isinstance(database, str) or database.lower() != expected_database.lower():
        wrong_db = True

    login = row.get("login_name")
    if isinstance(login, str) and login.strip():
        result["principal_fingerprint"] = hashlib.sha256(login.strip().lower().encode("utf-8")).hexdigest()[:12]
        result["principal_is_windows_login"] = "\\" in login
    else:
        unknown.append("principal_unknown")

    for name in WRITE_CAPABLE_FLAGS:
        value = row.get(name)
        if not _is_flag(value):
            unknown.append(f"{name}_unknown")
            continue
        result["checks"][name] = value
        if value == 1:
            write_capable.append(name)

    writable, tables = row.get("writable_table_count"), row.get("table_count")
    if _is_count(writable):
        result["writable_table_count"] = int(writable)
        if writable > 0:
            write_capable.append("writable_tables")
    else:
        unknown.append("writable_table_count_unknown")
    if _is_count(tables):
        result["table_count"] = int(tables)
        if tables == 0:
            unknown.append("no_visible_tables")  # cannot judge object permissions without any table to test
    else:
        unknown.append("table_count_unknown")

    sim = row.get("can_access_dataops_sim")  # informational only; never part of the verdict
    result["dataops_sim_accessible"] = sim if _is_flag(sim) else None

    if wrong_db:
        result["status"], result["reasons"] = "wrong_database", ["database_mismatch"]
    elif write_capable:
        result["status"], result["reasons"] = "write_capable", [f"write_capable:{n}" for n in write_capable]
    elif unknown:
        result["status"], result["reasons"] = "inconclusive", unknown
    else:
        result["verified"], result["status"] = True, "verified"
    return result


def verify_read_only_principal(conn: Any, expected_database: str) -> dict[str, Any]:
    """Run the permission query and evaluate it. Any failure is inconclusive; no driver text is kept."""
    try:
        row = conn.execute(text(PERMISSION_SQL)).mappings().first()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure means "could not verify"
        return {**evaluate_permissions(None, expected_database), "reasons": [f"permission_query_failed:{type(exc).__name__}"]}
    return evaluate_permissions(row, expected_database)
