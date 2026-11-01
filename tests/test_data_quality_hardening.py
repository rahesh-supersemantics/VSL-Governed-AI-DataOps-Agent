"""Stage 2 hardening: strict config schema, pre-flight, read-only verification, statuses and report integrity.

Every test here covers a defect reproduced during the Stage 2 audit. No database, no network."""

from __future__ import annotations

import copy
import datetime
import json
import os

import pytest
import yaml

from app.db import readonly_preflight as pre
from app.diagnostics import data_quality_engine as dq
from app.diagnostics import report_io
from app.diagnostics.data_quality_engine import DataQualityConfigError, TARGET_DB, load_rules
from tests.dq_support import (
    LOGIN,
    FakeConnection,
    FakeInspector,
    FakeSettings,
    basic_rule,
    install_fake_run,
    read_only_row,
    write_rules,
)

COND = {"column": "order_status", "operator": "equals", "value": "delivered"}


def cond_rule(rule_id="C-1", **cond_overrides):
    condition = {**COND, **cond_overrides}
    return {"id": rule_id, "type": "conditional_not_null", "table": "ecom_orders",
            "column": "order_delivered_customer_date", "severity": "high", "enabled": True, "condition": condition}


def load(tmp_path, rules, **top):
    return load_rules(write_rules(tmp_path, rules, **top))


# ------------------------------------------------------------------------------------------ strict configuration
def test_valid_minimal_config_loads_and_defaults_are_applied(tmp_path):
    config = load(tmp_path, [basic_rule("OK-1")])
    assert config["execution"] == {"mode": "read_only", "stop_on_error": False}
    assert config["rules"][0]["classification"] == "integrity"


@pytest.mark.parametrize("bad", ["true", "yes", 1, None])
def test_enabled_must_be_a_real_boolean(tmp_path, bad):
    with pytest.raises(DataQualityConfigError, match="enabled"):
        load(tmp_path, [basic_rule("EN-1", enabled=bad)])


def test_missing_enabled_is_rejected_not_silently_disabled(tmp_path):
    rule = basic_rule("EN-2")
    del rule["enabled"]
    with pytest.raises(DataQualityConfigError, match="missing required key"):
        load(tmp_path, [rule])


def test_misspelt_enabled_key_is_rejected(tmp_path):
    rule = basic_rule("EN-3")
    del rule["enabled"]
    rule["enabeld"] = True
    with pytest.raises(DataQualityConfigError, match="unknown or unsupported key"):
        load(tmp_path, [rule])


def test_unknown_rule_key_is_rejected_including_sql(tmp_path):
    with pytest.raises(DataQualityConfigError, match="unknown or unsupported key"):
        load(tmp_path, [basic_rule("UK-1", sql="DROP TABLE ecom_orders")])


def test_unknown_top_level_key_is_rejected(tmp_path):
    with pytest.raises(DataQualityConfigError, match="unknown or unsupported key"):
        load(tmp_path, [basic_rule("UK-2")], connection_string="x")


def test_version_is_required_and_must_be_1(tmp_path):
    with pytest.raises(DataQualityConfigError, match="version"):
        load(tmp_path, [basic_rule("V-1")], version=2)


@pytest.mark.parametrize("mode", ["read_write", "write", "", None, 1])
def test_execution_mode_must_be_read_only(tmp_path, mode):
    with pytest.raises(DataQualityConfigError, match="execution.mode"):
        load(tmp_path, [basic_rule("M-1")], execution={"mode": mode})


@pytest.mark.parametrize("value", ["false", 0, None, "yes"])
def test_stop_on_error_must_be_boolean(tmp_path, value):
    with pytest.raises(DataQualityConfigError, match="stop_on_error"):
        load(tmp_path, [basic_rule("S-1")], execution={"mode": "read_only", "stop_on_error": value})


def test_unknown_execution_key_is_rejected(tmp_path):
    with pytest.raises(DataQualityConfigError, match="unknown or unsupported key"):
        load(tmp_path, [basic_rule("S-2")], execution={"mode": "read_only", "timeout": 5})


def test_condition_without_value_is_rejected(tmp_path):
    rule = cond_rule()
    del rule["condition"]["value"]
    with pytest.raises(DataQualityConfigError, match="missing required key"):
        load(tmp_path, [rule])


