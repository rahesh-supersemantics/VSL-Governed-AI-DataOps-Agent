"""Read-only Ecommerce diagnostics for ``dataops_ecommerce_demo``. Counts only.

* Rule SQL comes from the already-tested rule engine (``_build_query``: catalog-validated, bracket-quoted identifiers,
  bound parameters, ``NULL`` handling). The YAML rule file is read, never changed; rules that are not in it
  (payments->orders, reviews->orders, key checks) are defined here.
* No result contains an order, customer, product, seller or review identifier or any text column. Evidence holds rule
  ids, table names, counts and aggregate dates only.
* A confirmed count is a fact. Nothing here states why the data looks the way it does; ``recommended_next_step`` is
  worded as a suggestion. Late delivery is a business signal, not an integrity failure, and duplicate ``review_id``
  groups are reported as a *pending investigation* (rule OLIST-009 stays disabled in the configuration).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from app.diagnostics.data_quality_engine import (
    DEFAULT_CONFIG_PATH,
    DataQualityConfigError,
    _build_query,
    _read_config,
    _validate_column,
    qi,
)
from app.diagnostics.models import (
    STATUS_ERROR,
    STATUS_ISSUE,
    STATUS_OK,
    STATUS_WARNING,
    DiagnosticResult,
    InvalidInput,
    error_result,
    worst_severity,
)
from app.ecommerce.guard import CATALOG_SQL, GuardedConnection, GuardRefusal
from app.ecommerce.target import ECOMMERCE_DATABASE, ECOMMERCE_TABLES

TARGET_NAME = "check_ecommerce_target"
INTEGRITY_NAME = "check_referential_integrity"
COMPLETENESS_NAME = "check_delivery_completeness"
TIMELINE_NAME = "check_delivery_timeline"
MISSING_NAME = "check_missing_values"
DUPLICATES_NAME = "check_duplicate_keys"
TIMESTAMPS_NAME = "profile_ecommerce_timestamps"
ALL_NAME = "run_ecommerce_diagnostics"

CHECK_NAMES = (TARGET_NAME, INTEGRITY_NAME, COMPLETENESS_NAME, TIMELINE_NAME, MISSING_NAME, DUPLICATES_NAME, TIMESTAMPS_NAME)

ECOM_GUARD_REFUSED = "ECOM-GUARD-REFUSED"
ECOM_DB_ERROR = "ECOM-DB-ERROR"
ECOM_SCHEMA_MISMATCH = "ECOM-SCHEMA-MISMATCH"
ECOM_RULE_CONFIG = "ECOM-RULE-CONFIG"
ECOM_PARTIAL = "ECOM-PARTIAL"
ECOM_TIMEOUT = "ECOM-TIMEOUT"
ECOM_BUSY = "ECOM-BUSY"

SEVERITY_MAP = {"critical": "critical", "high": "high", "medium": "medium", "warning": "low"}
COUNTS_ONLY_NOTE = "Counts only: no record identifiers, customer data or text values are returned."
CAUSE_NOTE = "Counts describe what the database contains; they do not explain why."

RELATIONSHIPS = {
    "orders_customers": "OLIST-001",
    "items_orders": "OLIST-002",
    "items_products": "OLIST-003",
    "items_sellers": "OLIST-004",
    "payments_orders": "ECOM-FK-PAYMENTS-ORDERS",
    "reviews_orders": "ECOM-FK-REVIEWS-ORDERS",
}
COMPLETENESS_RULES = ("OLIST-005", "OLIST-006")
TIMELINE_RULES = ("OLIST-007", "OLIST-008")
REVIEW_NOT_NULL_RULE = "OLIST-010"
REVIEW_UNIQUE_RULE = "OLIST-009"  # disabled in the configuration; observed only, as a pending investigation


class SchemaMismatch(Exception):
    """A table or column the check needs is not in the database (message is safe to show)."""


class RuleConfigError(Exception):
    """The rule file is unreadable or lacks a rule this module relies on (message is safe to show)."""


def _rule(rule_id: str, name: str, table: str, rule_type: str, **fields: Any) -> dict:
    rule = {"id": rule_id, "name": name, "table": table, "type": rule_type, "severity": "high", "enabled": True,
            "classification": "integrity"}
    rule.update(fields)
    return rule


MODULE_RULES: dict[str, dict] = {
    "ECOM-FK-PAYMENTS-ORDERS": _rule("ECOM-FK-PAYMENTS-ORDERS", "Payments have valid order references", "ecom_order_payments",
                                     "foreign_key", column="order_id", references={"table": "ecom_orders", "column": "order_id"}),
    "ECOM-FK-REVIEWS-ORDERS": _rule("ECOM-FK-REVIEWS-ORDERS", "Reviews have valid order references", "ecom_order_reviews",
                                    "foreign_key", column="order_id", references={"table": "ecom_orders", "column": "order_id"}),
}
_KEY_NOT_NULL = (
    ("ecom_customers", "customer_id"), ("ecom_orders", "order_id"), ("ecom_orders", "customer_id"),
    ("ecom_order_items", "order_id"), ("ecom_order_items", "product_id"), ("ecom_order_items", "seller_id"),
    ("ecom_order_payments", "order_id"), ("ecom_order_reviews", "order_id"), ("ecom_products", "product_id"),
    ("ecom_sellers", "seller_id"),
)
for _table, _column in _KEY_NOT_NULL:
    _id = f"ECOM-NN-{_table.removeprefix('ecom_').upper()}-{_column.upper()}"
    MODULE_RULES[_id] = _rule(_id, f"{_table}.{_column} is not null", _table, "not_null", column=_column)
_KEY_UNIQUE = (
    ("ecom_customers", ["customer_id"]), ("ecom_orders", ["order_id"]), ("ecom_products", ["product_id"]),
    ("ecom_sellers", ["seller_id"]), ("ecom_order_items", ["order_id", "order_item_id"]),
    ("ecom_order_payments", ["order_id", "payment_sequential"]), ("ecom_category_translation", ["product_category_name"]),
)
for _table, _columns in _KEY_UNIQUE:
    _id = f"ECOM-UQ-{_table.removeprefix('ecom_').upper()}"
    MODULE_RULES[_id] = _rule(_id, f"{_table} key ({', '.join(_columns)}) is unique", _table, "unique", columns=list(_columns))
NOT_NULL_RULE_IDS = tuple(i for i in MODULE_RULES if i.startswith("ECOM-NN-"))
UNIQUE_RULE_IDS = tuple(i for i in MODULE_RULES if i.startswith("ECOM-UQ-"))

TIMESTAMP_COLUMNS = {
    "ecom_orders": ("order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
                    "order_delivered_customer_date", "order_estimated_delivery_date"),
    "ecom_order_items": ("shipping_limit_date",),
    "ecom_order_reviews": ("review_creation_date", "review_answer_timestamp"),
}


class _CatalogInspector:
    """Just enough of a SQLAlchemy inspector for ``_build_query``, backed by one INFORMATION_SCHEMA query."""

    def __init__(self, catalog: dict[str, set[str]]) -> None:
        self.catalog = catalog

    def get_columns(self, table: str, schema: str | None = None) -> list[dict[str, str]]:
        return [{"name": c} for c in sorted(self.catalog.get(table, ()))]


@dataclass
class Context:
    conn: GuardedConnection
    catalog: dict[str, set[str]]
    yaml_rules: dict[str, dict]
    rules_sha256: str
    now: str
    inspector: _CatalogInspector = field(init=False)
    cache: dict = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)  # filled by whichever check counts a table first

    def __post_init__(self) -> None:
        self.inspector = _CatalogInspector(self.catalog)

    @property
    def tables(self) -> set[str]:
        return set(self.catalog)


def open_context(conn: GuardedConnection, clock: Callable[[], datetime] | None = None) -> Context:
    try:
        config, digest = _read_config(DEFAULT_CONFIG_PATH)
    except DataQualityConfigError as exc:
        raise RuleConfigError("the configuration failed validation") from None
    catalog: dict[str, set[str]] = {}
    for row in conn.mapping_rows(CATALOG_SQL):
        catalog.setdefault(str(row["table_name"]), set()).add(str(row["column_name"]))
    now = (clock() if clock else datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return Context(conn, catalog, {r["id"]: r for r in config["rules"]}, digest, now)


@dataclass
class Outcome:
    rule: dict
    violations: int | None = None
    error: str | None = None
    pending: bool = False
    skipped: bool = False
    detail: dict[str, int] | None = None  # duplicate rules: rows_in_duplicate_groups, surplus_rows

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if self.error:
            return "error"
        if not self.violations:
            return "passed"
        if self.pending:
            return "pending_investigation"
        return "failed" if self.rule["classification"] == "integrity" else "flagged"

    def evidence(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "rule_result", "rule_id": self.rule["id"], "rule_name": self.rule.get("name", self.rule["id"]),
            "table": self.rule["table"], "rule_type": self.rule["type"], "classification": self.rule["classification"],
            "severity": SEVERITY_MAP[self.rule["severity"]], "unit": "groups" if self.rule["type"] == "unique" else "rows",
            "status": self.status, "violations": self.violations,
        }
        if self.detail:
            item.update(self.detail)
        if self.pending:
            item.update(pending_investigation=True, rule_enabled_in_configuration=False)
        if self.error:
            item["error_type"] = self.error
        if self.skipped:
            item["reason"] = "Rule is disabled in the configuration."
        return item


def _yaml_rule(ctx: Context, rule_id: str) -> dict:
    try:
        return ctx.yaml_rules[rule_id]
    except KeyError:
        raise RuleConfigError(f"Rule {rule_id} is missing from the rule file.") from None


def _rule_for(ctx: Context, rule_id: str) -> dict:
    return MODULE_RULES[rule_id] if rule_id in MODULE_RULES else _yaml_rule(ctx, rule_id)


def _unique_sql(ctx: Context, rule: dict) -> str:
    """Duplicate-key query for the Ecommerce module only (the shared builder is deliberately left alone).

    A row is a duplicate-group candidate only if EVERY key column is non-NULL, so NULL keys are never counted as duplicates
    (they are reported by the missing-values check instead). One statement returns three counts:
      duplicate_groups          distinct complete key values that occur more than once  (this is the rule's ``violations``)
      rows_in_duplicate_groups  rows that carry such a key value
      surplus_rows              rows beyond the first of each group (rows_in_duplicate_groups - duplicate_groups)
    """
    table, columns = rule["table"], rule["columns"]
    if len({c.lower() for c in columns}) != len(columns):
        raise SchemaMismatch("A candidate key lists the same column twice.")
    for column in columns:
        _validate_column(ctx.inspector, table, column, ctx.tables, ctx.cache)
    keys = ", ".join(qi(c) for c in columns)
    complete = " AND ".join(f"{qi(c)} IS NOT NULL" for c in columns)
    return (f"SELECT COUNT_BIG(*) AS duplicate_groups, COALESCE(SUM(occurrences), 0) AS rows_in_duplicate_groups FROM ("
            f"SELECT COUNT_BIG(*) AS occurrences FROM dbo.{qi(table)} WHERE {complete} "
            f"GROUP BY {keys} HAVING COUNT_BIG(*) > 1) AS duplicate_groups_by_key")


def _execute(ctx: Context, rule: dict, *, pending: bool = False) -> Outcome:
    if rule.get("enabled") is not True and not pending:
        return Outcome(rule, skipped=True)
    if rule["type"] == "unique":
        try:
            sql = _unique_sql(ctx, rule)
        except DataQualityConfigError as exc:
            raise SchemaMismatch(str(exc)) from None
        try:
            groups, rows = ctx.conn.first_row(sql)
            groups, rows = int(groups), int(rows)
            return Outcome(rule, violations=groups, pending=pending,
                           detail={"rows_in_duplicate_groups": rows, "surplus_rows": rows - groups})
        except GuardRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - only the exception type is kept
            return Outcome(rule, error=type(exc).__name__, pending=pending)
    try:
        sql, params = _build_query(rule, ctx.inspector, ctx.tables, ctx.cache)
    except DataQualityConfigError as exc:
        raise SchemaMismatch(str(exc)) from None
    try:
        return Outcome(rule, violations=int(ctx.conn.scalar(sql, params)), pending=pending)
    except GuardRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - only the exception type is kept; driver text can contain server details
        return Outcome(rule, error=type(exc).__name__, pending=pending)


def _assemble(name: str, params: dict, ctx: Context, outcomes: list[Outcome], *, subject: str, next_step: str,
              extra_evidence: list[dict] | None = None, extra_notes: list[str] | None = None) -> DiagnosticResult:
    ran = [o for o in outcomes if not o.skipped]
    failed = [o for o in ran if o.status == "failed"]
    flagged = [o for o in ran if o.status == "flagged"]
    pending = [o for o in ran if o.status == "pending_investigation"]
    errors = [o for o in ran if o.status == "error"]
    unit = lambda o: "group(s)" if o.rule["type"] == "unique" else "row(s)"  # noqa: E731

    notes = [COUNTS_ONLY_NOTE, CAUSE_NOTE] + list(extra_notes or [])
    if flagged:
        notes.append("Late delivery is a business signal (service level), not a data-integrity failure.")
    if pending:
        notes.append("Duplicate review_id groups are a pending investigation (rule OLIST-009 stays disabled); "
                     "they are not classified as corruption.")
    if any(o.status == "skipped" for o in outcomes):
        notes.append("Rules disabled in the configuration were not run.")
    evidence = [o.evidence() for o in outcomes] + list(extra_evidence or [])
    counts = {o.rule["id"]: o.violations for o in outcomes if o.violations is not None}
    observed = {"violations": counts,
                "integrity_violations_total": sum(o.violations for o in failed),
                "business_signal_total": sum(o.violations for o in flagged),
                "pending_investigation_total": sum(o.violations for o in pending),
                "rules_run": len(ran), "rules_with_errors": len(errors), "rules_sha256": ctx.rules_sha256}
    expected = {"integrity_violations_total": 0}
    if errors:  # keep the findings of the rules that ran, but say plainly which rules were NOT assessed
        notes.append("INCOMPLETE: " + ", ".join(o.rule["id"] for o in errors) + " could not be assessed. The counts above come "
                     "only from the rules that ran; an unassessed rule is not a pass.")
    observed["rules_not_assessed"] = [o.rule["id"] for o in errors]
    affected = sorted({f"table:{o.rule['table']}" for o in failed + flagged + pending})
    common = dict(evidence=evidence, affected_records=affected, observed_value=observed, expected_value=expected,
                  parameters=params, notes=notes, evaluated_at=ctx.now)

    def listing(items: list[Outcome]) -> str:
        return "; ".join(f"{o.rule['id']} {o.violations} {unit(o)}" for o in items)

    if failed:
        severity = worst_severity(*(SEVERITY_MAP[o.rule["severity"]] for o in failed))
        text = f"{len(failed)} of {len(ran)} {subject} check(s) found violations: {listing(failed)}."
        if flagged:
            text += f" Business signal(s): {listing(flagged)}."
        if errors:
            text += f" {len(errors)} check(s) could not run."
        return DiagnosticResult(name, STATUS_ISSUE, severity, text, error_code=ECOM_PARTIAL if errors else None,
                                recommended_next_step=next_step, **common)
    if flagged or pending:
        severity = worst_severity(*(SEVERITY_MAP[o.rule["severity"]] for o in flagged), *(["low"] if pending else []))
        parts = ([f"business signal(s): {listing(flagged)}"] if flagged else []) + (
            [f"pending investigation: {listing(pending)}"] if pending else [])
        text = f"No integrity violations in {subject} checks; " + "; ".join(parts) + "."
        if errors:
            text += f" {len(errors)} check(s) could not run."
        return DiagnosticResult(name, STATUS_WARNING, severity, text, error_code=ECOM_PARTIAL if errors else None,
                                recommended_next_step=next_step, **common)
    if errors:
        return DiagnosticResult(name, STATUS_ERROR, "unknown",
                                f"{len(errors)} of {len(ran)} {subject} check(s) could not run (database error, details withheld).",
                                error_code=ECOM_DB_ERROR, recommended_next_step=
                                "Re-run later; if it persists, check SQL Server availability and the read-only account.", **common)
    return DiagnosticResult(name, STATUS_OK, "info", f"All {len(ran)} {subject} check(s) found no violations.", **common)


def _run_rules(ctx: Context, rule_ids: tuple[str, ...] | list[str]) -> list[Outcome]:
    return [_execute(ctx, _rule_for(ctx, rid)) for rid in rule_ids]


# ------------------------------------------------------------------------------------------------ the checks
def check_ecommerce_target(ctx: Context) -> DiagnosticResult:
    counts: dict[str, int] = {}
    missing = [t for t in ECOMMERCE_TABLES if t not in ctx.catalog]
    for table in ECOMMERCE_TABLES:
        if table in ctx.catalog:
            if table not in ctx.row_counts:
                ctx.row_counts[table] = int(ctx.conn.scalar(f"SELECT COUNT_BIG(*) FROM dbo.{qi(table)}"))
            counts[table] = ctx.row_counts[table]
    verification = dict(ctx.conn.verification)
    evidence = [{"type": "target", "database": ECOMMERCE_DATABASE, "verification": verification,
                 "row_counts": counts, "missing_tables": missing}]
    observed = {"database": ECOMMERCE_DATABASE, "read_only_verified": bool(verification.get("read_only_verified")),
                "tables_found": len(counts), "tables_expected": len(ECOMMERCE_TABLES), "total_rows": sum(counts.values())}
    notes = [COUNTS_ONLY_NOTE, "The connection was verified before this query: database, Windows login (expected "
             "read-only account, no impersonation), effective permissions and no access to the simulation database."]
    common = dict(evidence=evidence, observed_value=observed, expected_value={"tables_found": len(ECOMMERCE_TABLES)},
                  parameters={}, notes=notes, evaluated_at=ctx.now)
    if missing:
        return DiagnosticResult(TARGET_NAME, STATUS_ISSUE, "high", f"{len(missing)} expected table(s) are missing: "
                                f"{', '.join(missing)}.", affected_records=[f"table:{t}" for t in missing],
                                error_code=ECOM_SCHEMA_MISMATCH, recommended_next_step=
                                "Confirm the Olist import completed; this tool does not change any schema.", **common)
    return DiagnosticResult(TARGET_NAME, STATUS_OK, "info", f"Connected to {ECOMMERCE_DATABASE} as the verified read-only "
                            f"account; all {len(ECOMMERCE_TABLES)} expected tables are present ({observed['total_rows']} rows in total).",
                            **common)


def check_referential_integrity(ctx: Context, relationship: str | None = None) -> DiagnosticResult:
    if relationship is not None and relationship not in RELATIONSHIPS:
        raise InvalidInput(f"relationship must be one of: {', '.join(RELATIONSHIPS)}.")
    ids = [RELATIONSHIPS[relationship]] if relationship else list(RELATIONSHIPS.values())
    return _assemble(INTEGRITY_NAME, {"relationship": relationship}, ctx, _run_rules(ctx, ids),
                     subject="referential-integrity",
                     next_step="Compare the affected tables' load dates and sources to find where the child rows lost "
                               "their parent rows; consider whether the extract was partial. This tool does not repair data.")


def check_delivery_completeness(ctx: Context) -> DiagnosticResult:
    return _assemble(COMPLETENESS_NAME, {}, ctx, _run_rules(ctx, COMPLETENESS_RULES), subject="delivery-completeness",
                     next_step="Review the delivered orders that lack a carrier or customer date in the source system; "
                               "a missing date may be a source gap rather than a loading fault.")


def check_delivery_timeline(ctx: Context) -> DiagnosticResult:
    return _assemble(TIMELINE_NAME, {}, ctx, _run_rules(ctx, TIMELINE_RULES), subject="delivery-timeline",
                     next_step="For delivery-before-purchase, check timestamp time zones and source ordering. Late delivery "
                               "is a service-level signal; segment it by period or seller before drawing conclusions.")


def _null_counts(ctx: Context, table: str) -> dict[str, Any]:
    columns = sorted(ctx.catalog[table])
    select = ", ".join(["COUNT_BIG(*) AS row_count"] + [f"COUNT_BIG({qi(c)}) AS nn_{i}" for i, c in enumerate(columns)])
    row = ctx.conn.first_row(f"SELECT {select} FROM dbo.{qi(table)}")
    total = int(row[0])
    ctx.row_counts[table] = total
    nulls = {c: total - int(row[i + 1]) for i, c in enumerate(columns)}
    return {"type": "null_counts", "table": table, "row_count": total, "columns_checked": len(columns),
            "columns_with_nulls": {c: n for c, n in nulls.items() if n > 0}}


def check_missing_values(ctx: Context, table: str | None = None) -> DiagnosticResult:
    if table is not None and table not in ECOMMERCE_TABLES:
        raise InvalidInput(f"table must be one of: {', '.join(ECOMMERCE_TABLES)}.")
    if table is not None and table not in ctx.catalog:
        raise SchemaMismatch(f"Unknown table: {table}")
    rule_ids = [REVIEW_NOT_NULL_RULE] + list(NOT_NULL_RULE_IDS)
    outcomes = [o for o in _run_rules(ctx, rule_ids) if table is None or o.rule["table"] == table]
    targets = [t for t in ECOMMERCE_TABLES if t in ctx.catalog and (table is None or t == table)]
    observations = [_null_counts(ctx, t) for t in targets]
    return _assemble(MISSING_NAME, {"table": table}, ctx, outcomes, subject="key-column null", extra_evidence=observations,
                     extra_notes=["Null counts of optional columns (for example delivery dates of orders that are not "
                                  "delivered, or review comments) are observations, not violations; see check_delivery_completeness."],
                     next_step="Trace the affected key columns to the source extract; a null key prevents joins to the parent table.")


def check_duplicate_keys(ctx: Context) -> DiagnosticResult:
    outcomes = _run_rules(ctx, UNIQUE_RULE_IDS)
    outcomes.append(_execute(ctx, _yaml_rule(ctx, REVIEW_UNIQUE_RULE), pending=True))
    return _assemble(DUPLICATES_NAME, {}, ctx, outcomes, subject="candidate-key uniqueness",
                     next_step="For key duplicates, compare the duplicate groups' source rows to decide whether they are "
                               "replays or conflicting records. review_id duplicates remain a pending investigation.")


def profile_ecommerce_timestamps(ctx: Context) -> DiagnosticResult:
    profiles: list[dict[str, Any]] = []
    for table, columns in TIMESTAMP_COLUMNS.items():
        if table not in ctx.catalog:
            raise SchemaMismatch(f"Unknown table: {table}")
        for column in columns:
            if column not in ctx.catalog[table]:
                raise SchemaMismatch(f"Unknown column {column!r} in table {table!r}.")
            low, high = ctx.conn.first_row(f"SELECT MIN({qi(column)}) AS min_v, MAX({qi(column)}) AS max_v FROM dbo.{qi(table)}")
            iso = lambda v: v.isoformat() if isinstance(v, (datetime, date)) else None  # noqa: E731
            profiles.append({"type": "timestamp_range", "table": table, "column": column, "min": iso(low), "max": iso(high)})
    return DiagnosticResult(
        TIMESTAMPS_NAME, STATUS_OK, "info", f"Profiled the date range of {len(profiles)} timestamp column(s).",
        evidence=profiles, observed_value={"columns_profiled": len(profiles)}, parameters={},
        notes=[COUNTS_ONLY_NOTE, "Aggregate minimum and maximum dates only. This dataset is a historical extract with no "
               "pipeline or load metadata, so no freshness verdict is made."], evaluated_at=ctx.now)
