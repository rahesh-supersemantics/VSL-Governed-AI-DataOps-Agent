"""Fakes for the Ecommerce tests. No SQL Server, no network, no .env.

``SqliteRaw`` stands in for the raw SQLAlchemy connection: it answers the three fixed guard statements from supplied rows and
runs every other statement on an in-memory SQLite database (SQL Server specifics are translated), so the REAL guard, statement
gate, rule builder and diagnostics run end to end on synthetic data. ``sqlite_connect`` runs the real ``verify_connection``.
"""

from __future__ import annotations

import sqlite3
import types
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.exc import OperationalError

from app.db.readonly_preflight import PERMISSION_SQL
from app.ecommerce.guard import CATALOG_SQL, IDENTITY_SQL, GuardedConnection, _apply_timeout, verify_connection
from app.ecommerce.target import ECOMMERCE_DATABASE, QUERY_TIMEOUT_SECONDS
from tests.dq_support import read_only_row

MACHINE = "FIXTUREPC"
READER = f"{MACHINE}\\dq_reader"

SCHEMA = """
create table ecom_category_translation(product_category_name text, product_category_name_english text);
create table ecom_customers(customer_id text, customer_unique_id text, customer_zip_code_prefix text, customer_city text, customer_state text);
create table ecom_geolocation(geolocation_zip_code_prefix text, geolocation_lat real, geolocation_lng real, geolocation_city text, geolocation_state text);
create table ecom_order_items(order_id text, order_item_id integer, product_id text, seller_id text, shipping_limit_date text, price real, freight_value real);
create table ecom_order_payments(order_id text, payment_sequential integer, payment_type text, payment_installments integer, payment_value real);
create table ecom_order_reviews(review_id text, order_id text, review_score integer, review_comment_title text, review_comment_message text, review_creation_date text, review_answer_timestamp text);
create table ecom_orders(order_id text, customer_id text, order_status text collate nocase, order_purchase_timestamp text, order_approved_at text,
  order_delivered_carrier_date text, order_delivered_customer_date text, order_estimated_delivery_date text);
create table ecom_products(product_id text, product_category_name text, product_weight_g integer);
create table ecom_sellers(seller_id text, seller_zip_code_prefix text, seller_city text, seller_state text);
"""


def o(n: int) -> str:
    return f"ORD-SECRET-{n:02d}"


C1, C2, C_MISSING = "CUST-SECRET-1", "CUST-SECRET-2", "CUST-SECRET-9"
SECRET_VALUES = [o(i) for i in range(1, 12)] + [C1, C2, C_MISSING, "PROD-SECRET-1", "PROD-SECRET-X", "SELL-SECRET-1",
                                                   "SELL-SECRET-X", "REV-SECRET-1", "REV-SECRET-3", "secret review text"]

# id, customer, status, purchase, carrier, customer date, estimated
ORDERS = [
    (o(1), C1, "delivered", "2020-01-02", "2020-01-03", "2020-01-05", "2020-01-10"),   # clean
    (o(2), C1, "delivered", "2020-01-02", None, "2020-01-05", "2020-01-10"),           # no carrier date
    (o(3), C2, "delivered", "2020-01-02", "2020-01-03", None, "2020-01-10"),           # no customer date
    (o(4), C_MISSING, "delivered", "2020-01-02", "2020-01-03", "2020-01-20", "2020-01-10"),  # orphan customer, late
    (o(5), None, "shipped", "2020-01-02", None, None, "2020-01-10"),                   # NULL customer is not an orphan
    (o(6), C1, "delivered", "2020-01-09", "2020-01-09", "2020-01-05", "2020-01-10"),   # delivered before purchase
    (o(7), C1, "canceled", "2020-01-02", None, "2020-01-30", "2020-01-10"),            # late but not delivered
    (o(8), C1, "DELIVERED", "2020-01-02", "2020-01-03", "2020-01-20", "2020-01-10"),   # case-insensitive match, late
    (o(9), C1, "delivered", "2020-01-02", "2020-01-03", "2020-01-10", "2020-01-10"),   # exactly on time
    (o(10), C1, "delivered", "2020-01-02", "2020-01-03", "2020-01-05", None),          # NULL estimate: not judged
]
ITEMS = [(o(1), 1, "PROD-SECRET-1", "SELL-SECRET-1"), ("ORD-SECRET-77", 1, "PROD-SECRET-1", "SELL-SECRET-1"),
         (o(1), 2, "PROD-SECRET-X", "SELL-SECRET-1"), (o(1), 3, "PROD-SECRET-1", "SELL-SECRET-X"),
         (o(1), 3, None, None)]
