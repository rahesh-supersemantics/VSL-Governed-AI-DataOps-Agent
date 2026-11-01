"""Ecommerce diagnostics: semantics against a SQLite oracle, error handling, counts-only output."""

from __future__ import annotations

import json

import pytest
import yaml

from app.ecommerce import diagnostics as dx
from app.ecommerce.guard import GuardRefusal
from app.ecommerce.service import EcommerceDiagnosticsService
from app.ecommerce.guard import IDENTITY_SQL
from tests.ecommerce_support import (
    DIRTY, SECRET_VALUES, SqliteRaw, make_clean_db, make_db, sqlite_connect,
)


def svc(db=None, **kw):
    return EcommerceDiagnosticsService(connect=sqlite_connect(db if db is not None else make_db(), **kw))


def by_rule(result):
    return {e["rule_id"]: e for e in result.to_dict()["evidence"] if e["type"] == "rule_result"}


def counts(result):
    return {k: e.get("violations") for k, e in by_rule(result).items()}


def test_integrity_counts_match_hand_derived_expectations():
    r = svc().check_referential_integrity()
    assert (r.status, r.severity) == ("issue_detected", "critical")
    assert counts(r) == {k: DIRTY[k] for k in ("OLIST-001", "OLIST-002", "OLIST-003", "OLIST-004",
                                               "ECOM-FK-PAYMENTS-ORDERS", "ECOM-FK-REVIEWS-ORDERS")}


@pytest.mark.parametrize("relationship,rule", [("orders_customers", "OLIST-001"), ("items_orders", "OLIST-002"),
                                                ("items_products", "OLIST-003"), ("items_sellers", "OLIST-004"),
                                                ("payments_orders", "ECOM-FK-PAYMENTS-ORDERS"),
                                                ("reviews_orders", "ECOM-FK-REVIEWS-ORDERS")])
def test_each_relationship_can_be_checked_alone(relationship, rule):
    r = svc().check_referential_integrity(relationship)
    assert list(by_rule(r)) == [rule] and counts(r)[rule] == 1


def test_unknown_relationship_is_a_safe_error():
    r = svc().check_referential_integrity("orders_everything")
    assert r.status == "error" and r.error_code == "DIAG-INVALID-INPUT"


def test_null_foreign_keys_are_not_orphans():
    # order 5 has a NULL customer_id: counted by the not-null check, never as an orphan
    assert counts(svc().check_referential_integrity("orders_customers")) == {"OLIST-001": 1}
    nn = by_rule(svc().check_missing_values("ecom_orders"))["ECOM-NN-ORDERS-CUSTOMER_ID"]
    assert nn["violations"] == 1


def test_delivery_completeness_and_timeline():
    c = svc().check_delivery_completeness()
    assert (c.status, counts(c)) == ("issue_detected", {"OLIST-005": 1, "OLIST-006": 1})
    t = svc().check_delivery_timeline()
    got = by_rule(t)
    assert got["OLIST-007"]["violations"] == 1 and got["OLIST-007"]["classification"] == "integrity"
    assert got["OLIST-008"]["violations"] == 2 and got["OLIST-008"]["classification"] == "business_signal"


def test_late_delivery_alone_is_a_low_severity_signal_not_an_integrity_failure():
    db = make_clean_db()
    db.execute("update ecom_orders set order_delivered_customer_date='2020-02-01'")  # later than the estimate
    r = svc(db).check_delivery_timeline()
    assert r.status == "warning" and r.severity == "low" and counts(r)["OLIST-008"] == 1 and counts(r)["OLIST-007"] == 0


def test_missing_values_and_pending_review_uniqueness():
    m = svc().check_missing_values("ecom_order_reviews")
    assert by_rule(m)["OLIST-010"]["violations"] == DIRTY["OLIST-010"]
    d = svc().check_duplicate_keys().to_dict()
    rules = by_rule(svc().check_duplicate_keys())
    assert rules["OLIST-009"]["status"] == "pending_investigation"
    assert rules["OLIST-009"]["violations"] == DIRTY["OLIST-009"] == 2  # NULL review ids are not a duplicate group
    assert rules["OLIST-009"]["rows_in_duplicate_groups"] == 4 and rules["OLIST-009"]["surplus_rows"] == 2
    assert any("pending investigation" in note for note in d["notes"])