@pytest.mark.parametrize("value", [None, ["a", "b"], {"x": 1}, datetime.date(2024, 1, 1), ""])
def test_condition_value_must_be_a_non_empty_scalar(tmp_path, value):
    with pytest.raises(DataQualityConfigError, match="scalar"):
        load(tmp_path, [cond_rule(value=value)])


@pytest.mark.parametrize("value", ["delivered", 3, 1.5, True])
def test_scalar_condition_values_are_accepted(tmp_path, value):
    assert load(tmp_path, [cond_rule(value=value)])["rules"][0]["condition"]["value"] == value


@pytest.mark.parametrize("rule", [
    {"id": "X-1", "type": "not_null", "column": "order_id"},
    {"id": "X-2", "type": "unique", "columns": ["order_id"]},
    {"id": "X-3", "type": "foreign_key", "column": "customer_id", "references": {"table": "ecom_customers", "column": "customer_id"}},
])
def test_condition_is_rejected_on_unsupported_rule_types(tmp_path, rule):
    rule = {**rule, "table": "ecom_orders", "severity": "high", "enabled": True, "condition": dict(COND)}
    with pytest.raises(DataQualityConfigError, match="'condition' is not supported"):
        load(tmp_path, [rule])


def test_conditional_not_null_requires_a_condition(tmp_path):
    rule = cond_rule()
    del rule["condition"]
    with pytest.raises(DataQualityConfigError, match="missing required key"):
        load(tmp_path, [rule])


def test_unsupported_condition_operator_is_rejected(tmp_path):
    with pytest.raises(DataQualityConfigError, match="condition operator"):
        load(tmp_path, [cond_rule(operator="like")])


@pytest.mark.parametrize("column", ["", " ", " order_id", "order_id ", None, 5, "a" * 129, "bad\nname"])
def test_empty_or_malformed_column_names_are_rejected(tmp_path, column):
    with pytest.raises(DataQualityConfigError):
        load(tmp_path, [basic_rule("COL-1", column=column)])


@pytest.mark.parametrize("columns", [[], ["order_id", "order_id"], ["order_id", "ORDER_ID"], ["order_id", ""], "order_id"])
def test_unique_columns_must_be_a_non_empty_list_without_duplicates(tmp_path, columns):
    rule = {"id": "U-1", "type": "unique", "table": "ecom_orders", "columns": columns, "severity": "high", "enabled": True}
    with pytest.raises(DataQualityConfigError):
        load(tmp_path, [rule])


def test_column_comparison_columns_must_differ(tmp_path):
    rule = {"id": "CC-1", "type": "column_comparison", "table": "ecom_orders", "left_column": "order_id",
            "right_column": "ORDER_ID", "operator": "greater_than_or_equal", "severity": "high", "enabled": True}
    with pytest.raises(DataQualityConfigError, match="must differ"):
        load(tmp_path, [rule])


@pytest.mark.parametrize("rule_id", ["", "has space", "semi;colon", "x" * 65, "../x", 5])
def test_rule_ids_are_validated(tmp_path, rule_id):
    with pytest.raises(DataQualityConfigError):
        load(tmp_path, [basic_rule(rule_id)])


def test_empty_rule_list_is_rejected(tmp_path):
    with pytest.raises(DataQualityConfigError, match="between 1 and"):
        load(tmp_path, [])


def test_invalid_classification_is_rejected(tmp_path):
    with pytest.raises(DataQualityConfigError, match="classification"):
        load(tmp_path, [basic_rule("CL-1", classification="whatever")])


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        "version: 1\ndatabase: dataops_ecommerce_demo\nrules:\n  - id: D-1\n    type: not_null\n    table: ecom_orders\n"
        "    column: order_id\n    severity: high\n    enabled: false\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(DataQualityConfigError, match="not valid YAML.*duplicate"):
        load_rules(path)


def test_config_loading_errors_are_wrapped_and_safe(tmp_path):
    secret_dir = tmp_path / "very-private-folder-name"
    with pytest.raises(DataQualityConfigError) as missing:
        load_rules(secret_dir / "missing.yaml")
    assert "very-private-folder-name" not in str(missing.value)

    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(DataQualityConfigError, match="not valid YAML"):
        load_rules(bad)

    binary = tmp_path / "bin.yaml"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(DataQualityConfigError, match="UTF-8"):
        load_rules(binary)

    huge = tmp_path / "huge.yaml"
    huge.write_text("#" * (dq.MAX_CONFIG_BYTES + 1), encoding="utf-8")
    with pytest.raises(DataQualityConfigError, match="too large"):
        load_rules(huge)

    for content in ("- a\n- b\n", "just a string", ""):
        path = tmp_path / "root.yaml"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(DataQualityConfigError, match="root must be a mapping"):
            load_rules(path)


