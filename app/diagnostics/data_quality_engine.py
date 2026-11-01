"""Read-only, configuration-driven data-quality checks for SQL Server.

Safety model (see docs on the Stage 2 audit):

* Rule files are validated against a strict schema (unknown keys, bad types and unsupported combinations are
  rejected) and every enabled rule is turned into SQL and checked against the catalog BEFORE any rule query runs.
* Identifiers come only from the SQL Server catalog and are bracket-quoted; values are bound parameters; operators
  come from fixed allow-lists. Rule SQL is SELECT-only.
* SELECT-only SQL is not a database-level guarantee. Before any rule query, the engine checks that the server is
  local, that the connection landed in the expected database, and what the connected principal can actually do. If
  that cannot be verified the run stops (fail closed). ``read_only_verified`` in the report is a computed result,
  never a constant.
* Reports are written atomically, the previous report is preserved, and they contain counts and hashes only: no
  record values, no connection details and no login names.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import inspect, text

from app.config import get_settings
from app.db.connection import make_engine, safe_error_summary
from app.db.readonly_preflight import assert_local_target, verify_read_only_principal
from app.db.safety import UnsafeTarget
from app.diagnostics.report_io import write_json_atomic

TARGET_DB = "dataops_ecommerce_demo"

REPORT_SCHEMA_VERSION = 2
ENGINE_VERSION = "2.0"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "data_quality_rules.yaml"
DEFAULT_REPORT_PATH = REPO_ROOT / "reports" / "data_quality_report.json"
MAX_CONFIG_BYTES = 1_000_000
MAX_RULES = 500
MAX_IDENTIFIER_LENGTH = 128
MAX_CONDITION_VALUE_LENGTH = 256
MAX_REPORTED_CONFIG_ERRORS = 50

SUPPORTED_TYPES = {
    "foreign_key",
    "conditional_not_null",
    "column_comparison",
    "unique",
    "not_null",
}
SUPPORTED_SEVERITIES = {"critical", "high", "medium", "warning"}
SUPPORTED_CLASSIFICATIONS = {"integrity", "business_signal"}
CONDITION_OPERATORS = {"equals"}
COMPARISON_OPERATORS = {
    "greater_than_or_equal": ">=",
    "less_than_or_equal": "<=",
}

_TOP_LEVEL_KEYS = {"version", "database", "execution", "rules"}
_EXECUTION_KEYS = {"mode", "stop_on_error"}
_COMMON_RULE_KEYS = {"id", "name", "table", "type", "severity", "enabled", "notes", "classification"}
_TYPE_KEYS = {
    "foreign_key": {"column", "references"},
    "not_null": {"column"},
    "conditional_not_null": {"column", "condition"},
    "unique": {"columns"},
    "column_comparison": {"left_column", "right_column", "operator", "condition"},
}
_REQUIRED_KEYS = {
    "foreign_key": {"column", "references"},
    "not_null": {"column"},
    "conditional_not_null": {"column", "condition"},
    "unique": {"columns"},
    "column_comparison": {"left_column", "right_column", "operator"},
}
_CONDITION_TYPES = {"conditional_not_null", "column_comparison"}
_RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class DataQualityConfigError(ValueError):
    """The rule configuration is invalid. The message is safe to show (no values from the database)."""


class _RunAborted(Exception):
    """The run cannot continue safely; ``kind`` is a fixed, safe label."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def qi(identifier: str) -> str:
    """Safely quote a SQL Server identifier."""
    if not isinstance(identifier, str) or not identifier:
        raise DataQualityConfigError("Identifiers must be non-empty strings.")
    if len(identifier) > MAX_IDENTIFIER_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in identifier):
        raise DataQualityConfigError("Identifier is too long or contains control characters.")
    return "[" + identifier.replace("]", "]]") + "]"