def test_review_id_uniqueness_alone_never_fails_the_check_and_stays_disabled_in_the_yaml():
    db = make_clean_db()
    db.execute("insert into ecom_order_reviews(review_id, order_id) values('REV-SECRET-1','ORD-SECRET-01')")
    r = svc(db).check_duplicate_keys()
    assert r.status == "warning" and r.severity == "low"
    config = yaml.safe_load(open(dx.DEFAULT_CONFIG_PATH, encoding="utf-8"))
    rule = next(x for x in config["rules"] if x["id"] == "OLIST-009")
    assert rule["enabled"] is False  # the shared YAML is not changed and the rule is not enabled


def test_clean_database_is_all_ok():
    s = svc(make_clean_db())
    for call in (s.check_referential_integrity, s.check_delivery_completeness, s.check_delivery_timeline):
        assert call().status == "ok"
    assert counts(s.check_referential_integrity()) == {k: 0 for k in
                                                       ("OLIST-001", "OLIST-002", "OLIST-003", "OLIST-004",
                                                        "ECOM-FK-PAYMENTS-ORDERS", "ECOM-FK-REVIEWS-ORDERS")}


def test_empty_tables_do_not_fail():
    s = svc(make_db(dirty=False))
    for call in (s.check_referential_integrity, s.check_delivery_completeness, s.check_delivery_timeline,
                 s.check_missing_values, s.check_duplicate_keys, s.profile_ecommerce_timestamps):
        result = call()
        assert result.status == "ok", (result.diagnostic_name, result.summary)


def test_timestamp_profile_is_min_max_only():
    r = svc().profile_ecommerce_timestamps()
    assert r.status == "ok"
    blob = json.dumps(r.to_dict()).lower()
    assert "stale" not in blob and "is fresh" not in blob
    ranges = [e for e in r.to_dict()["evidence"] if e["type"] == "timestamp_range"]
    assert ranges and all(set(e) == {"type", "table", "column", "min", "max"} for e in ranges)


def test_results_never_contain_identifiers_or_text_values():
    s = svc()
    results = [s.check_ecommerce_target(), s.check_referential_integrity(), s.check_delivery_completeness(),
               s.check_delivery_timeline(), s.check_missing_values(), s.check_duplicate_keys(), s.profile_ecommerce_timestamps()]
    results.append(s.run_all())
    blob = json.dumps([r.to_dict() for r in results])
    for secret in SECRET_VALUES + ["hunter2", "dq_reader", "FIXTUREPC"]:
        assert secret not in blob
    for r in results[:-1]:
        assert all(a.startswith("table:") for a in r.to_dict()["affected_records"])


def test_run_all_runs_seven_checks_on_one_verified_connection():
    record = []
    report = EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record)).run_all()
    assert len(record) == 1  # one connection, verified once
    d = report.to_dict()
    assert [r["diagnostic_name"] for r in d["results"]] == list(dx.CHECK_NAMES)
    assert d["overall_status"] == "issue_detected" and d["ecommerce_summary"]["execution"] == "complete"


def test_every_call_opens_and_verifies_a_new_connection():
    record = []
    s = EcommerceDiagnosticsService(connect=sqlite_connect(make_db(), record=record))
    s.check_delivery_completeness()
    s.check_delivery_completeness()
    assert len(record) == 2 and record[0] is not record[1]
    assert all(r.closed for r in record)  # each connection was closed when its call finished
    assert [r.executed.count(IDENTITY_SQL) for r in record] == [1, 1]  # and each was verified on its own


# ------------------------------------------------------------------------------------------- failure modes
def test_a_guard_refusal_is_an_error_result_not_a_fallback():
    r = svc(identity={"database_name": "dataops_sim", "effective_login": "sa", "original_login": "sa", "machine_name": "X"}
            ).check_referential_integrity()
    assert r.status == "error"
    assert r.error_code == "ECOM-GUARD-REFUSED"


def test_wrong_permissions_stop_every_tool():
    from tests.ecommerce_support import permission_row
    s = svc(permissions=permission_row(can_insert=1))
    for call in (s.check_ecommerce_target, s.check_referential_integrity, s.check_delivery_completeness,
                 s.check_delivery_timeline, s.check_missing_values, s.check_duplicate_keys, s.profile_ecommerce_timestamps):
        r = call()
        assert r.status == "error" and r.error_code == "ECOM-GUARD-REFUSED"