def test_bom_prefixed_config_still_loads(tmp_path):
    path = write_rules(tmp_path, [basic_rule("BOM-1")])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert load_rules(path)["rules"][0]["id"] == "BOM-1"


def test_hostile_names_never_reach_sql(tmp_path):
    inspector, tables = FakeInspector(), set(FakeInspector.columns)
    with pytest.raises(DataQualityConfigError, match="Unknown column"):
        dq._build_query(basic_rule("H-1", column="x]; DROP TABLE t;--"), inspector, tables)
    with pytest.raises(DataQualityConfigError, match="Unknown table"):
        dq._build_query(basic_rule("H-2", **{"table": "ecom_orders]; DROP"}), inspector, tables)


def test_build_query_rejects_value_less_condition_even_if_validation_is_bypassed():
    rule = cond_rule()
    del rule["condition"]["value"]
    with pytest.raises(DataQualityConfigError, match="scalar"):
        dq._build_query(rule, FakeInspector(), set(FakeInspector.columns))


def test_build_query_rejects_unsupported_condition_and_missing_condition_even_if_validation_is_bypassed():
    inspector, tables = FakeInspector(), set(FakeInspector.columns)
    with pytest.raises(DataQualityConfigError, match="not supported"):
        dq._build_query(basic_rule("B-1", condition=dict(COND)), inspector, tables)
    no_condition = cond_rule()
    del no_condition["condition"]
    with pytest.raises(DataQualityConfigError, match="requires a condition"):
        dq._build_query(no_condition, inspector, tables)


def test_catalog_lookups_are_cached_per_table():
    class CountingInspector(FakeInspector):
        calls = 0

        def get_columns(self, table, schema=None):
            type(self).calls += 1
            return super().get_columns(table, schema)

    rule = {"id": "Q-1", "type": "unique", "table": "ecom_order_items", "severity": "high", "enabled": True,
            "columns": ["order_id", "order_item_id"]}
    dq._build_query(rule, CountingInspector(), set(CountingInspector.columns), {})
    assert CountingInspector.calls == 1


# ------------------------------------------------------------------------------------------------- pre-flight
def test_preflight_validates_every_rule_before_any_rule_query(tmp_path, monkeypatch):
    path = write_rules(tmp_path, [
        basic_rule("GOOD-1"),
        basic_rule("BAD-1", column="no_such_column"),
        basic_rule("BAD-2", **{"table": "no_such_table"}),
        basic_rule("OFF-1", enabled=False, column="also_missing"),
    ])
    connection = FakeConnection()
    install_fake_run(monkeypatch, dq, connection)

    report = dq.run_data_quality(path, tmp_path / "r.json")

    assert report["status"] == "invalid_configuration"
    assert report["verdict"] == "not_assessed"
    assert {e["rule_id"] for e in report["config_errors"]} == {"BAD-1", "BAD-2"}  # aggregated; disabled rule not checked
    assert connection.rule_statements() == []  # not even the valid rule ran
    statuses = {r["id"]: r["status"] for r in report["results"]}
    assert statuses == {"GOOD-1": "not_run", "BAD-1": "not_run", "BAD-2": "not_run", "OFF-1": "skipped"}
    assert report["summary"]["reconciled"] is True


# ------------------------------------------------------------------------------ read-only verification (fail closed)
def run_with(monkeypatch, tmp_path, connection, rules=None, settings=None, **kwargs):
    path = write_rules(tmp_path, rules or [basic_rule("RO-1")])
    install_fake_run(monkeypatch, dq, connection, settings)
    return dq.run_data_quality(path, tmp_path / "ro.json", **kwargs)


def test_verified_read_only_principal_runs_and_report_never_contains_the_login(tmp_path, monkeypatch):
    connection = FakeConnection()
    report = run_with(monkeypatch, tmp_path, connection)
    assert report["read_only_verified"] is True
    assert report["read_only_verification"]["status"] == "verified"
    assert report["read_only_verification"]["principal_fingerprint"]
    assert report["target"] == {"expected_database": TARGET_DB, "connected_database": TARGET_DB, "server_is_local": True}
    text = (tmp_path / "ro.json").read_text(encoding="utf-8")
    assert LOGIN not in text and "dq_reader_fixture" not in text
    assert connection.only_selects()
    order = [s.strip().split("\n")[0][:30] for s, _ in connection.executed]
    assert order[0] == "SELECT DB_NAME()" and "SELECT" in order[1]  # database verified before the permission check


