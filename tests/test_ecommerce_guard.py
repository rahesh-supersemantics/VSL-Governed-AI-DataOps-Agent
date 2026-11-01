"""The Ecommerce connection guard: statement gate, identity, permissions, settings, timeout, cleanup."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.db import readonly_preflight as pre
from app.ecommerce import guard
from app.ecommerce.guard import (
    CATALOG_SQL,
    IDENTITY_SQL,
    GuardedConnection,
    GuardRefusal,
    check_statement,
    evaluate_identity,
    verified_ecommerce_connection,
    verify_connection,
)
from app.ecommerce.target import ECOMMERCE_DATABASE, QUERY_TIMEOUT_SECONDS
from tests.ecommerce_support import (
    MACHINE,
    READER,
    FakeEngine,
    FixtureSettings,
    SqliteRaw,
    identity_row,
    make_db,
    permission_row,
)


# ----------------------------------------------------------------------------------------- statement gate
@pytest.mark.parametrize("sql", [
    IDENTITY_SQL, CATALOG_SQL, pre.PERMISSION_SQL,
    "SELECT COUNT_BIG(*) FROM dbo.[ecom_orders] AS t WHERE t.[customer_id] IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dbo.[ecom_customers] AS p WHERE p.[customer_id] = t.[customer_id])",
    "SELECT COUNT_BIG(*) FROM dbo.[ecom_orders] AS t WHERE t.[order_delivered_customer_date] IS NULL AND t.[order_status] = :condition_value",
    "SELECT COUNT_BIG(*) FROM (SELECT [review_id] FROM dbo.[ecom_order_reviews] GROUP BY [review_id] HAVING COUNT_BIG(*) > 1) AS duplicates",
    "SELECT MIN([order_purchase_timestamp]) AS min_v, MAX([order_purchase_timestamp]) AS max_v FROM dbo.[ecom_orders]",
    "SELECT COUNT_BIG(*) FROM dbo.[ecom_orders] AS t WHERE t.[update_date] IS NULL",  # keyword inside a quoted identifier
])
def test_allowed_statements_pass(sql):
    check_statement(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO dbo.ecom_orders VALUES (1)",
    "UPDATE dbo.ecom_orders SET order_status = 'x'",
    "DELETE FROM dbo.ecom_orders",
    "MERGE dbo.ecom_orders AS t USING x ON 1=1 WHEN MATCHED THEN DELETE;",
    "DROP TABLE dbo.ecom_orders",
    "TRUNCATE TABLE dbo.ecom_orders",
    "ALTER TABLE dbo.ecom_orders ADD x int",
    "CREATE TABLE x (a int)",
    "EXEC sp_who",
    "EXECUTE xp_cmdshell 'dir'",
    "GRANT SELECT ON dbo.ecom_orders TO public",
    "SELECT * FROM dbo.ecom_orders",
    "SELECT 1",
    "SELECT COUNT_BIG(*) INTO dbo.stolen FROM dbo.ecom_orders",
    "SELECT COUNT_BIG(*) FROM dbo.ecom_orders; DROP TABLE dbo.ecom_orders",
    "SELECT COUNT_BIG(*) FROM dbo.ecom_orders -- comment",
    "SELECT COUNT_BIG(*) FROM dbo.ecom_orders /* hidden */",
    "SELECT COUNT_BIG(*) FROM dbo.ecom_orders WHERE order_status = 'delivered'",  # literal values must be bound parameters
    "SELECT COUNT_BIG(*) FROM OPENROWSET('x','y','z')",
    "select count_big(*) from dbo.t where 1=1 waitfor delay '0:0:5'",
    "WITH x AS (SELECT 1) SELECT COUNT_BIG(*) FROM x",
    "  UPDATE dbo.t SET a = 1",
    "",
])
def test_everything_else_is_refused(sql):
    with pytest.raises(GuardRefusal) as caught:
        check_statement(sql)
    assert caught.value.kind == "statement_refused"


def test_fixed_statements_are_unchanged_by_text_wrapping():
    for sql in (IDENTITY_SQL, CATALOG_SQL, pre.PERMISSION_SQL):
        assert str(text(sql)) == sql  # the exact-match allow-list compares str(statement)


def test_guarded_connection_gates_before_executing():
    raw = SqliteRaw(make_db())
    guarded = GuardedConnection(raw)
    with pytest.raises(GuardRefusal):
        guarded.execute(text("DELETE FROM dbo.ecom_orders"))
    assert raw.executed == []  # nothing reached the connection
    assert guarded.scalar("SELECT COUNT_BIG(*) FROM dbo.[ecom_orders]") == 10
    assert raw.executed == ["SELECT COUNT_BIG(*) FROM dbo.[ecom_orders]"]


# ------------------------------------------------------------------------------------------------ identity
def test_expected_identity_is_accepted():
    result = evaluate_identity(identity_row())
    assert result["ok"] is True and result["reasons"] == [] and result["windows_login"] is True
    assert len(result["principal_fingerprint"]) == 12


@pytest.mark.parametrize("override,reason", [
    ({"database_name": "dataops_sim"}, "database_mismatch"),
    ({"database_name": "master"}, "database_mismatch"),
    ({"database_name": None}, "database_mismatch"),
    ({"effective_login": f"{MACHINE}\\dq_reader", "original_login": f"{MACHINE}\\rahes"}, "impersonation_detected"),
    ({"effective_login": "sa", "original_login": "sa"}, "not_a_windows_login"),
    ({"effective_login": f"{MACHINE}\\rahes", "original_login": f"{MACHINE}\\rahes"}, "unexpected_principal"),
    ({"effective_login": f"{MACHINE}\\dq_reader2", "original_login": f"{MACHINE}\\dq_reader2"}, "unexpected_principal"),
    ({"effective_login": "OTHERPC\\dq_reader", "original_login": "OTHERPC\\dq_reader"}, "unexpected_principal_domain"),
    ({"effective_login": None}, "identity_unknown"),
    ({"original_login": ""}, "identity_unknown"),
    ({"machine_name": None}, "identity_unknown"),
])
def test_wrong_identities_are_refused(override, reason):
    result = evaluate_identity(identity_row(**override))
    assert result["ok"] is False and reason in result["reasons"]


def test_identity_comparison_ignores_case_only():
    assert evaluate_identity(identity_row(effective_login=READER.upper(), original_login=READER.lower()))["ok"] is True


def test_missing_identity_row_is_refused():
    assert evaluate_identity(None)["reasons"] == ["identity_row_missing"]


# ------------------------------------------------------------------------------------------- verification
def verify(**kwargs):
    raw = SqliteRaw(make_db(), **kwargs)
    return raw, verify_connection(GuardedConnection(raw))


def test_verification_succeeds_and_reports_no_login_name():
    raw, summary = verify()
    assert summary["verified"] is True and summary["read_only_verified"] is True
    assert summary["database"] == ECOMMERCE_DATABASE and summary["authentication"] == "windows_integrated"
    assert summary["expected_reader"] is True and summary["impersonation"] is False and summary["dataops_sim_accessible"] == 0
    assert summary["query_timeout_seconds"] == QUERY_TIMEOUT_SECONDS == 30
    blob = json.dumps(summary)
    assert READER not in blob and "dq_reader" not in blob and MACHINE not in blob
    assert [s for s in raw.executed] == [IDENTITY_SQL, pre.PERMISSION_SQL]  # identity first, then the existing permission check


def test_the_existing_permission_check_is_what_runs(monkeypatch):
    calls = []
    real = guard.verify_read_only_principal

    def spy(conn, database):
        calls.append(database)
        return real(conn, database)

    monkeypatch.setattr(guard, "verify_read_only_principal", spy)
    verify()
    assert calls == [ECOMMERCE_DATABASE]


def test_identity_failure_stops_before_the_permission_query():
    raw = SqliteRaw(make_db(), identity=identity_row(effective_login="sa", original_login="sa"))
    with pytest.raises(GuardRefusal) as caught:
        verify_connection(GuardedConnection(raw))
    assert caught.value.kind == "identity_not_verified" and raw.executed == [IDENTITY_SQL]


@pytest.mark.parametrize("flag", pre.WRITE_CAPABLE_FLAGS)
def test_every_write_capable_flag_refuses(flag):
    with pytest.raises(GuardRefusal) as caught:
        verify(permissions=permission_row(**{flag: 1}))
    assert caught.value.kind == "read_only_not_verified" and f"write_capable:{flag}" in caught.value.reasons


def test_writable_tables_unknown_flags_and_failed_permission_query_refuse():
    for overrides in ({"writable_table_count": 1}, {"is_sysadmin": None}, {"can_delete": "0"}, {"table_count": 0}):
        with pytest.raises(GuardRefusal) as caught:
            verify(permissions=permission_row(**overrides))
        assert caught.value.kind == "read_only_not_verified"
    with pytest.raises(GuardRefusal) as caught:
        verify(permissions=None)
    assert caught.value.kind == "read_only_not_verified"


def test_a_permission_row_for_another_principal_is_refused():
    with pytest.raises(GuardRefusal) as caught:
        verify(permissions=permission_row(login_name=f"{MACHINE}\\someone_else"))
    assert caught.value.kind == "principal_mismatch"


@pytest.mark.parametrize("value", [1, None, "0"])
def test_access_to_dataops_sim_or_an_unknown_answer_refuses(value):
    with pytest.raises(GuardRefusal) as caught:
        verify(permissions=permission_row(can_access_dataops_sim=value))
    assert caught.value.kind == "dataops_sim_accessible"


# --------------------------------------------------------------------------------------- connection factory
def open_with(settings=None, *, raw=None, engine=None, factory=None, **kwargs):
    raw = raw or SqliteRaw(make_db())
    engine = engine or FakeEngine(raw)
    seen = {}

    def default_factory(settings_, **kw):
        seen.update(kw)
        return engine

    return raw, engine, seen, verified_ecommerce_connection(
        settings or FixtureSettings(), engine_factory=factory or default_factory, **kwargs)


def test_connection_is_made_to_the_fixed_database_not_db_name():
    raw, engine, seen, manager = open_with()
    with manager as conn:
        assert conn.verification["verified"] is True
        assert conn.scalar("SELECT COUNT_BIG(*) FROM dbo.[ecom_orders]") == 10
    assert seen == {"database": ECOMMERCE_DATABASE, "admin": False}  # FixtureSettings.db_name is dataops_sim and is ignored
    assert raw.closed and engine.disposed
    assert raw.connection.driver_connection.timeout == 30


def test_sql_authentication_is_refused_before_any_engine_is_built():
    class SqlAuth(FixtureSettings):
        db_auth_mode = "sql"

    built = []
    with pytest.raises(GuardRefusal) as caught:
        with verified_ecommerce_connection(SqlAuth(), engine_factory=lambda *a, **k: built.append(1)):
            pass
    assert caught.value.kind == "windows_authentication_required" and built == []


@pytest.mark.parametrize("server", ["prod-sql.example.com", "10.0.0.5\\SQLEXPRESS", "", "tcp:remote,1433"])
def test_non_local_servers_are_refused(server):
    class Remote(FixtureSettings):
        db_server = server

    built = []
    with pytest.raises(GuardRefusal) as caught:
        with verified_ecommerce_connection(Remote(), engine_factory=lambda *a, **k: built.append(1)):
            pass
    assert caught.value.kind == "non_local_server" and built == []


def test_unusable_settings_are_refused_without_echoing_values(monkeypatch):
    def boom():
        raise ValueError("DB_PASSWORD=hunter2")

    monkeypatch.setattr(guard, "get_settings", boom)
    with pytest.raises(GuardRefusal) as caught:
        with verified_ecommerce_connection(engine_factory=lambda *a, **k: None):
            pass
    assert caught.value.kind == "settings_invalid" and "hunter2" not in str(caught.value)


def test_connection_failure_is_refused_without_driver_text():
    engine = FakeEngine(SqliteRaw(make_db()), connect_error=RuntimeError("Server=SECRET-HOST;PWD=hunter2"))
    with pytest.raises(GuardRefusal) as caught:
        with open_with(engine=engine)[3]:
            pass
    assert caught.value.kind == "connection_failed"
    assert "SECRET-HOST" not in str(caught.value) and "hunter2" not in str(caught.value) and engine.disposed


def test_the_timeout_must_be_applied_or_the_connection_is_refused():
    raw = SqliteRaw(make_db(), set_timeout=False)
    engine = FakeEngine(raw)
    with pytest.raises(GuardRefusal) as caught:
        with open_with(raw=raw, engine=engine)[3]:
            pass
    assert caught.value.kind == "timeout_not_applied" and raw.closed and engine.disposed and raw.executed == []


def test_verification_failure_closes_everything_and_never_yields():
    raw = SqliteRaw(make_db(), permissions=permission_row(is_db_owner=1))
    engine = FakeEngine(raw)
    entered = []
    with pytest.raises(GuardRefusal):
        with open_with(raw=raw, engine=engine)[3]:
            entered.append(True)
    assert entered == [] and raw.closed and engine.disposed


def test_errors_in_the_body_pass_through_unchanged_and_resources_are_closed():
    raw, engine, _seen, manager = open_with()
    with pytest.raises(KeyError):
        with manager:
            raise KeyError("diagnostic bug")
    assert raw.closed and engine.disposed


def test_there_is_no_override_or_unverified_mode():
    import inspect

    assert "allow_unverified_read_only" not in inspect.getsource(guard)
    assert "unverified" not in inspect.signature(verified_ecommerce_connection).parameters
    assert set(inspect.signature(verified_ecommerce_connection).parameters) == {
        "settings", "engine_factory", "timeout_seconds", "deadline", "monotonic"}  # none of them can relax a check


def test_the_existing_guard_is_untouched():
    assert len(pre.WRITE_CAPABLE_FLAGS) == 16
    assert "dataops_sim" in pre.PERMISSION_SQL and "writable_table_count" in pre.PERMISSION_SQL