def test_sql_errors_become_sanitised_error_results():
    r = svc(fail_on="ecom_orders").check_delivery_completeness()
    assert r.status == "error" and r.error_code == "ECOM-DB-ERROR"
    blob = json.dumps(r.to_dict())
    assert "SECRET-HOST" not in blob and "hunter2" not in blob


def test_unexpected_schema_is_reported_not_guessed():
    db = make_db()
    db.execute("drop table ecom_order_reviews")
    r = svc(db).check_referential_integrity("reviews_orders")
    assert r.status == "error" and r.error_code == "ECOM-SCHEMA-MISMATCH"


def test_a_missing_column_is_a_schema_error():
    db = make_db()
    db.execute("alter table ecom_orders drop column order_status")
    r = svc(db).check_delivery_completeness()
    assert r.status == "error" and r.error_code == "ECOM-SCHEMA-MISMATCH"


def test_malformed_rule_configuration_is_reported(monkeypatch):
    def broken(path):
        raise dx.DataQualityConfigError("bad config SECRET")

    monkeypatch.setattr(dx, "_read_config", broken)
    r = svc().check_delivery_completeness()
    assert r.status == "error" and r.error_code == "ECOM-RULE-CONFIG" and "SECRET" not in json.dumps(r.to_dict())


def test_run_all_with_a_failing_check_is_partial_not_clean():
    report = svc(fail_on="ecom_order_reviews").run_all().to_dict()
    blob = json.dumps(report)
    results = {r["diagnostic_name"]: r for r in report["results"]}
    assert report["overall_status"] == "error"
    assert results["check_ecommerce_target"]["error_code"] == "DIAG-DB-ERROR"          # its table count failed
    assert results["check_referential_integrity"]["status"] == "issue_detected"        # real findings are kept...
    assert results["check_referential_integrity"]["error_code"] == "ECOM-PARTIAL"      # ...and flagged as partial
    assert report["ecommerce_summary"]["execution"] == "incomplete"
    assert report["ecommerce_summary"]["rules"]["unassessed"] > 0


def test_module_rules_are_valid_and_do_not_touch_the_yaml_rules():
    ids = list(dx.MODULE_RULES)
    assert len(ids) == len(set(ids)) == 19
    yaml_ids = {r["id"] for r in yaml.safe_load(open(dx.DEFAULT_CONFIG_PATH, encoding="utf-8"))["rules"]}
    assert not (set(ids) & yaml_ids)
    assert "ECOM-FK-PAYMENTS-ORDERS" in ids and "ECOM-FK-REVIEWS-ORDERS" in ids


# =============================================================================== duplicate-key semantics (Stage 4.1)
def dup_rule(result, rule_id):
    return by_rule(result)[rule_id]


def reviews_db(rows):
    db = make_db(dirty=False)
    db.executemany("insert into ecom_order_reviews(review_id, order_id) values(?,?)", rows)
    return db


def items_db(rows):
    db = make_db(dirty=False)
    db.executemany("insert into ecom_order_items(order_id, order_item_id) values(?,?)", rows)
    return db


def test_dup_1_the_same_non_null_key_twice_is_one_duplicate_group():
    r = dup_rule(svc(reviews_db([("R1", "o1"), ("R1", "o2"), ("R2", "o3")])).check_duplicate_keys(), "OLIST-009")
    assert (r["violations"], r["rows_in_duplicate_groups"], r["surplus_rows"]) == (1, 2, 1)


def test_dup_2_null_keys_in_a_nullable_single_column_key_are_not_a_duplicate_group():
    r = dup_rule(svc(reviews_db([(None, "o1"), (None, "o2"), (None, "o3"), ("R1", "o4")])).check_duplicate_keys(), "OLIST-009")
    assert (r["violations"], r["rows_in_duplicate_groups"], r["surplus_rows"]) == (0, 0, 0)
    assert r["status"] == "passed"  # nothing pending: no complete key repeats


def test_dup_3_composite_keys_with_a_null_component_are_excluded():
    rows = [("o1", None), ("o1", None), (None, 1), (None, 1), (None, None), (None, None)]
    r = dup_rule(svc(items_db(rows)).check_duplicate_keys(), "ECOM-UQ-ORDER_ITEMS")
    assert (r["violations"], r["rows_in_duplicate_groups"]) == (0, 0)
    assert r["status"] == "passed"