@pytest.mark.parametrize("flag", pre.WRITE_CAPABLE_FLAGS)
def test_every_write_capable_flag_fails_closed(tmp_path, monkeypatch, flag):
    connection = FakeConnection(permission_row=read_only_row(**{flag: 1}))
    report = run_with(monkeypatch, tmp_path, connection)
    assert report["status"] == "aborted"
    assert report["fatal_error_kind"] == "read_only_not_verified"
    assert report["read_only_verified"] is False
    assert report["read_only_verification"]["status"] == "write_capable"
    assert f"write_capable:{flag}" in report["read_only_verification"]["reasons"]
    assert connection.rule_statements() == []
    assert report["summary"]["not_run"] == 1 and report["verdict"] == "not_assessed"


def test_writable_tables_fail_closed(tmp_path, monkeypatch):
    report = run_with(monkeypatch, tmp_path, FakeConnection(permission_row=read_only_row(writable_table_count=2)))
    assert report["read_only_verification"]["status"] == "write_capable"
    assert "write_capable:writable_tables" in report["read_only_verification"]["reasons"]


@pytest.mark.parametrize("field,value", [
    ("is_db_owner", None), ("can_insert", None), ("can_delete", "0"), ("is_sysadmin", True), ("can_execute", 2),
    ("writable_table_count", None), ("writable_table_count", -1), ("table_count", None), ("table_count", 0),
    ("login_name", None), ("login_name", "  "),
])
def test_unknown_or_malformed_permission_values_are_inconclusive_and_fail_closed(tmp_path, monkeypatch, field, value):
    connection = FakeConnection(permission_row=read_only_row(**{field: value}))
    report = run_with(monkeypatch, tmp_path, connection)
    assert report["read_only_verified"] is False
    assert report["read_only_verification"]["status"] == "inconclusive"
    assert report["status"] == "aborted"
    assert connection.rule_statements() == []


def test_missing_permission_row_or_failing_permission_query_is_inconclusive_without_driver_text(tmp_path, monkeypatch):
    for kwargs in ({"permission_row": None},
                   {"permission_error": RuntimeError("Login failed for user 'secret_user' on server 'secret_server'")}):
        connection = FakeConnection(**kwargs)
        report = run_with(monkeypatch, tmp_path, connection)
        assert report["status"] == "aborted" and report["read_only_verified"] is False
        assert report["read_only_verification"]["status"] == "inconclusive"
        assert connection.rule_statements() == []
        dumped = json.dumps(report)
        assert "secret_user" not in dumped and "secret_server" not in dumped


def test_permission_row_for_a_different_database_fails_closed(tmp_path, monkeypatch):
    connection = FakeConnection(permission_row=read_only_row(database_name="dataops_sim"))
    report = run_with(monkeypatch, tmp_path, connection)
    assert report["read_only_verification"]["status"] == "wrong_database"
    assert report["status"] == "aborted" and connection.rule_statements() == []


def test_access_to_dataops_sim_is_recorded_but_is_not_a_verdict(tmp_path, monkeypatch):
    report = run_with(monkeypatch, tmp_path, FakeConnection(permission_row=read_only_row(can_access_dataops_sim=1)))
    assert report["read_only_verified"] is True
    assert report["read_only_verification"]["dataops_sim_accessible"] == 1


def test_override_runs_but_never_claims_verification(tmp_path, monkeypatch):
    connection = FakeConnection(permission_row=read_only_row(is_sysadmin=1))
    report = run_with(monkeypatch, tmp_path, connection, allow_unverified_read_only=True)
    assert report["status"] == "completed"
    assert report["read_only_verified"] is False
    assert report["read_only_verification"]["status"] == "write_capable"
    assert any("NOT verified" in w for w in report["warnings"])
    assert connection.only_selects()


def test_override_does_not_bypass_the_database_check(tmp_path, monkeypatch):
    connection = FakeConnection(database="dataops_sim")
    report = run_with(monkeypatch, tmp_path, connection, allow_unverified_read_only=True)
    assert report["status"] == "aborted" and report["fatal_error_kind"] == "wrong_database"
    assert connection.rule_statements() == []