# ----------------------------------------------------------------------------------------- configuration loading
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one)."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "found an unhashable mapping key", key_node.start_mark) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "found a duplicate key", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _read_config(path: str | Path) -> tuple[dict, str]:
    """Read, parse and validate the configuration. Returns ``(config, sha256_of_file_bytes)``."""
    config_path = Path(path)
    try:
        size = config_path.stat().st_size
        if size > MAX_CONFIG_BYTES:
            raise DataQualityConfigError("Configuration file is too large.")
        raw = config_path.read_bytes()
        content = raw.decode("utf-8-sig")  # tolerate a BOM written by Windows editors
    except DataQualityConfigError:
        raise
    except UnicodeDecodeError:
        raise DataQualityConfigError("Configuration file is not valid UTF-8 text.") from None
    except OSError:
        raise DataQualityConfigError("Configuration file could not be read (missing or not accessible).") from None

    try:
        config = yaml.load(content, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        problem = str(getattr(exc, "problem", "") or "")[:80]
        detail = f" ({problem})" if problem else ""
        raise DataQualityConfigError(f"Configuration file is not valid YAML{where}{detail}.") from None

    _validate_config(config)
    return config, hashlib.sha256(raw).hexdigest()


def load_rules(path: str | Path) -> dict:
    """Load and strictly validate the YAML configuration."""
    config, _digest = _read_config(path)
    return config


def _fail(message: str) -> DataQualityConfigError:
    return DataQualityConfigError(message)


def _check_keys(where: str, mapping: dict, allowed: set, required: set = frozenset()) -> None:
    unknown = sorted(str(k)[:40] for k in mapping if k not in allowed)
    if unknown:
        raise _fail(f"{where}: unknown or unsupported key(s): {', '.join(unknown)}.")
    missing = sorted(required - set(mapping))
    if missing:
        raise _fail(f"{where}: missing required key(s): {', '.join(missing)}.")


def _check_identifier(rule_id: str, label: str, value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(f"{rule_id}: {label} must be a non-empty string without surrounding spaces.")
    if len(value) > MAX_IDENTIFIER_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _fail(f"{rule_id}: {label} is too long or contains control characters.")
    return value


def _check_condition(rule_id: str, rule_type: str, condition: Any) -> None:
    if rule_type not in _CONDITION_TYPES:
        raise _fail(f"{rule_id}: 'condition' is not supported for rule type {rule_type!r}.")
    if not isinstance(condition, dict):
        raise _fail(f"{rule_id}: condition must be a mapping.")
    _check_keys(f"{rule_id}: condition", condition, {"column", "operator", "value"}, {"column", "operator", "value"})
    if condition["operator"] not in CONDITION_OPERATORS:
        raise _fail(f"{rule_id}: unsupported condition operator.")
    _check_identifier(rule_id, "condition column", condition["column"])
    value = condition["value"]
    if isinstance(value, float) and not math.isfinite(value):
        raise _fail(f"{rule_id}: condition value must be a finite number (NaN and infinity are not allowed).")
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return
    if isinstance(value, str) and 0 < len(value) <= MAX_CONDITION_VALUE_LENGTH:
        return
    raise _fail(f"{rule_id}: condition value must be a non-empty scalar (string, number or boolean; quote dates).")


def _check_columns_list(rule_id: str, label: str, columns: Any) -> None:
    if not isinstance(columns, list) or not columns:
        raise _fail(f"{rule_id}: {label} must be a non-empty list.")
    seen: set[str] = set()
    for column in columns:
        _check_identifier(rule_id, "column name", column)
        if column.lower() in seen:  # SQL Server identifiers are case-insensitive by default
            raise _fail(f"{rule_id}: duplicate column name in {label}.")
        seen.add(column.lower())


def _validate_rule(rule: Any, seen_ids: set) -> None:
    if not isinstance(rule, dict):
        raise _fail("Each rule must be a mapping.")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise _fail("Every rule needs a non-empty id.")
    if not _RULE_ID.match(rule_id):
        raise _fail("Rule ids may only contain letters, digits, '_', '-' and '.' (max 64 characters).")
    if rule_id in seen_ids:
        raise _fail(f"Duplicate rule id: {rule_id}")
    seen_ids.add(rule_id)

    rule_type = rule.get("type")
    if rule_type not in SUPPORTED_TYPES:
        raise _fail(f"{rule_id}: unsupported rule type.")
    if "condition" in rule and rule_type not in _CONDITION_TYPES:
        raise _fail(f"{rule_id}: 'condition' is not supported for rule type {rule_type!r}.")
    _check_keys(rule_id, rule, _COMMON_RULE_KEYS | _TYPE_KEYS[rule_type],
                {"id", "type", "table", "severity", "enabled"} | _REQUIRED_KEYS[rule_type])

    _check_identifier(rule_id, "table", rule["table"])
    if rule["severity"] not in SUPPORTED_SEVERITIES:
        raise _fail(f"{rule_id}: invalid severity.")
    if not isinstance(rule["enabled"], bool):
        raise _fail(f"{rule_id}: 'enabled' must be a boolean (true or false), not a string or a missing value.")
    if rule.get("classification", "integrity") not in SUPPORTED_CLASSIFICATIONS:
        raise _fail(f"{rule_id}: classification must be 'integrity' or 'business_signal'.")
    for label in ("name", "notes"):
        if label in rule and not isinstance(rule[label], str):
            raise _fail(f"{rule_id}: {label} must be a string.")

    if rule_type in {"not_null", "conditional_not_null"}:
        _check_identifier(rule_id, "column", rule["column"])
    elif rule_type == "foreign_key":
        _check_identifier(rule_id, "column", rule["column"])
        ref = rule["references"]
        if not isinstance(ref, dict):
            raise _fail(f"{rule_id}: references must be a mapping with table and column.")
        _check_keys(f"{rule_id}: references", ref, {"table", "column"}, {"table", "column"})
        _check_identifier(rule_id, "reference table", ref["table"])
        _check_identifier(rule_id, "reference column", ref["column"])
    elif rule_type == "unique":
        _check_columns_list(rule_id, "columns", rule["columns"])
    elif rule_type == "column_comparison":
        _check_identifier(rule_id, "left_column", rule["left_column"])
        _check_identifier(rule_id, "right_column", rule["right_column"])
        if rule["left_column"].lower() == rule["right_column"].lower():
            raise _fail(f"{rule_id}: left_column and right_column must differ.")
        if rule["operator"] not in COMPARISON_OPERATORS:
            raise _fail(f"{rule_id}: unsupported comparison operator.")

    if "condition" in rule:
        _check_condition(rule_id, rule_type, rule["condition"])
    rule.setdefault("classification", "integrity")


def _validate_config(config: Any) -> None:
    """Validate (and normalise in place) a parsed configuration. Raises ``DataQualityConfigError``."""
    if not isinstance(config, dict):
        raise _fail("Configuration root must be a mapping.")
    _check_keys("configuration", config, _TOP_LEVEL_KEYS)
    if config.get("database") != TARGET_DB:
        raise _fail(f"Only database {TARGET_DB!r} is permitted.")
    version = config.get("version")
    if isinstance(version, bool) or version != 1:
        raise _fail("Unsupported configuration 'version' (expected 1).")

    execution = config.get("execution")
    if execution is None:
        execution = {}
    if not isinstance(execution, dict):
        raise _fail("'execution' must be a mapping.")
    _check_keys("execution", execution, _EXECUTION_KEYS)
    if execution.get("mode", "read_only") != "read_only":
        raise _fail("execution.mode must be 'read_only'; no other mode is supported.")
    stop_on_error = execution.get("stop_on_error", False)
    if not isinstance(stop_on_error, bool):
        raise _fail("execution.stop_on_error must be a boolean.")
    config["execution"] = {"mode": "read_only", "stop_on_error": stop_on_error}

    rules = config.get("rules")
    if not isinstance(rules, list):
        raise _fail("'rules' must be a list.")
    if not rules or len(rules) > MAX_RULES:
        raise _fail(f"'rules' must contain between 1 and {MAX_RULES} rules.")
    seen_ids: set = set()
    for rule in rules:
        _validate_rule(rule, seen_ids)


# ----------------------------------------------------------------------------------------------- SQL building
def _validate_column(inspector, table: str, column: str, known_tables: set, columns_cache: dict | None = None) -> None:
    """Reject unknown tables and columns before building SQL."""
    if table not in known_tables:
        raise DataQualityConfigError(f"Unknown table: {table}")
    columns = columns_cache.get(table) if columns_cache is not None else None
    if columns is None:
        columns = {c["name"] for c in inspector.get_columns(table, schema="dbo")}
        if columns_cache is not None:
            columns_cache[table] = columns
    if column not in columns:
        raise DataQualityConfigError(f"Unknown column {column!r} in table {table!r}.")


def _condition_sql(rule: dict, inspector, tables: set, columns_cache: dict | None = None) -> tuple[str, dict]:
    condition = rule.get("condition")
    if rule["type"] not in _CONDITION_TYPES:
        if condition:
            raise DataQualityConfigError(f"{rule.get('id', 'rule')}: condition is not supported for this rule type.")
        return "", {}
    if not condition:
        if rule["type"] == "conditional_not_null":
            raise DataQualityConfigError(f"{rule.get('id', 'rule')}: conditional_not_null requires a condition.")
        return "", {}
    if condition.get("operator") not in CONDITION_OPERATORS:
        raise DataQualityConfigError("Unsupported condition operator.")
    value = condition.get("value")
    if value is None or not isinstance(value, (str, int, float, bool)):
        raise DataQualityConfigError("Condition value must be a scalar; NULL is not allowed.")

    table = rule["table"]
    column = condition["column"]
    _validate_column(inspector, table, column, tables, columns_cache)
    return f" AND t.{qi(column)} = :condition_value", {"condition_value": value}


def _build_query(rule: dict, inspector, tables: set, columns_cache: dict | None = None) -> tuple[str, dict]:
    """Build a COUNT_BIG query from validated rule definitions only."""
    rule_type = rule["type"]
    table = rule["table"]
    table_sql = f"dbo.{qi(table)}"
    params: dict = {}
    if rule.get("condition") and rule_type not in _CONDITION_TYPES:
        # Defence in depth: load_rules already rejects this; never silently run a broader rule than was written.
        raise DataQualityConfigError(f"{rule.get('id', 'rule')}: condition is not supported for this rule type.")

    if rule_type == "foreign_key":
        column = rule["column"]
        ref = rule["references"]
        _validate_column(inspector, table, column, tables, columns_cache)
        _validate_column(inspector, ref["table"], ref["column"], tables, columns_cache)
        sql = (
            f"SELECT COUNT_BIG(*) FROM {table_sql} AS t "
            f"WHERE t.{qi(column)} IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM dbo.{qi(ref['table'])} AS p "
            f"WHERE p.{qi(ref['column'])} = t.{qi(column)})"
        )

    elif rule_type == "not_null":
        column = rule["column"]
        _validate_column(inspector, table, column, tables, columns_cache)
        sql = f"SELECT COUNT_BIG(*) FROM {table_sql} AS t WHERE t.{qi(column)} IS NULL"

    elif rule_type == "conditional_not_null":
        column = rule["column"]
        _validate_column(inspector, table, column, tables, columns_cache)
        cond_sql, params = _condition_sql(rule, inspector, tables, columns_cache)
        sql = f"SELECT COUNT_BIG(*) FROM {table_sql} AS t WHERE t.{qi(column)} IS NULL{cond_sql}"

    elif rule_type == "unique":
        columns = rule["columns"]
        if len({c.lower() for c in columns}) != len(columns):
            raise DataQualityConfigError(f"{rule.get('id', 'rule')}: duplicate column name in columns.")
        for column in columns:
            _validate_column(inspector, table, column, tables, columns_cache)
        key_sql = ", ".join(qi(c) for c in columns)
        sql = (
            f"SELECT COUNT_BIG(*) FROM ("
            f"SELECT {key_sql} FROM {table_sql} "
            f"GROUP BY {key_sql} HAVING COUNT_BIG(*) > 1"
            f") AS duplicates"
        )

    elif rule_type == "column_comparison":
        left = rule["left_column"]
        right = rule["right_column"]
        _validate_column(inspector, table, left, tables, columns_cache)
        _validate_column(inspector, table, right, tables, columns_cache)
        operator = COMPARISON_OPERATORS[rule["operator"]]
        cond_sql, params = _condition_sql(rule, inspector, tables, columns_cache)
        sql = (
            f"SELECT COUNT_BIG(*) FROM {table_sql} AS t "
            f"WHERE t.{qi(left)} IS NOT NULL "
            f"AND t.{qi(right)} IS NOT NULL "
            f"AND NOT (t.{qi(left)} {operator} t.{qi(right)})"
            f"{cond_sql}"
        )
    else:
        raise DataQualityConfigError("Unsupported rule type.")

    return sql, params


def _preflight(rules: list[dict], inspector, tables: set) -> tuple[list[tuple[dict, str, dict]], list[dict]]:
    """Build and catalog-check the SQL of every enabled rule. Executes nothing."""
    cache: dict = {}
    plans: list[tuple[dict, str, dict]] = []
    errors: list[dict] = []
    for rule in rules:
        if rule["enabled"] is not True:
            continue
        try:
            sql, params = _build_query(rule, inspector, tables, cache)
            plans.append((rule, sql, params))
        except DataQualityConfigError as exc:
            errors.append({"rule_id": rule["id"], "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - e.g. a catalog lookup failure; never echo driver text
            errors.append({"rule_id": rule["id"], "error": f"Rule could not be validated ({type(exc).__name__})."})
    return plans, errors[:MAX_REPORTED_CONFIG_ERRORS]


# ------------------------------------------------------------------------------------------------- reporting
def _unit(rule: dict) -> str:
    return "groups" if rule["type"] == "unique" else "rows"


def _rule_header(rule: dict) -> dict:
    return {
        "id": rule["id"], "name": rule.get("name", rule["id"]), "type": rule["type"], "table": rule["table"],
        "severity": rule["severity"], "classification": rule["classification"], "unit": _unit(rule),
    }


def _summarise(results: list[dict], total_rules: int) -> dict:
    counts = {"passed": 0, "failed": 0, "flagged": 0, "error": 0, "skipped": 0, "not_run": 0}
    failed_by_severity: dict[str, int] = {}
    flagged_by_severity: dict[str, int] = {}
    for item in results:
        counts[item["status"]] += 1
        if item["status"] == "failed":
            failed_by_severity[item["severity"]] = failed_by_severity.get(item["severity"], 0) + 1
        elif item["status"] == "flagged":
            flagged_by_severity[item["severity"]] = flagged_by_severity.get(item["severity"], 0) + 1
    enabled = counts["passed"] + counts["failed"] + counts["flagged"] + counts["error"] + counts["not_run"]
    return {
        "total_rules": total_rules,
        "enabled_rules": enabled,
        "disabled_rules": counts["skipped"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "flagged": counts["flagged"],
        "errors": counts["error"],
        "not_run": counts["not_run"],
        "failed_by_severity": failed_by_severity,
        "flagged_by_severity": flagged_by_severity,
        "reconciled": len(results) == total_rules and enabled + counts["skipped"] == total_rules,
    }


def _verdict(status: str, summary: dict) -> str:
    """not_assessed < pass < pass_with_flags < incomplete < fail (integrity failures always win). No enabled rules = not_assessed."""
    if status in {"aborted", "invalid_configuration"}:
        return "not_assessed"
    if summary["enabled_rules"] == 0:
        return "not_assessed"  # nothing was checked, so a "pass" would be a false assurance
    if summary["failed"]:
        return "fail"
    if summary["errors"] or summary["not_run"]:
        return "incomplete"
    if summary["flagged"]:
        return "pass_with_flags"
    return "pass"


def run_data_quality(
    config_path: str | Path | None = None,
    report_path: str | Path | None = None,
    *,
    allow_unverified_read_only: bool = False,
) -> dict:
    """Execute enabled rules against the explicitly permitted, local database.

    Raises ``DataQualityConfigError`` for an unreadable or invalid rule file (no connection is made). Every other
    problem is recorded in the report (``status`` "aborted" or "invalid_configuration") and the report is returned.

    ``allow_unverified_read_only`` lets a developer continue when the read-only check fails or is inconclusive. It
    never makes ``read_only_verified`` true; the report records the override in ``warnings``.
    """
    config_file = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    report_file = Path(report_path) if report_path is not None else DEFAULT_REPORT_PATH
    config, config_sha256 = _read_config(config_file)
    rules: list[dict] = config["rules"]
    stop_on_error: bool = config["execution"]["stop_on_error"]

    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "run_id": uuid.uuid4().hex,
        "database": TARGET_DB,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": None,
        "duration_seconds": None,
        "config": {"file_name": config_file.name, "sha256": config_sha256, "version": config["version"],
                   "execution": config["execution"]},
        "status": "aborted",
        "verdict": "not_assessed",
        "read_only_verified": False,
        "read_only_verification": None,
        "warnings": [],
        "target": {"expected_database": TARGET_DB, "connected_database": None, "server_is_local": None},
        "summary": {},
        "results": [],
    }

    executed: dict[str, dict] = {}
    not_run_reason = "The run was aborted before this rule was executed."
    engine = None
    try:
        settings = get_settings()
        assert_local_target(settings)
        report["target"]["server_is_local"] = True
        engine = make_engine(settings, database=TARGET_DB, admin=False)
        with engine.connect() as conn:
            actual_db = conn.execute(text("SELECT DB_NAME()")).scalar_one()
            report["target"]["connected_database"] = actual_db
            if not isinstance(actual_db, str) or actual_db.lower() != TARGET_DB.lower():
                raise _RunAborted("wrong_database", f"Wrong database: connected to {actual_db}")

            verification = verify_read_only_principal(conn, TARGET_DB)
            report["read_only_verification"] = verification
            report["read_only_verified"] = bool(verification["verified"])
            if not verification["verified"]:
                if not allow_unverified_read_only:
                    raise _RunAborted(
                        "read_only_not_verified",
                        f"Read-only access could not be verified ({verification['status']}). "
                        "Use a read-only login for dataops_ecommerce_demo; no rule query was run.")
                report["warnings"].append(
                    "Read-only access is NOT verified; the run continued only because allow_unverified_read_only was set.")
            if verification.get("dataops_sim_accessible") == 1:
                report["warnings"].append(
                    "The connected principal can access dataops_sim, which a dedicated reader should not. "
                    "This does not change the read-only verdict; review the account's database access.")

            inspector = inspect(conn)
            tables = set(inspector.get_table_names(schema="dbo"))
            plans, config_errors = _preflight(rules, inspector, tables)
            if config_errors:
                report["status"] = "invalid_configuration"
                report["config_errors"] = config_errors
                not_run_reason = "Configuration pre-flight failed; no rule query was executed."
            else:
                stopped = False
                for rule, sql, params in plans:
                    if stopped:
                        break
                    item = _rule_header(rule)
                    item["sql_sha256"] = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    began = time.perf_counter()
                    try:
                        violations = int(conn.execute(text(sql), params).scalar_one())
                        item["violations"] = violations
                        if violations == 0:
                            item["status"] = "passed"
                        else:
                            item["status"] = "failed" if rule["classification"] == "integrity" else "flagged"
                    except Exception as exc:  # noqa: BLE001 - recorded per rule; the run continues
                        item["status"] = "error"
                        item["error"] = safe_error_summary(exc)
                        stopped = stop_on_error
                    item["elapsed_ms"] = round((time.perf_counter() - began) * 1000, 1)
                    executed[rule["id"]] = item
                report["status"] = "completed"
                not_run_reason = "Not executed because execution.stop_on_error stopped the run after an error."
    except _RunAborted as exc:
        report["fatal_error_kind"] = exc.kind
        report["fatal_error"] = str(exc)
    except UnsafeTarget as exc:
        report["fatal_error_kind"] = "unsafe_target"
        report["fatal_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        report["fatal_error_kind"] = "unexpected_error"
        report["fatal_error"] = safe_error_summary(exc)
    finally:
        if engine is not None:
            engine.dispose()

    for rule in rules:
        if rule["id"] in executed:
            report["results"].append(executed[rule["id"]])
            continue
        item = _rule_header(rule)
        if rule["enabled"] is not True:
            item.update(status="skipped", reason="Rule is disabled.")
        else:
            item.update(status="not_run", reason=not_run_reason)
        report["results"].append(item)

    report["summary"] = _summarise(report["results"], len(rules))
    if report["status"] == "completed" and (report["summary"]["errors"] or report["summary"]["not_run"]):
        report["status"] = "completed_with_errors"
    report["verdict"] = _verdict(report["status"], report["summary"])
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = round(time.perf_counter() - clock, 3)

    write_json_atomic(report_file, report)
    return report
