from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.diagnostics import data_quality_engine as dq
from app.diagnostics.data_quality_engine import (
    DataQualityConfigError,
    TARGET_DB,
    _build_query,
    load_rules,
    qi,
)
from tests.dq_support import (
    FakeConnection,
    FakeInspector,
    basic_rule,
    install_fake_run,
    write_rules,
)


@pytest.fixture
def inspector():
    return FakeInspector()


@pytest.fixture
def tables(inspector):
    return set(inspector.columns)


def test_real_rules_configuration_loads():
    config_path = Path(__file__).resolve().parents[1] / "config" / "data_quality_rules.yaml"
    config = load_rules(config_path)

    assert config["database"] == TARGET_DB
    assert len(config["rules"]) == 10
    assert sum(rule["enabled"] is True for rule in config["rules"]) == 9


def test_wrong_database_is_rejected(tmp_path):
    config = {
        "version": 1,
        "database": "dataops_sim",
        "rules": [],
    }
    path = tmp_path / "wrong_database.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DataQualityConfigError, match="Only database"):
        load_rules(path)


def test_duplicate_rule_ids_are_rejected(tmp_path):
    rule = {
        "id": "TEST-001",
        "type": "not_null",
        "table": "ecom_orders",
        "column": "order_id",
        "severity": "high",
        "enabled": True,
    }
    config = {
        "version": 1,
        "database": TARGET_DB,
        "rules": [rule, copy.deepcopy(rule)],
    }
    path = tmp_path / "duplicate_ids.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DataQualityConfigError, match="Duplicate rule id"):
        load_rules(path)


def test_unsupported_rule_type_is_rejected(tmp_path):
    config = {
        "version": 1,
        "database": TARGET_DB,
        "rules": [{
            "id": "TEST-002",
            "type": "delete_rows",
            "table": "ecom_orders",
            "severity": "high",
            "enabled": True,
        }],
    }
    path = tmp_path / "unsupported_type.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(DataQualityConfigError, match="unsupported rule type"):
        load_rules(path)


def test_identifier_quoting_escapes_closing_bracket():
    assert qi("order]id") == "[order]]id]"


def test_unknown_column_is_rejected(inspector, tables):
    rule = {
        "id": "TEST-003",
        "type": "not_null",
        "table": "ecom_orders",
        "column": "unknown_column",
        "severity": "high",
    }

    with pytest.raises(DataQualityConfigError, match="Unknown column"):
        _build_query(rule, inspector, tables)


def test_foreign_key_rule_builds_read_only_count_query(inspector, tables):
    rule = {
        "id": "TEST-004",
        "type": "foreign_key",
        "table": "ecom_orders",
        "column": "customer_id",
        "references": {
            "table": "ecom_customers",
            "column": "customer_id",
        },
        "severity": "critical",
    }

    sql, params = _build_query(rule, inspector, tables)

    assert sql.lstrip().upper().startswith("SELECT COUNT_BIG")
    assert "NOT EXISTS" in sql
    assert "INSERT " not in sql.upper()
    assert "UPDATE " not in sql.upper()
    assert "DELETE " not in sql.upper()
    assert params == {}


def test_conditional_not_null_uses_bound_parameter(inspector, tables):
    rule = {
        "id": "TEST-005",
        "type": "conditional_not_null",
        "table": "ecom_orders",
        "column": "order_delivered_customer_date",
        "condition": {
            "column": "order_status",
            "operator": "equals",
            "value": "delivered",
        },
        "severity": "high",
    }

    sql, params = _build_query(rule, inspector, tables)

    assert "IS NULL" in sql
    assert ":condition_value" in sql
    assert params == {"condition_value": "delivered"}


def test_column_comparison_uses_supported_operator(inspector, tables):
    rule = {
        "id": "TEST-006",
        "type": "column_comparison",
        "table": "ecom_orders",
        "left_column": "order_delivered_customer_date",
        "right_column": "order_purchase_timestamp",
        "operator": "greater_than_or_equal",
        "severity": "high",
    }

    sql, params = _build_query(rule, inspector, tables)

    assert " >= " in sql
    assert sql.lstrip().upper().startswith("SELECT COUNT_BIG")
    assert params == {}