@pytest.mark.parametrize("server", ["prod-sql.example.com", "10.1.2.3\\INSTANCE", "tcp:db.internal,1433", "", None])
def test_non_local_or_unknown_servers_are_refused_before_any_connection(tmp_path, monkeypatch, server):
    settings = type("S", (), {"db_server": server})()
    path = write_rules(tmp_path, [basic_rule("L-1")])
    monkeypatch.setattr(dq, "get_settings", lambda: settings)

    def forbidden(*_a, **_k):
        pytest.fail("make_engine must not be called for a non-local server")

    monkeypatch.setattr(dq, "make_engine", forbidden)
    report = dq.run_data_quality(path, tmp_path / "l.json")
    assert report["status"] == "aborted" and report["fatal_error_kind"] == "unsafe_target"
    assert report["target"]["server_is_local"] is None
    assert server is None or str(server) not in report["fatal_error"] or server == ""


@pytest.mark.parametrize("server", ["localhost", "localhost\\SQLEXPRESS03", ".", "(local)", "127.0.0.1"])
def test_local_servers_are_accepted(server):
    pre.assert_local_target(type("S", (), {"db_server": server})())


def test_make_engine_is_called_for_the_target_database_with_the_non_admin_identity(tmp_path, monkeypatch):
    calls = []
    connection = FakeConnection()
    install_fake_run(monkeypatch, dq, connection)
    monkeypatch.setattr(dq, "make_engine", lambda settings, **kw: (calls.append(kw), __import__("tests.dq_support", fromlist=["x"]).FakeEngine(connection))[1])
    dq.run_data_quality(write_rules(tmp_path, [basic_rule("T-1")]), tmp_path / "t.json")
    assert calls == [{"database": TARGET_DB, "admin": False}]


def test_unexpected_exception_is_recorded_without_leaking_driver_text(tmp_path, monkeypatch):
    path = write_rules(tmp_path, [basic_rule("E-1")])
    monkeypatch.setattr(dq, "get_settings", lambda: FakeSettings())

    def boom(*_a, **_k):
        raise RuntimeError("password=hunter2 Server=secret_host")

    monkeypatch.setattr(dq, "make_engine", boom)
    report = dq.run_data_quality(path, tmp_path / "e.json")
    assert report["status"] == "aborted" and report["fatal_error_kind"] == "unexpected_error"
    assert "hunter2" not in json.dumps(report)


# ------------------------------------------------------------------------------ statuses, severity, verdict, counts
def classify_rules():
    return [
        basic_rule("PASS-1", column="order_id"),
        basic_rule("INTEG-1", column="customer_id", severity="critical"),
        {"id": "BIZ-1", "type": "column_comparison", "table": "ecom_orders", "severity": "warning",
         "left_column": "order_delivered_customer_date", "right_column": "order_estimated_delivery_date",
         "operator": "less_than_or_equal", "classification": "business_signal", "enabled": True, "condition": dict(COND)},
        basic_rule("OFF-1", enabled=False),
    ]


def counts(sql, params):
    if "[customer_id] IS NULL" in sql:
        return 4
    if "<=" in sql:
        return 7826
    return 0


def test_business_signals_are_flagged_not_failed_and_counts_reconcile(tmp_path, monkeypatch):
    connection = FakeConnection(rule_handler=counts)
    report = run_with(monkeypatch, tmp_path, connection, rules=classify_rules())
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["BIZ-1"]["status"] == "flagged" and by_id["BIZ-1"]["violations"] == 7826
    assert by_id["INTEG-1"]["status"] == "failed"
    summary = report["summary"]
    assert (summary["passed"], summary["failed"], summary["flagged"], summary["errors"], summary["not_run"]) == (1, 1, 1, 0, 0)
    assert summary["failed_by_severity"] == {"critical": 1}
    assert summary["flagged_by_severity"] == {"warning": 1}
    assert summary["reconciled"] is True
    assert summary["enabled_rules"] + summary["disabled_rules"] == summary["total_rules"] == len(report["results"])
    assert report["verdict"] == "fail"


