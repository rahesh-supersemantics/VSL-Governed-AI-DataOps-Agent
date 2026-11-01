"""Rule semantics for ``config/data_quality_rules.yaml``, checked without SQL Server.

The SQL the engine generates for every shipped rule is executed on a small synthetic SQLite dataset (SQL Server
specifics ``COUNT_BIG``, ``dbo.`` and the case-insensitive collation are translated) and compared with counts
worked out by hand. This pins NULL handling, boundaries, multiple violations and the empty-table case. It does not
prove SQL Server behaviour; the live baseline (profiler cross-check) covers that.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.diagnostics import data_quality_engine as engine

SCHEMA = """
create table ecom_customers(customer_id text);
create table ecom_orders(order_id text, customer_id text, order_status text, order_purchase_timestamp text,
  order_delivered_carrier_date text, order_delivered_customer_date text, order_estimated_delivery_date text);
create table ecom_products(product_id text);
create table ecom_sellers(seller_id text);
create table ecom_order_items(order_id text, product_id text, seller_id text);
create table ecom_order_reviews(review_id text, order_id text);
"""

# id, customer, status, purchase, carrier, customer date, estimated
ORDERS = [
    ("o1", "c1", "delivered", "2020-01-02", "2020-01-03", "2020-01-05", "2020-01-10"),  # clean
    ("o2", "c1", "delivered", "2020-01-02", None, "2020-01-05", "2020-01-10"),          # OLIST-006
    ("o3", "c2", "delivered", "2020-01-02", "2020-01-03", None, "2020-01-10"),          # OLIST-005
    ("o4", "c9", "delivered", "2020-01-02", "2020-01-03", "2020-01-20", "2020-01-10"),  # OLIST-001, late
    ("o5", None, "shipped", "2020-01-02", None, None, "2020-01-10"),                    # NULL customer ignored
    ("o6", "c1", "delivered", "2020-01-09", "2020-01-09", "2020-01-05", "2020-01-10"),  # OLIST-007
    ("o7", "c1", "canceled", "2020-01-02", None, "2020-01-30", "2020-01-10"),           # late but not delivered
    ("o8", "c1", "DELIVERED", "2020-01-02", "2020-01-03", "2020-01-20", "2020-01-10"),  # case-insensitive, late
    ("o9", "c1", "delivered", "2020-01-02", "2020-01-03", "2020-01-10", "2020-01-10"),  # exactly on time
    ("o10", "c1", "delivered", "2020-01-02", "2020-01-03", "2020-01-05", None),         # NULL estimate ignored
]
ITEMS = [("o1", "p1", "s1"), ("oX", "p1", "s1"), ("o1", "pX", "s1"), ("o1", "p1", "sX"), ("o1", None, None)]
REVIEWS = [("r1", "o1"), ("r1", "o2"), ("r2", "o1"), (None, "o1"), (None, "o2"), ("r3", "o1"), ("r3", "o1")]

EXPECTED = {
    "OLIST-001": 1, "OLIST-002": 1, "OLIST-003": 1, "OLIST-004": 1,
    "OLIST-005": 1, "OLIST-006": 1, "OLIST-007": 1,
    "OLIST-008": 2,   # o4 and o8; o7 (not delivered), o9 (boundary) and o10 (NULL estimate) are not flagged
    "OLIST-009": 3,   # groups r1, r3 and the NULL group: GROUP BY treats NULLs as equal (documented behaviour)
    "OLIST-010": 2,
}


class _Inspector:
    def __init__(self, db):
        self.db = db

    def get_columns(self, table, schema=None):
        return [{"name": row[1]} for row in self.db.execute(f"pragma table_info({table})")]


def _database(populated: bool) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    if populated:
        db.executemany("insert into ecom_customers values(?)", [("c1",), ("c2",)])
        db.executemany("insert into ecom_orders values(?,?,?,?,?,?,?)", ORDERS)
        db.executemany("insert into ecom_products values(?)", [("p1",)])
        db.executemany("insert into ecom_sellers values(?)", [("s1",)])
        db.executemany("insert into ecom_order_items values(?,?,?)", ITEMS)
        db.executemany("insert into ecom_order_reviews values(?,?)", REVIEWS)
    return db


def _count(db: sqlite3.Connection, rule: dict) -> int:
    tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
    sql, params = engine._build_query(rule, _Inspector(db), tables, {})
    sql = sql.replace("COUNT_BIG", "COUNT").replace("dbo.", "")
    args: list = []
    if "condition_value" in params:
        sql = sql.replace(":condition_value", "? COLLATE NOCASE")  # the demo database uses a CI collation
        args = [params["condition_value"]]
    return db.execute(sql, args).fetchone()[0]


@pytest.fixture(scope="module")
def shipped_rules():
    return {rule["id"]: rule for rule in engine.load_rules(engine.DEFAULT_CONFIG_PATH)["rules"]}


def test_all_ten_shipped_rules_are_covered(shipped_rules):
    assert set(shipped_rules) == set(EXPECTED)


@pytest.mark.parametrize("rule_id", sorted(EXPECTED))
def test_each_shipped_rule_counts_the_expected_violations(shipped_rules, rule_id):
    assert _count(_database(True), shipped_rules[rule_id]) == EXPECTED[rule_id]


@pytest.mark.parametrize("rule_id", sorted(EXPECTED))
def test_empty_tables_give_zero_violations_not_errors(shipped_rules, rule_id):
    assert _count(_database(False), shipped_rules[rule_id]) == 0


def test_shipped_rule_flags_match_the_documented_design(shipped_rules):
    assert [i for i, r in shipped_rules.items() if not r["enabled"]] == ["OLIST-009"]
    assert [i for i, r in shipped_rules.items() if r["classification"] == "business_signal"] == ["OLIST-008"]
    assert shipped_rules["OLIST-008"]["severity"] == "warning"
