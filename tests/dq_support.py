"""In-memory fakes for the data-quality engine and profiler tests. No database, no network, nothing is written
outside ``tmp_path``. ``FakeConnection`` records every statement so tests can prove only SELECTs were issued."""

from __future__ import annotations

import re
from typing import Any, Callable

import yaml

from app.db.readonly_preflight import WRITE_CAPABLE_FLAGS

TARGET_DB = "dataops_ecommerce_demo"
LOGIN = "CORP\\dq_reader_fixture"


class FakeSettings:
    db_server = "localhost\\SQLEXPRESS"


def read_only_row(**overrides: Any) -> dict:
    """A permission row for a principal with no write/DDL capability."""
    row: dict[str, Any] = {"database_name": TARGET_DB, "login_name": LOGIN,
                           "writable_table_count": 0, "table_count": 6, "can_access_dataops_sim": 0}
    row.update({name: 0 for name in WRITE_CAPABLE_FLAGS})
    row.update(overrides)
    return row


class FakeInspector:
    """A small, predictable schema without a database connection."""

    columns = {
        "ecom_orders": {"order_id", "customer_id", "order_status", "order_purchase_timestamp",
                        "order_delivered_customer_date", "order_delivered_carrier_date",
                        "order_estimated_delivery_date"},
        "ecom_customers": {"customer_id"},
        "ecom_order_items": {"order_id", "order_item_id", "product_id", "seller_id"},
        "ecom_products": {"product_id"},
        "ecom_sellers": {"seller_id"},
        "ecom_order_reviews": {"review_id", "order_id"},
    }

    def get_columns(self, table, schema=None):
        if schema != "dbo" or table not in self.columns:
            raise AssertionError(f"Unexpected schema lookup: {schema}.{table}")
        return [{"name": name} for name in sorted(self.columns[table])]

    def get_table_names(self, schema=None):
        assert schema == "dbo"
        return list(self.columns)


class FakeResult:
    def __init__(self, value: Any = None, row: Any = None) -> None:
        self.value, self.row = value, row

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def first(self):
        return self.row


class FakeConnection:
    def __init__(self, database: str = TARGET_DB, permission_row: Any = "default",
                 permission_error: Exception | None = None,
                 rule_handler: Callable[[str, dict | None], int] | None = None) -> None:
        self.database = database
        self.permission_row = read_only_row() if permission_row == "default" else permission_row
        self.permission_error = permission_error
        self.rule_handler = rule_handler or (lambda sql, params: 0)
        self.executed: list[tuple[str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "AS writable_table_count" in sql:
            if self.permission_error is not None:
                raise self.permission_error
            return FakeResult(row=self.permission_row)
        if sql.strip() == "SELECT DB_NAME()":
            return FakeResult(self.database)
        return FakeResult(self.rule_handler(sql, params))

    # helpers for assertions
    def rule_statements(self) -> list[str]:
        return [s for s, _ in self.executed if "COUNT_BIG" in s and "AS writable_table_count" not in s]

    def only_selects(self) -> bool:
        return all(re.match(r"\s*SELECT\b", s, re.I) for s, _ in self.executed)


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection, self.disposed = connection, False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def install_fake_run(monkeypatch, module, connection: FakeConnection, settings: Any = None) -> FakeEngine:
    """Point ``module`` (the engine or the profiler) at the fake connection."""
    engine = FakeEngine(connection)
    monkeypatch.setattr(module, "make_engine", lambda *a, **k: engine)
    monkeypatch.setattr(module, "get_settings", lambda: settings if settings is not None else FakeSettings())
    monkeypatch.setattr(module, "inspect", lambda _conn: FakeInspector())
    return engine


def basic_rule(rule_id: str, column: str = "order_id", enabled: bool = True, **extra: Any) -> dict:
    rule = {"id": rule_id, "type": "not_null", "table": "ecom_orders", "column": column,
            "severity": "high", "enabled": enabled}
    rule.update(extra)
    return rule


def write_rules(tmp_path, rules: list, **top: Any):
    config = {"version": 1, "database": TARGET_DB,
              "execution": {"mode": "read_only", "stop_on_error": False}, "rules": rules}
    config.update(top)
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path
