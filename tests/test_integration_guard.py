"""Offline regression tests for the SQL Server integration-suite safety guard (no database is contacted).

The reset guard deliberately allows ``dataops_sim`` (the reset CLI targets it); the integration suite must not, so it has
its own stricter guard. These tests also pin that the fixtures compare the configured name with the name the server
reports, instead of comparing a setting with itself.
"""

from pathlib import Path

import pytest

from app.config import Settings
from app.db.safety import (
    PROTECTED_DATABASES,
    UnsafeTarget,
    assert_safe_integration_target,
    assert_safe_reset_target,
)

CONFTEST = Path(__file__).parent / "integration" / "conftest.py"


def settings(**kw):
    return Settings(**{"db_name": "dataops_sim_test", "db_server": "localhost", **kw})


def test_the_dedicated_test_database_is_accepted():
    assert_safe_integration_target(settings())  # configuration-only check
    assert_safe_integration_target(settings(), "dataops_sim_test")
    assert_safe_integration_target(settings(), "  DataOps_Sim_Test ")  # SQL Server may report a different case


@pytest.mark.parametrize("name", ["dataops_sim", "dataops_ecommerce_demo", "master", "msdb"])
def test_protected_databases_are_refused_by_name(name):
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(settings(db_name=name))


@pytest.mark.parametrize("name", ["sales_prod", "dataops_sim_sim", "dataops_ecommerce_demo_x", "testdb"])
def test_names_without_the_test_suffix_are_refused(name):
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(settings(db_name=name))


def test_production_and_remote_servers_are_refused():
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(settings(app_env="production"))
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(settings(db_server="prod-sql-01"))


@pytest.mark.parametrize("observed", ["dataops_sim", "dataops_ecommerce_demo", "master", "other_test", "", "   "])
def test_a_connection_on_a_different_database_is_refused(observed):
    """The tautology fix: the configured name is checked against what the live connection reports."""
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(settings(), observed)


def test_the_integration_guard_is_stricter_than_the_reset_guard():
    sim = Settings(db_name="dataops_sim")
    assert_safe_reset_target(sim, "dataops_sim", confirmed=True)  # intended for the reset CLI
    with pytest.raises(UnsafeTarget):
        assert_safe_integration_target(sim)


def test_the_protected_set_covers_both_application_databases():
    assert {"dataops_sim", "dataops_ecommerce_demo"} <= PROTECTED_DATABASES


def test_the_fixtures_use_the_independent_guard_and_do_not_create_databases_implicitly():
    source = CONFTEST.read_text(encoding="utf-8")
    assert "assert_safe_integration_target(sql_settings, conn.execute(text(\"SELECT DB_NAME()\"))" in source
    assert "assert_safe_integration_target(sql_settings, observed)" in source
    assert "assert_safe_reset_target(sql_settings, TEST_DB" not in source  # compared a name with itself
    assert "IS NULL CREATE DATABASE" not in source  # creation is opt-in
    assert "SQLSERVER_TESTS_ALLOW_CREATE_DB" in source