PAYMENTS = [(o(1), 1), ("ORD-SECRET-88", 1), (o(1), 2), (None, 1)]
REVIEWS = [("REV-SECRET-1", o(1), "secret review text"), ("REV-SECRET-1", o(2), None), ("REV-2", o(1), None),
           (None, o(1), None), (None, o(2), None), ("REV-SECRET-3", o(1), None), ("REV-SECRET-3", o(1), None),
           ("REV-4", "ORD-SECRET-99", None)]

# What the dirty dataset must produce (derived by hand from the rows above, not from the code under test).
DIRTY = {
    "OLIST-001": 1, "OLIST-002": 1, "OLIST-003": 1, "OLIST-004": 1, "ECOM-FK-PAYMENTS-ORDERS": 1, "ECOM-FK-REVIEWS-ORDERS": 1,
    "OLIST-005": 1, "OLIST-006": 1, "OLIST-007": 1, "OLIST-008": 2, "OLIST-010": 2, "OLIST-009": 2,
}


def make_db(dirty: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(SCHEMA)
    if dirty:
        db.executemany("insert into ecom_customers(customer_id) values(?)", [(C1,), (C2,)])
        db.executemany("insert into ecom_orders values(?,?,?,?,NULL,?,?,?)", ORDERS)
        db.executemany("insert into ecom_products(product_id) values(?)", [("PROD-SECRET-1",), ("PROD-SECRET-1",)])
        db.executemany("insert into ecom_sellers(seller_id) values(?)", [("SELL-SECRET-1",)])
        db.executemany("insert into ecom_order_items(order_id, order_item_id, product_id, seller_id, shipping_limit_date) "
                       "values(?,?,?,?,'2020-02-01')", ITEMS)
        db.executemany("insert into ecom_order_payments(order_id, payment_sequential) values(?,?)", PAYMENTS)
        db.executemany("insert into ecom_order_reviews(review_id, order_id, review_comment_message, review_creation_date) "
                       "values(?,?,?,'2020-03-01')", REVIEWS)
        db.executemany("insert into ecom_category_translation values(?,?)", [("a", "A"), ("a", "B")])
    return db


def make_clean_db() -> sqlite3.Connection:
    """Valid rows only, and no duplicate review ids."""
    db = make_db(dirty=False)
    db.executemany("insert into ecom_customers(customer_id) values(?)", [(C1,)])
    db.executemany("insert into ecom_orders values(?,?,?,?,NULL,?,?,?)", [ORDERS[0]])
    db.executemany("insert into ecom_products(product_id) values(?)", [("PROD-SECRET-1",)])
    db.executemany("insert into ecom_sellers(seller_id) values(?)", [("SELL-SECRET-1",)])
    db.execute("insert into ecom_order_items(order_id, order_item_id, product_id, seller_id) values(?,1,'PROD-SECRET-1','SELL-SECRET-1')", (o(1),))
    db.execute("insert into ecom_order_payments(order_id, payment_sequential) values(?,1)", (o(1),))
    db.execute("insert into ecom_order_reviews(review_id, order_id, review_creation_date) values('REV-SECRET-1',?,'2020-03-01')", (o(1),))
    db.execute("insert into ecom_category_translation values('a','A')")
    return db


def identity_row(**overrides: Any) -> dict:
    row = {"database_name": ECOMMERCE_DATABASE, "effective_login": READER, "original_login": READER, "machine_name": MACHINE}
    row.update(overrides)
    return row


def permission_row(**overrides: Any) -> dict:
    row = read_only_row(database_name=ECOMMERCE_DATABASE, login_name=READER)
    row.update(overrides)
    return row


class _NoTimeoutDriver:
    __slots__ = ()


class _Rows:
    def __init__(self, rows: list) -> None:
        self.rows = rows

    def scalar_one(self):
        if len(self.rows) != 1:
            raise AssertionError("expected exactly one row")
        return self.rows[0][0]

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)

    def mappings(self):
        return _Rows([dict(r) if isinstance(r, dict) else r for r in self.rows])


