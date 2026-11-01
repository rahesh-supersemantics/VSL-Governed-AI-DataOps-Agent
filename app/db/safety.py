"""Guards for destructive operations (reset). Fail closed: any doubt refuses."""

from __future__ import annotations

from app.config import Settings
from app.db.connection import is_local_server

ALLOWED_DB_SUFFIXES = ("_sim", "_test")


class UnsafeTarget(Exception):
    """The requested destructive operation was refused (message is safe to show)."""


def assert_safe_reset_target(settings: Settings, database_arg: str | None, confirmed: bool) -> None:
    if not confirmed:
        raise UnsafeTarget("Refusing: pass --yes-delete-all-data to confirm you understand all "
                           "simulation rows in the target database will be deleted.")
    if settings.app_env == "production":
        raise UnsafeTarget("Refusing: APP_ENV=production.")
    if not is_local_server(settings.db_server):
        raise UnsafeTarget(f"Refusing: DB_SERVER '{settings.db_server}' is not a local server.")
    if not database_arg or database_arg != settings.db_name:
        raise UnsafeTarget("Refusing: --database must be given and must exactly match DB_NAME "
                           f"('{settings.db_name}').")
    if not settings.db_name.lower().endswith(ALLOWED_DB_SUFFIXES):
        raise UnsafeTarget("Refusing: the database name must end with one of "
                           f"{', '.join(ALLOWED_DB_SUFFIXES)} so a real database is never reset.")


# Databases the SQL Server integration suite (which creates tables and DELETEs every row) must never touch.
PROTECTED_DATABASES = frozenset({"dataops_sim", "dataops_ecommerce_demo", "master", "model", "msdb", "tempdb"})
INTEGRATION_DB_SUFFIX = "_test"


def assert_safe_integration_target(settings: Settings, observed_database: str | None = None) -> None:
    """Fail-closed guard for the SQL Server integration suite.

    Stricter than ``assert_safe_reset_target``, which deliberately allows ``dataops_sim`` for the reset CLI: here only a
    database whose name ends ``_test`` is accepted, and the protected databases are refused by name. ``observed_database``
    must be read from the live connection (``SELECT DB_NAME()``), never from the setting being checked, otherwise the
    comparison proves nothing. Pass ``None`` only for the configuration-only check made before anything is connected.
    """
    name = settings.db_name.strip().lower()
    if settings.app_env == "production":
        raise UnsafeTarget("Refusing: APP_ENV=production.")
    if not is_local_server(settings.db_server):
        raise UnsafeTarget(f"Refusing: DB_SERVER '{settings.db_server}' is not a local server.")
    if name in PROTECTED_DATABASES:
        raise UnsafeTarget(f"Refusing: '{settings.db_name}' is a protected database and is never an integration-test target.")
    if not name.endswith(INTEGRATION_DB_SUFFIX):
        raise UnsafeTarget(f"Refusing: the integration-test database name must end with '{INTEGRATION_DB_SUFFIX}'.")
    if observed_database is not None:
        observed = observed_database.strip().lower()
        if not observed:
            raise UnsafeTarget("Refusing: the connected database name could not be determined.")
        if observed in PROTECTED_DATABASES:
            raise UnsafeTarget(f"Refusing: the connection is on protected database '{observed_database}'.")
        if observed != name:
            raise UnsafeTarget(f"Refusing: the connection is on database '{observed_database}', not the configured "
                               f"'{settings.db_name}'.")