def test_only_business_signals_gives_pass_with_flags(tmp_path, monkeypatch):
    rules = [r for r in classify_rules() if r["id"] in {"PASS-1", "BIZ-1"}]
    report = run_with(monkeypatch, tmp_path, FakeConnection(rule_handler=counts), rules=rules)
    assert report["status"] == "completed" and report["verdict"] == "pass_with_flags"


def test_clean_run_is_pass_and_errors_make_the_verdict_incomplete(tmp_path, monkeypatch):
    report = run_with(monkeypatch, tmp_path, FakeConnection(), rules=[basic_rule("OK-1")])
    assert report["status"] == "completed" and report["verdict"] == "pass"

    def handler(sql, params):
        raise RuntimeError("boom")

    report = run_with(monkeypatch, tmp_path, FakeConnection(rule_handler=handler), rules=[basic_rule("ERR-1")])
    assert report["status"] == "completed_with_errors" and report["verdict"] == "incomplete"
    assert report["results"][0]["status"] == "error" and "violations" not in report["results"][0]


def test_integrity_failure_outranks_errors_in_the_verdict(tmp_path, monkeypatch):
    def handler(sql, params):
        if "[customer_id] IS NULL" in sql:
            return 1
        raise RuntimeError("boom")

    rules = [basic_rule("A-1", column="order_id"), basic_rule("B-1", column="customer_id")]
    report = run_with(monkeypatch, tmp_path, FakeConnection(rule_handler=handler), rules=rules)
    assert report["verdict"] == "fail" and report["summary"]["errors"] == 1


def test_stop_on_error_stops_and_marks_remaining_rules_not_run(tmp_path, monkeypatch):
    def handler(sql, params):
        raise RuntimeError("boom")

    path = write_rules(tmp_path, [basic_rule("S-1"), basic_rule("S-2", column="customer_id"), basic_rule("S-3", enabled=False)],
                       execution={"mode": "read_only", "stop_on_error": True})
    connection = FakeConnection(rule_handler=handler)
    install_fake_run(monkeypatch, dq, connection)
    report = dq.run_data_quality(path, tmp_path / "s.json")
    assert [r["status"] for r in report["results"]] == ["error", "not_run", "skipped"]
    assert "stop_on_error" in report["results"][1]["reason"]
    assert len(connection.rule_statements()) == 1
    assert report["status"] == "completed_with_errors" and report["summary"]["reconciled"] is True


def test_without_stop_on_error_every_rule_is_attempted(tmp_path, monkeypatch):
    connection = FakeConnection(rule_handler=lambda s, p: (_ for _ in ()).throw(RuntimeError("boom")))
    report = run_with(monkeypatch, tmp_path, connection, rules=[basic_rule("N-1"), basic_rule("N-2", column="customer_id")])
    assert [r["status"] for r in report["results"]] == ["error", "error"]


def test_unique_rule_reports_its_unit_as_groups(tmp_path, monkeypatch):
    rule = {"id": "UQ-1", "type": "unique", "table": "ecom_order_reviews", "columns": ["review_id"], "severity": "warning",
            "enabled": True}
    report = run_with(monkeypatch, tmp_path, FakeConnection(rule_handler=lambda s, p: 789), rules=[rule, basic_rule("RW-1")])
    assert report["results"][0]["unit"] == "groups" and report["results"][1]["unit"] == "rows"


def test_real_rules_file_classifies_late_delivery_as_a_business_signal():
    config = load_rules(dq.DEFAULT_CONFIG_PATH)
    by_id = {r["id"]: r for r in config["rules"]}
    assert by_id["OLIST-008"]["classification"] == "business_signal"
    assert by_id["OLIST-005"]["classification"] == "integrity"
    assert by_id["OLIST-009"]["enabled"] is False