class FakeClock:
    """A controllable ``monotonic``: statements advance it by their simulated duration."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SqliteRaw:
    def __init__(self, db: sqlite3.Connection, identity: Any = "default", permissions: Any = "default",
                 fail_on: str | None = None, set_timeout: bool = True, clock: FakeClock | None = None,
                 cost: float = 0.0, slow: dict[str, float] | None = None) -> None:
        self.db = db
        self.clock, self.cost, self.slow = clock, cost, dict(slow or {})
        self.timeouts_seen: list[int] = []
        self.identity = identity_row() if identity == "default" else identity
        self.permissions = permission_row() if permissions == "default" else permissions
        self.fail_on = fail_on
        self.executed: list[str] = []
        self.closed = False
        driver = types.SimpleNamespace(timeout=None)
        if not set_timeout:
            driver = _NoTimeoutDriver()  # a timeout cannot be applied or read back
        self.connection = types.SimpleNamespace(driver_connection=driver)

    def close(self) -> None:
        self.closed = True

    def _spend(self, sql: str) -> None:
        """Simulate time: a statement slower than the driver's timeout is cut off at the timeout, like pyodbc."""
        if self.clock is None:
            return
        limit = getattr(self.connection.driver_connection, "timeout", None)
        self.timeouts_seen.append(limit)
        duration = next((d for key, d in self.slow.items() if key in sql), self.cost)
        if limit is not None and duration > limit:
            self.clock.now += limit
            raise OperationalError("SELECT", {}, Exception("Query timeout expired (HYT00) Server=SECRET-HOST"))
        self.clock.now += duration

    def execute(self, statement: Any, params: dict | None = None):
        sql = str(statement)
        self.executed.append(sql)
        self._spend(sql)
        if sql == IDENTITY_SQL:
            return _Rows([self.identity] if self.identity is not None else [])
        if sql == PERMISSION_SQL:
            return _Rows([self.permissions] if self.permissions is not None else [])
        if sql == CATALOG_SQL:
            rows = []
            for (table,) in self.db.execute("select name from sqlite_master where type='table'").fetchall():
                rows += [{"table_name": table, "column_name": r[1]} for r in self.db.execute(f"pragma table_info({table})")]
            return _Rows(rows)
        if self.fail_on and self.fail_on in sql:
            raise OperationalError("SELECT", {}, Exception("driver text: Server=SECRET-HOST;PWD=hunter2"))
        translated = sql.replace("COUNT_BIG", "COUNT").replace("dbo.", "")
        rows = self.db.execute(translated, dict(params or {})).fetchall()
        if translated.startswith("SELECT MIN"):
            rows = [tuple(datetime.fromisoformat(v) if isinstance(v, str) else v for v in row) for row in rows]
        return _Rows(rows)


class FakeEngine:
    def __init__(self, raw: SqliteRaw, connect_error: Exception | None = None) -> None:
        self.raw, self.connect_error, self.disposed = raw, connect_error, False

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        return self.raw

    def dispose(self) -> None:
        self.disposed = True


class FixtureSettings:
    db_server = "localhost\\SQLEXPRESS03"
    db_auth_mode = "windows"
    db_name = "dataops_sim"  # the shared setting the Ecommerce path must ignore
    db_login_timeout_seconds = 5  # the shared default


def sqlite_connect(db: sqlite3.Connection | None = None, *, record: list | None = None, **raw_kwargs: Any) -> Callable:
    """A ``connect`` callable for ``EcommerceDiagnosticsService``: the real ``verify_connection`` over a ``SqliteRaw``."""
    database = db if db is not None else make_db()

    @contextmanager
    def connect(deadline: float | None = None):
        raw = SqliteRaw(database, **raw_kwargs)
        if record is not None:
            record.append(raw)
        clock = raw_kwargs.get("clock")
        _apply_timeout(raw, QUERY_TIMEOUT_SECONDS)  # what verified_ecommerce_connection does before anything else
        guarded = GuardedConnection(raw, deadline=deadline, **({"monotonic": clock} if clock else {}))
        try:
            guarded.verification = verify_connection(guarded)
            yield guarded
        finally:
            raw.close()  # the real factory closes the connection and disposes the engine

    return connect
