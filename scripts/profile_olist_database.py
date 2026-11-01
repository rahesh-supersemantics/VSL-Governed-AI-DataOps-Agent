"""Read-only profile of the Olist demo database (``dataops_ecommerce_demo``).

    python -m scripts.profile_olist_database [--allow-unverified-read-only] [--report-path PATH]

Safety (same model as ``app/diagnostics/data_quality_engine.py``):

* the server must be local and the connection must land in ``dataops_ecommerce_demo`` before any profiling query;
* the connected principal's effective permissions are checked first and the run stops if it can write or if the
  check is inconclusive (``--allow-unverified-read-only`` is a developer override and is recorded in the report);
* the report is always written, atomically; a failed run replaces a stale "completed" report, and the previous
  report is preserved as a timestamped copy;
* reports contain counts and hashes only, never record values, login names or raw driver errors.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

from app.config import get_settings
from app.db.connection import make_engine, safe_error_summary
from app.db.readonly_preflight import assert_local_target, verify_read_only_principal
from app.diagnostics.report_io import write_json_atomic

TARGET_DB = "dataops_ecommerce_demo"
REPORT_SCHEMA_VERSION = 2
DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "olist_data_profile.json"

# Candidate keys: only columns expected to identify a row uniquely.
KEYS = {
    "ecom_customers": ["customer_id"],
    "ecom_orders": ["order_id"],
    "ecom_order_items": ["order_id", "order_item_id"],
    "ecom_order_payments": ["order_id", "payment_sequential"],
    "ecom_order_reviews": ["review_id"],
    "ecom_products": ["product_id"],
    "ecom_sellers": ["seller_id"],
    "ecom_category_translation": ["product_category_name"],
}

RELATIONSHIPS = [
    ("orders_without_customer", "ecom_orders", "customer_id", "ecom_customers", "customer_id"),
    ("items_without_order", "ecom_order_items", "order_id", "ecom_orders", "order_id"),
    ("items_without_product", "ecom_order_items", "product_id", "ecom_products", "product_id"),
    ("items_without_seller", "ecom_order_items", "seller_id", "ecom_sellers", "seller_id"),
    ("payments_without_order", "ecom_order_payments", "order_id", "ecom_orders", "order_id"),
    ("reviews_without_order", "ecom_order_reviews", "order_id", "ecom_orders", "order_id"),
]

BUSINESS_SQL = {
    "delivered_missing_customer_date": """
        SELECT COUNT_BIG(*) FROM dbo.ecom_orders
        WHERE order_status = 'delivered'
          AND order_delivered_customer_date IS NULL
    """,
    "delivered_missing_carrier_date": """
        SELECT COUNT_BIG(*) FROM dbo.ecom_orders
        WHERE order_status = 'delivered'
          AND order_delivered_carrier_date IS NULL
    """,
    "delivery_before_purchase": """
        SELECT COUNT_BIG(*) FROM dbo.ecom_orders
        WHERE order_delivered_customer_date
              < order_purchase_timestamp
    """,
    "delivered_after_estimated_date": """
        SELECT COUNT_BIG(*) FROM dbo.ecom_orders
        WHERE order_status = 'delivered'
          AND order_delivered_customer_date
              > order_estimated_delivery_date
    """,
}


def qi(identifier):
    """Safely quote a SQL Server identifier."""
    return "[" + identifier.replace("]", "]]") + "]"


class ProfileAborted(Exception):
    """The profile cannot continue safely (message is safe to show)."""


def _profile(conn, report: dict, allow_unverified: bool) -> None:
    actual_db = conn.execute(text("SELECT DB_NAME()")).scalar_one()
    report["target"]["connected_database"] = actual_db
    if not isinstance(actual_db, str) or actual_db.lower() != TARGET_DB.lower():
        raise ProfileAborted(f"Wrong database: connected to {actual_db}")

    verification = verify_read_only_principal(conn, TARGET_DB)
    report["read_only_verification"] = verification
    report["read_only_verified"] = bool(verification["verified"])
    if not verification["verified"]:
        if not allow_unverified:
            raise ProfileAborted(
                f"Read-only access could not be verified ({verification['status']}). "
                "Use a read-only login for dataops_ecommerce_demo; no profiling query was run.")
        report["warnings"].append(
            "Read-only access is NOT verified; the run continued only because --allow-unverified-read-only was set.")
        print("WARNING: read-only access is not verified (override in use).")

    inspector = inspect(conn)
    table_names = sorted(inspector.get_table_names(schema="dbo"))
    print(f"Connected database: {actual_db}")
    print(f"Tables discovered: {len(table_names)}")

    for table in table_names:
        columns = inspector.get_columns(table, schema="dbo")
        col_names = [c["name"] for c in columns]
        table_sql = f"dbo.{qi(table)}"

        row_count = conn.execute(text(f"SELECT COUNT_BIG(*) FROM {table_sql}")).scalar_one()

        null_counts = {}
        for col in col_names:
            count = conn.execute(
                text(f"SELECT COUNT_BIG(*) FROM {table_sql} WHERE {qi(col)} IS NULL")
            ).scalar_one()
            if count:
                null_counts[col] = int(count)

        table_report = {
            "row_count": int(row_count),
            "columns": [
                {"name": c["name"], "type": str(c["type"]), "nullable": bool(c["nullable"])}
                for c in columns
            ],
            "null_counts": null_counts,
        }

        if table in KEYS:
            keys = KEYS[table]
            if all(k in col_names for k in keys):
                key_sql = ", ".join(qi(k) for k in keys)
                duplicate_count = conn.execute(
                    text(
                        "SELECT COUNT_BIG(*) FROM ("
                        f"SELECT {key_sql} FROM {table_sql} "
                        f"GROUP BY {key_sql} HAVING COUNT_BIG(*) > 1"
                        ") AS duplicates"
                    )
                ).scalar_one()
                table_report["candidate_key"] = keys
                table_report["duplicate_key_groups"] = int(duplicate_count)

        report["tables"][table] = table_report
        print(f"  {table}: {int(row_count):,} rows, {len(null_counts)} columns with NULLs")

    available = set(table_names)

    for name, child, child_col, parent, parent_col in RELATIONSHIPS:
        if child not in available or parent not in available:
            report["skipped_checks"].append({"check": name, "reason": "A required table is missing."})
            continue
        query = text(
            f"SELECT COUNT_BIG(*) FROM dbo.{qi(child)} AS c "
            f"WHERE c.{qi(child_col)} IS NOT NULL "
            f"AND NOT EXISTS ("
            f"SELECT 1 FROM dbo.{qi(parent)} AS p "
            f"WHERE p.{qi(parent_col)} = c.{qi(child_col)})"
        )
        value = conn.execute(query).scalar_one()
        report["relationships"][name] = int(value)
        print(f"  Relationship {name}: {int(value):,} unmatched rows")

    if "ecom_orders" in available:
        for name, sql in BUSINESS_SQL.items():
            value = conn.execute(text(sql)).scalar_one()
            report["business_checks"][name] = int(value)
            print(f"  Business check {name}: {int(value):,}")
    else:
        for name in BUSINESS_SQL:
            report["skipped_checks"].append({"check": name, "reason": "A required table is missing."})


def run_profile(report_path: Path | str = DEFAULT_REPORT_PATH, *, allow_unverified_read_only: bool = False) -> tuple[int, dict]:
    """Profile the database and always write a report. Returns ``(exit_code, report)``."""
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    report: dict = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "database": TARGET_DB,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": None,
        "duration_seconds": None,
        "status": "failed",
        "read_only_verified": False,
        "read_only_verification": None,
        "warnings": [],
        "target": {"expected_database": TARGET_DB, "connected_database": None, "server_is_local": None},
        "tables": {},
        "relationships": {},
        "business_checks": {},
        "skipped_checks": [],
        "errors": [],
    }

    engine = None
    try:
        settings = get_settings()
        assert_local_target(settings)
        report["target"]["server_is_local"] = True
        engine = make_engine(settings, database=TARGET_DB, admin=False)
        with engine.connect() as conn:
            _profile(conn, report, allow_unverified_read_only)
        report["status"] = "completed"
    except ProfileAborted as exc:
        report["errors"].append(str(exc))
    except Exception as exc:  # noqa: BLE001 - the report records a redacted one-line summary, never the raw error
        report["errors"].append(f"{type(exc).__name__}: {safe_error_summary(exc)}")
    finally:
        if engine is not None:
            engine.dispose()

    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = round(time.perf_counter() - clock, 3)
    archived = write_json_atomic(report_path, report)

    if report["status"] == "completed":
        print(f"\nReport saved to: {Path(report_path).name}")
        print("Profiling completed. No database write statements were issued by this script.")
        if archived is not None:
            print(f"Previous report preserved as: {archived.name}")
        return 0, report
    print(f"PROFILE FAILED: {report['errors'][-1] if report['errors'] else 'unknown error'}")
    print(f"A failed-run report replaced any older report at: {Path(report_path).name}")
    return 1, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-unverified-read-only", action="store_true",
                        help="Developer override: continue when read-only access cannot be verified (recorded in the report).")
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH), help="Where to write the JSON report.")
    args = parser.parse_args(argv)
    code, _report = run_profile(args.report_path, allow_unverified_read_only=args.allow_unverified_read_only)
    return code


if __name__ == "__main__":
    sys.exit(main())