def test_dup_3b_a_complete_composite_key_repeated_is_still_found_next_to_null_ones():
    rows = [("o1", None), ("o1", None), ("o2", 1), ("o2", 1)]
    r = dup_rule(svc(items_db(rows)).check_duplicate_keys(), "ECOM-UQ-ORDER_ITEMS")
    assert (r["violations"], r["rows_in_duplicate_groups"], r["surplus_rows"]) == (1, 2, 1)


def test_dup_4_distinct_complete_composite_keys_are_not_duplicates():
    rows = [("o1", 1), ("o1", 2), ("o2", 1), ("o2", 2)]
    r = dup_rule(svc(items_db(rows)).check_duplicate_keys(), "ECOM-UQ-ORDER_ITEMS")
    assert (r["violations"], r["rows_in_duplicate_groups"]) == (0, 0) and r["status"] == "passed"


def test_dup_5_null_keys_stay_visible_in_the_missing_values_check():
    s = svc(items_db([("o1", None), (None, 1), (None, 1)]))
    assert dup_rule(s.check_duplicate_keys(), "ECOM-UQ-ORDER_ITEMS")["violations"] == 0
    missing = by_rule(s.check_missing_values("ecom_order_items"))
    assert missing["ECOM-NN-ORDER_ITEMS-ORDER_ID"]["violations"] == 2  # two rows have a NULL order_id
    nulls = next(e for e in s.check_missing_values("ecom_order_items").to_dict()["evidence"] if e["type"] == "null_counts")
    assert nulls["columns_with_nulls"]["order_item_id"] == 1
    reviews = svc(reviews_db([(None, "o1"), (None, "o2")]))
    assert by_rule(reviews.check_missing_values("ecom_order_reviews"))["OLIST-010"]["violations"] == 2
    assert dup_rule(reviews.check_duplicate_keys(), "OLIST-009")["violations"] == 0


def test_dup_6_groups_and_rows_are_different_quantities():
    r = dup_rule(svc(reviews_db([("R1", "a"), ("R1", "b"), ("R1", "c"), ("R2", "d"), ("R2", "e")])).check_duplicate_keys(),
                 "OLIST-009")
    assert r["violations"] == 2                  # the rule's count: duplicate GROUPS (distinct repeated keys)
    assert r["rows_in_duplicate_groups"] == 5    # every row that carries a repeated key
    assert r["surplus_rows"] == 3                # rows beyond the first of each group (5 - 2)
    assert r["unit"] == "groups"


def test_duplicate_summary_text_says_groups():
    result = svc(items_db([("o1", 1), ("o1", 1)])).check_duplicate_keys()
    assert "group(s)" in result.summary and result.status == "issue_detected"


def test_pending_review_id_rule_with_real_duplicates_is_pending_not_failed_and_not_enabled():
    result = svc(reviews_db([("R1", "a"), ("R1", "b")])).check_duplicate_keys()
    rule = dup_rule(result, "OLIST-009")
    assert rule["status"] == "pending_investigation" and rule["rule_enabled_in_configuration"] is False
    assert result.status == "warning" and result.severity == "low"


def test_the_shared_engine_builder_is_unchanged_and_the_unique_sql_passes_the_gate():
    from app.diagnostics import data_quality_engine as engine
    from app.ecommerce.guard import check_statement

    sql, _ = engine._build_query({"type": "unique", "table": "ecom_order_reviews", "columns": ["review_id"], "id": "X"},
                                 dx._CatalogInspector({"ecom_order_reviews": {"review_id"}}), {"ecom_order_reviews"})
    assert "IS NOT NULL" not in sql  # the shared builder still groups NULLs: it is deliberately not touched
    ctx_sql = dx._unique_sql(type("C", (), {"inspector": dx._CatalogInspector({"ecom_order_items": {"order_id", "order_item_id"}}),
                                            "tables": {"ecom_order_items"}, "cache": {}})(),
                             dx.MODULE_RULES["ECOM-UQ-ORDER_ITEMS"])
    check_statement(ctx_sql)
    assert "[order_id] IS NOT NULL AND [order_item_id] IS NOT NULL" in ctx_sql
