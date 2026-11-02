"""Read-only: ask the configured SQL Server which database and server this application is actually connected to.

    python -m scripts.check_database_identity

It runs exactly two statements, ``SELECT DB_NAME()`` and ``SELECT @@SERVERNAME``, and compares the database it got with the
configured ``DB_NAME``. It uses the same engine factory and settings as the application. It creates, changes, seeds, wipes,
migrates and switches nothing, and never prints a connection string or credential.
Exit codes: 0 identity matches; 1 identity could not be established or does not match; 2 configuration or connection problem.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.connection import DatabaseConfigError, DatabaseUnavailable, make_engine, safe_error_summary
from scripts._common import EXIT_CHECK_FAILED, EXIT_ENV, EXIT_OK, load_settings, run_cli

STATEMENTS = ("SELECT DB_NAME()", "SELECT @@SERVERNAME")  # the only SQL this script may run


@dataclass(frozen=True)
class Identity:
    database: str | None
    server: str | None


def read_identity(engine: Any) -> Identity:
    """Two scalar reads on one connection. Raises on a connection failure; returns None fields if a value is not a clean string."""
    with engine.connect() as conn:
        database = conn.execute(text(STATEMENTS[0])).scalar()
        server = conn.execute(text(STATEMENTS[1])).scalar()
    clean = lambda v: v.strip() if isinstance(v, str) and v.strip() else None  # noqa: E731
    return Identity(clean(database), clean(server))


def compare(identity: Identity, expected: str) -> tuple[int, str]:
    """(exit code, message). Names are compared case-sensitively after trimming; anything unclear is a failure."""
    if identity.database is None:
        return EXIT_CHECK_FAILED, "FAIL: the server did not return a database name; the identity cannot be established."
    if identity.database != expected:
        return EXIT_CHECK_FAILED, f"FAIL: connected to database '{identity.database}' but the configured database is '{expected}'."
    return EXIT_OK, f"OK: connected database matches the configured database '{expected}'."


def run(settings: Settings, engine_factory: Callable[[Settings], Any] = make_engine) -> int:
    expected = settings.db_name
    print(f"Configured: server={settings.db_server}  database={expected}  auth={'windows' if settings.db_auth_mode == 'windows' else 'sql login'}")
    try:
        engine = engine_factory(settings)
        identity = read_identity(engine)
    except (DatabaseConfigError, DatabaseUnavailable) as exc:
        print(f"FAIL: {exc}")
        return EXIT_ENV
    except (SQLAlchemyError, OSError, ImportError) as exc:
        print(f"FAIL: could not read the database identity ({safe_error_summary(exc)})")
        return EXIT_ENV
    print(f"Actual:     server={identity.server or 'unknown'}  database={identity.database or 'unknown'}")
    code, message = compare(identity, expected)
    print(message)
    listed = bool(identity.database) and identity.database.lower() in settings.recovery_targets()
    print(f"Informational: actual database {'is' if listed else 'is NOT'} in RECOVERY_TARGET_ALLOWLIST (this does not affect the exit code).")
    return code


def main() -> int:
    argparse.ArgumentParser(prog="python -m scripts.check_database_identity", description=__doc__.splitlines()[0]).parse_args()
    return run(load_settings())


if __name__ == "__main__":
    run_cli(main)