def test_unique_rule_groups_by_configured_columns(inspector, tables):
    rule = {
        "id": "TEST-007",
        "type": "unique",
        "table": "ecom_order_reviews",
        "columns": ["review_id"],
        "severity": "warning",
    }

    sql, params = _build_query(rule, inspector, tables)

    assert "GROUP BY [review_id]" in sql
    assert "HAVING COUNT_BIG(*) > 1" in sql
    assert params == {}


def test_not_null_rule_builds_count_query(inspector, tables):
    rule = {
        "id": "TEST-008",
        "type": "not_null",
        "table": "ecom_orders",
        "column": "order_id",
        "severity": "high",
    }

    sql, params = _build_query(rule, inspector, tables)

    assert "WHERE t.[order_id] IS NULL" in sql
    assert params == {}


def test_run_writes_report_and_counts_pass_fail_skip(tmp_path, monkeypatch):
    config_path = write_rules(tmp_path, [
        basic_rule("PASS-001"),
        basic_rule("FAIL-001", column="customer_id"),
        basic_rule("SKIP-001", enabled=False),
    ])
    report_path = tmp_path / "reports" / "report.json"
    connection = FakeConnection(rule_handler=lambda sql, params: 3 if "[customer_id] IS NULL" in sql else 0)
    engine = install_fake_run(monkeypatch, dq, connection)

    report = dq.run_data_quality(config_path, report_path)

    assert report["status"] == "completed"
    assert report["read_only_verified"] is True
    summary = report["summary"]
    assert (summary["total_rules"], summary["enabled_rules"], summary["disabled_rules"]) == (3, 2, 1)
    assert (summary["passed"], summary["failed"], summary["errors"], summary["not_run"]) == (1, 1, 0, 0)
    assert [r["status"] for r in report["results"]] == ["passed", "failed", "skipped"]
    assert report_path.exists()
    assert yaml.safe_load(config_path.read_text())["database"] == TARGET_DB
    assert engine.disposed is True


def test_rule_error_is_recorded_and_next_rule_runs(tmp_path, monkeypatch):
    config_path = write_rules(tmp_path, [
        basic_rule("ERROR-001", column="order_id"),
        basic_rule("PASS-002", column="customer_id"),
    ])

    def handler(sql, params):
        if "[order_id] IS NULL" in sql:
            raise RuntimeError("Simulated rule query failure")
        return 0

    connection = FakeConnection(rule_handler=handler)
    engine = install_fake_run(monkeypatch, dq, connection)

    report = dq.run_data_quality(config_path, tmp_path / "report.json")

    assert report["status"] == "completed_with_errors"
    assert report["summary"]["errors"] == 1
    assert report["summary"]["passed"] == 1
    assert [r["status"] for r in report["results"]] == ["error", "passed"]
    assert len(connection.rule_statements()) == 2
    assert engine.disposed is True


def test_wrong_database_marks_report_aborted_and_runs_no_rule_query(tmp_path, monkeypatch):
    config_path = write_rules(tmp_path, [basic_rule("TEST-DB-001")])
    connection = FakeConnection(database="dataops_sim")
    engine = install_fake_run(monkeypatch, dq, connection)

    report = dq.run_data_quality(config_path, tmp_path / "wrong-db-report.json")

    assert report["status"] == "aborted"
    assert report["fatal_error_kind"] == "wrong_database"
    assert "fatal_error" in report
    assert report["summary"]["passed"] == 0 and report["summary"]["failed"] == 0
    assert connection.rule_statements() == []
    assert [s for s, _ in connection.executed] == ["SELECT DB_NAME()"]
    assert engine.disposed is True
    assert (tmp_path / "wrong-db-report.json").exists()


def test_invalid_config_fails_before_engine_creation(tmp_path, monkeypatch):
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text("this: [is: not valid yaml", encoding="utf-8")

    def forbidden_engine(*_args, **_kwargs):
        pytest.fail("Engine must not be created for invalid YAML")

    monkeypatch.setattr(dq, "make_engine", forbidden_engine)

    with pytest.raises(DataQualityConfigError, match="not valid YAML"):
        dq.run_data_quality(config_path, tmp_path / "report.json")
