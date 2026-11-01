"""Verified, read-only, single-purpose connection for the Ecommerce diagnostics.

Nothing in the Ecommerce path touches SQL Server except through ``verified_ecommerce_connection``. Before it hands
out a connection it proves, on that very connection:

1. the settings demand Windows integrated authentication and a local server (no SQL logins, no remote servers);
2. the connection landed in ``dataops_ecommerce_demo`` (the constant, never ``DB_NAME``);
3. the effective login equals ``ORIGINAL_LOGIN()`` (no ``EXECUTE AS`` impersonation) and is ``<this machine>\\dq_reader``;
4. the existing fail-closed ``verify_read_only_principal`` check passes (16 write/DDL capabilities all definitely 0 and no
   writable table);
5. the principal that was permission-checked is the principal that was identified;
6. the principal cannot access ``dataops_sim`` (unknown counts as refusal);
7. a 30 second query timeout is applied to the driver connection.

Any failure raises ``GuardRefusal``. There is no override, no fallback identity and no unverified mode.

The real protection is the database principal verified in step 4. ``GuardedConnection`` adds a second, independent layer:
every statement must be one of three fixed texts or a single aggregate ``SELECT`` with no forbidden keyword. That layer is
defence in depth, not a substitute for the permission check.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from sqlalchemy import text

from app.config import get_settings
from app.db.connection import make_engine
from app.db.readonly_preflight import PERMISSION_SQL, assert_local_target, verify_read_only_principal
from app.db.safety import UnsafeTarget
from app.ecommerce.target import (
    ECOMMERCE_DATABASE,
    EXPECTED_READER_NAME,
    QUERY_TIMEOUT_SECONDS,
    TOOL_BUDGET_SECONDS,
    validate_login_timeout,
)


class GuardRefusal(Exception):
    """The connection or statement was refused. ``kind`` is a fixed label; the message is safe to show."""

    def __init__(self, kind: str, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.reasons = list(reasons or [])


class TimeBudgetExceeded(GuardRefusal):
    """The whole-call time budget ran out before this statement. Nothing further was sent to the database.

    A subclass of ``GuardRefusal`` so every ``except GuardRefusal: raise`` in the diagnostics lets it through; the service
    reports it as ``ECOM-TIMEOUT`` (incomplete), never as a verdict.
    """

    def __init__(self) -> None:
        super().__init__("time_budget_exceeded", "The time budget for this tool call ran out before the checks finished.")


IDENTITY_SQL = (
    "SELECT DB_NAME() AS database_name, SUSER_SNAME() AS effective_login, ORIGINAL_LOGIN() AS original_login, "
    "CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS machine_name"
)
CATALOG_SQL = (
    "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name "
    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = N'dbo'"
)
EXACT_STATEMENTS = frozenset({IDENTITY_SQL, PERMISSION_SQL, CATALOG_SQL})

_AGGREGATE_SELECT = re.compile(r"^SELECT\s+(COUNT_BIG\s*\(|MIN\s*\(|MAX\s*\()", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY|INTO|BACKUP|RESTORE|"
    r"OPENROWSET|OPENQUERY|OPENDATASOURCE|OPENXML|WAITFOR|SHUTDOWN|KILL|RECONFIGURE|USE|DBCC|BULK|XP_\w+|SP_\w+)\b",
    re.IGNORECASE,
)
_BRACKETED = re.compile(r"\[[^\]]*\]")
_BIND = re.compile(r":[A-Za-z_]\w*")


def check_statement(sql: str) -> None:
    """Raise ``GuardRefusal`` unless ``sql`` is a fixed statement or a single aggregate SELECT."""
    if sql in EXACT_STATEMENTS:
        return
    flat = " ".join(str(sql).split())
    bare = _BIND.sub(" ", _BRACKETED.sub(" ", flat))  # quoted identifiers and bind names cannot hide keywords
    if (not _AGGREGATE_SELECT.match(flat) or ";" in flat or "--" in flat or "/*" in flat
            or "'" in flat or _FORBIDDEN.search(bare)):
        raise GuardRefusal("statement_refused", "Only fixed identity/permission/catalog statements and single "
                           "aggregate SELECT statements are allowed.")


def _fingerprint(login: str) -> str:
    return hashlib.sha256(login.strip().lower().encode("utf-8")).hexdigest()[:12]  # same as readonly_preflight


class GuardedConnection:
    """The only handle diagnostics get. It forwards to the raw connection after ``check_statement``."""

    def __init__(self, raw: Any, *, deadline: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._raw = raw
        self.verification: dict[str, Any] = {}
        self._deadline = deadline
        self._monotonic = monotonic
        self._applied_timeout = QUERY_TIMEOUT_SECONDS
        self.deadline_hit = False  # set when a statement was refused for lack of time (see verify_connection)
        self.cleanup_errors: list[str] = []  # exception TYPES from closing the connection/engine (never driver text)

    def _enforce_deadline(self) -> None:
        """Refuse to start a statement after the deadline; shrink the statement timeout so it cannot run past it.

        The timeout only ever decreases (never above ``QUERY_TIMEOUT_SECONDS``). If a new value cannot be applied the
        statement is refused (``timeout_not_applied``), so this cannot fail open.
        """
        if self._deadline is None:
            return
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            self.deadline_hit = True
            raise TimeBudgetExceeded()
        seconds = max(1, min(QUERY_TIMEOUT_SECONDS, math.ceil(remaining)))
        if seconds != self._applied_timeout:
            _apply_timeout(self._raw, seconds)
            self._applied_timeout = seconds

    def execute(self, statement: Any, params: Mapping[str, Any] | None = None) -> Any:
        check_statement(str(statement))
        self._enforce_deadline()
        return self._raw.execute(statement, params) if params else self._raw.execute(statement)

    def scalar(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.execute(text(sql), params).scalar_one()

    def first_row(self, sql: str) -> tuple:
        row = self.execute(text(sql)).first()
        return tuple(row) if row is not None else ()

    def mapping_rows(self, sql: str) -> list[Mapping[str, Any]]:
        return list(self.execute(text(sql)).mappings().all())


# --------------------------------------------------------------------------------------------- identity
def evaluate_identity(row: Any) -> dict[str, Any]:
    """Pure check of the identity row. ``ok`` only for ``<machine>\\dq_reader`` in the expected database, unimpersonated."""
    result: dict[str, Any] = {"ok": False, "reasons": [], "principal_fingerprint": None, "windows_login": None}
    if not isinstance(row, Mapping):
        result["reasons"].append("identity_row_missing")
        return result
    reasons: list[str] = result["reasons"]
    database = row.get("database_name")
    if not isinstance(database, str) or database.lower() != ECOMMERCE_DATABASE.lower():
        reasons.append("database_mismatch")
    effective, original, machine = row.get("effective_login"), row.get("original_login"), row.get("machine_name")
    if not all(isinstance(v, str) and v.strip() for v in (effective, original, machine)):
        reasons.append("identity_unknown")
        return result
    result["principal_fingerprint"] = _fingerprint(effective)
    if effective.strip().lower() != original.strip().lower():
        reasons.append("impersonation_detected")
    domain, separator, local = effective.strip().partition("\\")
    result["windows_login"] = bool(separator and local)
    if not result["windows_login"]:
        reasons.append("not_a_windows_login")
    else:
        if local.lower() != EXPECTED_READER_NAME:
            reasons.append("unexpected_principal")
        if domain.lower() != machine.strip().lower():
            reasons.append("unexpected_principal_domain")
    result["ok"] = not reasons
    return result


def _check_settings(settings: Any) -> None:
    if getattr(settings, "db_auth_mode", None) != "windows":
        raise GuardRefusal("windows_authentication_required",
                           "The Ecommerce diagnostics require Windows integrated authentication (DB_AUTH_MODE=windows).")
    try:
        assert_local_target(settings)
    except UnsafeTarget as exc:
        raise GuardRefusal("non_local_server", str(exc)) from None


def _check_login_timeout(settings: Any, deadline: float | None, monotonic: Callable[[], float]) -> None:
    """Refuse a login timeout that could consume the call budget. Explicit refusal, never a silent clamp."""
    budget = TOOL_BUDGET_SECONDS if deadline is None else deadline - monotonic()
    try:
        validate_login_timeout(getattr(settings, "db_login_timeout_seconds", None), budget)
    except ValueError as exc:
        raise GuardRefusal("login_timeout_invalid", f"{exc}. Lower DB_LOGIN_TIMEOUT_SECONDS; no connection was attempted.") from None


def _apply_timeout(raw: Any, seconds: int) -> None:
    try:
        driver = raw.connection.driver_connection
        driver.timeout = seconds
        applied = getattr(driver, "timeout", None) == seconds
    except Exception:  # noqa: BLE001
        applied = False
    if not applied:
        raise GuardRefusal("timeout_not_applied", f"The {seconds} second query timeout could not be applied, so the run was refused.")


def verify_connection(guarded: GuardedConnection) -> dict[str, Any]:
    """Run every identity and permission check on ``guarded``. Returns a safe summary or raises ``GuardRefusal``."""
    try:
        row = guarded.mapping_rows(IDENTITY_SQL)
        identity_row = row[0] if row else None
    except GuardRefusal:
        raise
    except Exception:  # noqa: BLE001 - driver text is never kept
        raise GuardRefusal("identity_query_failed", "The identity of the connected principal could not be read.") from None
    identity = evaluate_identity(identity_row)
    if not identity["ok"]:
        raise GuardRefusal("identity_not_verified", "The connected principal is not the expected read-only account "
                           f"or the wrong database was reached ({', '.join(identity['reasons'])}).", identity["reasons"])

    permissions = verify_read_only_principal(guarded, ECOMMERCE_DATABASE)  # the existing guard, unchanged
    if not permissions.get("verified") and guarded.deadline_hit:
        # the existing check turns any failure, including our time limit, into "inconclusive"; say what really happened
        raise TimeBudgetExceeded()
    if not permissions.get("verified"):
        raise GuardRefusal("read_only_not_verified", "Read-only access could not be verified "
                           f"({permissions.get('status')}); no diagnostic query was run.", list(permissions.get("reasons", [])))
    if permissions.get("principal_fingerprint") != identity["principal_fingerprint"]:
        raise GuardRefusal("principal_mismatch", "The permission check and the identity check disagree about the principal.")
    if permissions.get("dataops_sim_accessible") != 0:
        raise GuardRefusal("dataops_sim_accessible", "The principal can access (or may be able to access) the simulation "
                           "database; the dedicated reader must not. No diagnostic query was run.")
    return {
        "verified": True,
        "database": ECOMMERCE_DATABASE,
        "authentication": "windows_integrated",
        "principal_fingerprint": identity["principal_fingerprint"],
        "expected_reader": True,
        "impersonation": False,
        "read_only_verified": True,
        "permission_status": permissions["status"],
        "checks": permissions["checks"],
        "writable_table_count": permissions["writable_table_count"],
        "table_count": permissions["table_count"],
        "dataops_sim_accessible": 0,
        "query_timeout_seconds": QUERY_TIMEOUT_SECONDS,
    }


@contextmanager
def verified_ecommerce_connection(settings: Any = None, *, engine_factory: Callable[..., Any] = make_engine,
                                  timeout_seconds: int = QUERY_TIMEOUT_SECONDS, deadline: float | None = None,
                                  monotonic: Callable[[], float] = time.monotonic) -> Iterator[GuardedConnection]:
    """Yield a verified ``GuardedConnection`` or raise ``GuardRefusal``. Errors raised by the caller's body pass through.

    ``deadline`` (a ``monotonic()`` instant) bounds every statement on the connection, verification included.
    """
    engine = raw = None
    try:
        try:
            settings = settings if settings is not None else get_settings()
        except Exception:  # noqa: BLE001
            raise GuardRefusal("settings_invalid", "The application settings could not be loaded (values not shown).") from None
        _check_settings(settings)
        _check_login_timeout(settings, deadline, monotonic)  # before any engine exists
        try:
            engine = engine_factory(settings, database=ECOMMERCE_DATABASE, admin=False)  # fixed target; DB_NAME ignored
            raw = engine.connect()
        except Exception:  # noqa: BLE001
            if deadline is not None and monotonic() >= deadline:  # the login itself used up the whole budget
                raise TimeBudgetExceeded() from None
            raise GuardRefusal("connection_failed", "Could not connect to SQL Server as the current Windows identity "
                               "(details withheld).") from None
        _apply_timeout(raw, timeout_seconds)
        guarded = GuardedConnection(raw, deadline=deadline, monotonic=monotonic)
        guarded._applied_timeout = timeout_seconds
        guarded.verification = verify_connection(guarded)
    except BaseException:
        _close(raw, engine)
        raise
    try:
        yield guarded
    finally:
        guarded.cleanup_errors.extend(_close(raw, engine))  # visible to the caller after the block; never silently dropped


def _close(raw: Any, engine: Any) -> list[str]:
    """Close the connection, then dispose the engine; both are always attempted. Returns the exception type of each failure."""
    failures: list[str] = []
    for resource, method in ((raw, "close"), (engine, "dispose")):
        if resource is not None:
            try:
                getattr(resource, method)()
            except Exception as exc:  # noqa: BLE001 - only the type is kept; driver text can contain server details
                failures.append(f"{method}:{type(exc).__name__}")
    return failures