# ---------------------------------------------------------------------------------- report integrity / provenance
def test_report_has_run_metadata_hashes_and_no_values(tmp_path, monkeypatch):
    rules = [cond_rule("MD-1", value="very-sensitive-literal")]
    path = write_rules(tmp_path, rules)
    install_fake_run(monkeypatch, dq, FakeConnection(rule_handler=lambda s, p: 1))
    report = dq.run_data_quality(path, tmp_path / "md.json")
    on_disk = json.loads((tmp_path / "md.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(report))
    assert len(report["run_id"]) == 32 and report["schema_version"] == 2
    assert report["config"]["file_name"] == "rules.yaml" and str(tmp_path) not in json.dumps(report)
    import hashlib
    assert report["config"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    item = report["results"][0]
    assert len(item["sql_sha256"]) == 64 and item["elapsed_ms"] >= 0
    assert report["started_at_utc"] <= report["finished_at_utc"] and report["duration_seconds"] >= 0
    assert "very-sensitive-literal" not in json.dumps(report)
    assert "read_only" not in report  # the old hard-coded claim is gone


def test_each_run_gets_a_new_run_id(tmp_path, monkeypatch):
    first = run_with(monkeypatch, tmp_path, FakeConnection())
    second = run_with(monkeypatch, tmp_path, FakeConnection())
    assert first["run_id"] != second["run_id"]


def test_previous_report_is_preserved_and_new_one_replaces_it(tmp_path, monkeypatch):
    path = write_rules(tmp_path, [basic_rule("P-1")])
    report_path = tmp_path / "keep" / "report.json"
    install_fake_run(monkeypatch, dq, FakeConnection())
    first = dq.run_data_quality(path, report_path)
    second = dq.run_data_quality(path, report_path)
    copies = [p for p in report_path.parent.glob("report.*.json")]
    assert len(copies) == 1
    assert json.loads(copies[0].read_text(encoding="utf-8"))["run_id"] == first["run_id"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["run_id"] == second["run_id"]


def test_atomic_write_failure_leaves_original_intact_and_no_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "r.json"
    target.write_text('{"keep": true}', encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        report_io.write_json_atomic(target, {"new": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_unserialisable_payload_touches_nothing(tmp_path):
    target = tmp_path / "r.json"
    target.write_text('{"keep": true}', encoding="utf-8")
    with pytest.raises(TypeError):
        report_io.write_json_atomic(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]


def test_default_paths_are_anchored_to_the_repository_not_the_working_directory():
    assert dq.DEFAULT_CONFIG_PATH.is_absolute() and dq.DEFAULT_CONFIG_PATH.name == "data_quality_rules.yaml"
    assert dq.DEFAULT_CONFIG_PATH.is_file()
    assert dq.DEFAULT_REPORT_PATH.is_absolute() and dq.DEFAULT_REPORT_PATH.parent.name == "reports"


def test_all_statements_in_a_full_run_are_selects(tmp_path, monkeypatch):
    connection = FakeConnection(rule_handler=counts)
    run_with(monkeypatch, tmp_path, connection, rules=classify_rules())
    assert connection.executed and connection.only_selects()


# ------------------------------------------------------------------------------------------- Stage 3 regressions
def test_non_finite_condition_values_are_rejected(tmp_path):
    for bad in (float("nan"), float("inf"), float("-inf")):
        rule = {"id": "A-1", "type": "conditional_not_null", "table": "ecom_orders", "column": "order_id",
                "severity": "high", "enabled": True,
                "condition": {"column": "order_status", "operator": "equals", "value": bad}}
        with pytest.raises(DataQualityConfigError, match="finite"):
            load_rules(write_rules(tmp_path, [rule]))


def test_a_run_with_no_enabled_rules_is_not_assessed_rather_than_pass(tmp_path, monkeypatch):
    connection = FakeConnection()
    install_fake_run(monkeypatch, dq, connection)
    report = dq.run_data_quality(write_rules(tmp_path, [basic_rule("A-1", enabled=False)]), tmp_path / "r.json")
    assert report["status"] == "completed"
    assert report["summary"]["enabled_rules"] == 0
    assert report["verdict"] == "not_assessed"
    assert connection.rule_statements() == []


def test_access_to_dataops_sim_adds_a_warning_but_not_a_verdict_change(tmp_path, monkeypatch):
    connection = FakeConnection(permission_row=read_only_row(can_access_dataops_sim=1))
    install_fake_run(monkeypatch, dq, connection)
    report = dq.run_data_quality(write_rules(tmp_path, [basic_rule("A-1")]), tmp_path / "r.json")
    assert report["read_only_verified"] is True
    assert report["verdict"] == "pass"
    assert any("dataops_sim" in warning for warning in report["warnings"])


def test_no_warning_when_dataops_sim_is_inaccessible_or_unknown(tmp_path, monkeypatch):
    for value in (0, None):
        connection = FakeConnection(permission_row=read_only_row(can_access_dataops_sim=value))
        install_fake_run(monkeypatch, dq, connection)
        report = dq.run_data_quality(write_rules(tmp_path, [basic_rule("A-1")]), tmp_path / "r.json")
        assert report["warnings"] == []
