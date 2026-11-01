"""EcommerceDiagnosticsService: the one entry point the Ecommerce MCP tools call.

Every public method returns a ``DiagnosticResult`` and never raises. Each call opens its own *verified* connection
(``verified_ecommerce_connection``); if verification fails the result is a structured ``ECOM-GUARD-REFUSED`` error and no
diagnostic query runs. There is no fallback to an unverified identity. Driver and exception text is never copied into a
result because it can contain server names or connection details.
"""

from __future__ import annotations

import logging
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.exc import SQLAlchemyError

from app.diagnostics.models import (
    DIAG_DB_ERROR,
    DIAG_INTERNAL,
    DIAG_INVALID_INPUT,
    DiagnosticReport,
    DiagnosticResult,
    InvalidInput,
    error_result,
)
from app.ecommerce import diagnostics as dx
from app.ecommerce.guard import GuardedConnection, GuardRefusal, TimeBudgetExceeded, verified_ecommerce_connection
from app.ecommerce.target import TOOL_BUDGET_SECONDS, validate_timeouts

logger = logging.getLogger("app.ecommerce")

Connect = Callable[..., AbstractContextManager]  # called as connect(deadline=<monotonic instant>)

_RULE_BUCKETS = {"passed": "passed", "failed": "failed", "flagged": "flagged", "pending_investigation": "pending_investigation",
                 "skipped": "skipped", "error": "unassessed"}
CLEANUP_NOTE = "CLEANUP-WARNING: closing the database connection reported an error"
INCOMPLETE_CODES = frozenset({dx.ECOM_PARTIAL, dx.ECOM_TIMEOUT})  # a result carrying one of these was not fully assessed
_CHECK_BUCKETS = {"ok": "passed", "issue_detected": "failed", "warning": "flagged", "error": "unassessed"}


class EcommerceReport(DiagnosticReport):
    """``DiagnosticReport`` plus an Ecommerce roll-up: target verification and per-rule / per-check counts."""

    def summary(self) -> dict[str, Any]:
        rules = {b: 0 for b in _RULE_BUCKETS.values()}
        checks = {b: 0 for b in _CHECK_BUCKETS.values()}
        listing: list[dict[str, Any]] = []
        partial: list[str] = []
        verified, reason = False, "the target check did not run"
        for result in self.results:
            checks[_CHECK_BUCKETS[result.status]] += 1
            rule_items = [e for e in result.evidence if e.get("type") == "rule_result"]
            for item in rule_items:
                rules[_RULE_BUCKETS.get(item.get("status"), "unassessed")] += 1
            not_assessed = [e["rule_id"] for e in rule_items if e.get("status") == "error"]
            if result.status == "error":
                execution = "not_completed"
            elif result.error_code in INCOMPLETE_CODES or not_assessed:
                execution = "partial"  # findings of the rules that ran are kept; the others were NOT assessed
                partial.append(result.diagnostic_name)
            else:
                execution = "complete"
            listing.append({"diagnostic": result.diagnostic_name, "status": result.status, "severity": result.severity,
                            "error_code": result.error_code, "execution": execution, "rules_not_assessed": not_assessed,
                            "classifications": sorted({e["classification"] for e in rule_items}),
                            "violations": {e["rule_id"]: e.get("violations") for e in rule_items
                                           if e.get("status") not in {"passed", "skipped"}}})
            if result.diagnostic_name == dx.TARGET_NAME:
                observed = result.observed_value or {}
                verified = (result.status in {"ok", "issue_detected"} and observed.get("database") == dx.ECOMMERCE_DATABASE
                            and observed.get("read_only_verified") is True)
                reason = ("database, account and read-only permissions verified" if verified
                          else f"the target check reported {result.status}")
        if self.results and all(r.error_code == "ECOM-GUARD-REFUSED" for r in self.results):
            verified, reason = False, "the connection guard refused; nothing was queried"
        not_completed = [r.diagnostic_name for r in self.results if r.status == "error"]
        # a partly assessed check (ECOM-PARTIAL, even when its status is warning or issue_detected) is NOT a complete run
        executed = bool(self.results) and not not_completed and not partial
        return {"execution": "complete" if executed else "incomplete", "target_verified": verified,
                "time_budget_exceeded": any(r.error_code == dx.ECOM_TIMEOUT for r in self.results),
                "checks_not_completed": not_completed, "checks_partially_completed": partial,
                "connection_cleanup_warning": any(n.startswith(CLEANUP_NOTE) for r in self.results for n in r.notes),
                "target_verification": reason, "checks": checks, "rules": rules, "diagnostics": listing}

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["ecommerce_summary"] = self.summary()
        return data


class EcommerceDiagnosticsService:
    def __init__(self, connect: Connect = verified_ecommerce_connection,
                 clock: Callable[[], datetime] | None = None, *, monotonic: Callable[[], float] = time.monotonic,
                 budget_seconds: float = TOOL_BUDGET_SECONDS) -> None:
        self._connect = connect
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        validate_timeouts(budget_seconds, budget_seconds, 0)  # the budget alone must be a positive, finite number
        self._budget = budget_seconds

    @property
    def budget_seconds(self) -> float:
        return self._budget

    def _deadline(self) -> float:
        """One deadline per tool call: connecting, verifying and every statement all fit inside the budget."""
        return self._monotonic() + self._budget

    # ------------------------------------------------------------------------------------------ plumbing
    def _guard_error(self, name: str, params: dict[str, Any], exc: GuardRefusal) -> DiagnosticResult:
        result = error_result(
            name, dx.ECOM_GUARD_REFUSED, f"The connection was refused by the read-only guard ({exc.kind}): {exc}",
            params, evidence=[{"type": "guard_refusal", "reason": exc.kind, "details": exc.reasons[:20]}],
            next_step="Run the Ecommerce MCP server under the dedicated read-only Windows account (see "
                      "docs/ecommerce-agent.md). The guard cannot be bypassed or relaxed.")
        result.evaluated_at = self._clock().isoformat(timespec="seconds")
        return result

    def timeout_result(self, name: str, params: dict[str, Any] | None = None, *, hard_limit_seconds: float | None = None) -> DiagnosticResult:
        """The explicit "incomplete" result when a call ran out of time. With ``hard_limit_seconds`` it is the server's
        backstop: the caller stopped waiting, but the worker is NOT cancelled."""
        if hard_limit_seconds is None:
            return self._timeout_error(name, params or {})
        result = error_result(
            name, dx.ECOM_TIMEOUT, f"The server stopped waiting after {hard_limit_seconds:g} s, so this check has no result. This is an "
            "incomplete run, not a clean one. The background worker could not be cancelled: a read-only statement already sent may "
            "still be finishing, and its result will be discarded.", params or {},
            evidence=[{"type": "time_budget", "budget_seconds": self._budget, "hard_limit_seconds": hard_limit_seconds,
                       "completed": False, "worker_cancelled": False}],
            next_step="Run the smaller individual checks one at a time; nothing was changed in the database.")
        result.evaluated_at = self._clock().isoformat(timespec="seconds")
        return result

    def busy_result(self, name: str, params: dict[str, Any] | None = None, *, in_flight: int, capacity: int) -> DiagnosticResult:
        """Refusal when the worker cap is reached: no worker, connection or statement was started."""
        result = error_result(
            name, dx.ECOM_BUSY, f"{in_flight} of {capacity} diagnostic worker(s) are still running from earlier calls, so this call was "
            "not started and nothing was queried. This is an incomplete run, not a clean one.", params or {},
            evidence=[{"type": "worker_capacity", "in_flight": in_flight, "capacity": capacity, "started": False}],
            next_step="Wait for the earlier calls to finish (each is bounded by the time budget), then retry.")
        result.evaluated_at = self._clock().isoformat(timespec="seconds")
        return result

    def _timeout_error(self, name: str, params: dict[str, Any]) -> DiagnosticResult:
        result = error_result(
            name, dx.ECOM_TIMEOUT, "The time budget for this tool call ran out before this check finished, so it has no "
            "result. This is an incomplete run, not a clean one.", params,
            evidence=[{"type": "time_budget", "budget_seconds": self._budget, "completed": False}],
            next_step="Run the smaller individual checks one at a time; nothing was changed in the database.")
        result.evaluated_at = self._clock().isoformat(timespec="seconds")
        return result

    def _note_cleanup(self, conn: Any, results: list[DiagnosticResult]) -> None:
        """If closing the connection failed, say so on every result. The results themselves were computed before cleanup, so
        their status is unchanged, but the run is not described as fully clean-up-successful."""
        failures = list(getattr(conn, "cleanup_errors", None) or [])
        if not failures:
            return
        logger.warning("ecommerce connection cleanup failed (%s)", ", ".join(failures))
        for r in results:
            r.notes.append(f"{CLEANUP_NOTE} ({', '.join(failures)}); the result above was computed before cleanup.")

    def _safe(self, name: str, params: dict[str, Any], conn: GuardedConnection,
              fn: Callable[[dx.Context], DiagnosticResult],
              context: Callable[[], dx.Context] | None = None) -> DiagnosticResult:
        try:
            result = fn(context() if context else dx.open_context(conn, self._clock))
        except TimeBudgetExceeded:
            result = self._timeout_error(name, params)
        except InvalidInput as exc:
            result = error_result(name, DIAG_INVALID_INPUT, str(exc), params)
        except GuardRefusal as exc:
            result = self._guard_error(name, params, exc)
        except dx.SchemaMismatch as exc:
            result = error_result(name, dx.ECOM_SCHEMA_MISMATCH, f"The database does not match what this check needs: {exc}", params,
                                  next_step="Run check_ecommerce_target to see which tables are missing; this tool never changes a schema.")
        except dx.RuleConfigError as exc:
            result = error_result(name, dx.ECOM_RULE_CONFIG, f"The data-quality rule file cannot be used: {exc}", params,
                                  next_step="Fix config/data_quality_rules.yaml; the diagnostics do not guess missing rules.")
        except (SQLAlchemyError, ImportError):
            result = error_result(name, DIAG_DB_ERROR, "A database error stopped this diagnostic (details withheld).", params,
                                  next_step="Check that SQL Server is running and the read-only account can connect.")
        except Exception as exc:  # noqa: BLE001 - a bug in a check must not crash the server
            result = error_result(name, DIAG_INTERNAL, f"Unexpected internal error ({type(exc).__name__}).", params)
        logger.info("ecommerce diagnostic %s finished status=%s severity=%s code=%s",
                    name, result.status, result.severity, result.error_code)
        return result

    def _run(self, name: str, params: dict[str, Any], fn: Callable[[dx.Context], DiagnosticResult]) -> DiagnosticResult:
        try:
            with self._connect(deadline=self._deadline()) as conn:
                result = self._safe(name, params, conn, fn)
            self._note_cleanup(conn, [result])
            return result
        except TimeBudgetExceeded:
            return self._timeout_error(name, params)
        except GuardRefusal as exc:
            return self._guard_error(name, params, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ecommerce connection failed (%s)", type(exc).__name__)
            return error_result(name, DIAG_INTERNAL, f"Unexpected internal error ({type(exc).__name__}).", params)

    # --------------------------------------------------------------------------------------------- tools
    def check_ecommerce_target(self) -> DiagnosticResult:
        return self._run(dx.TARGET_NAME, {}, dx.check_ecommerce_target)

    def check_referential_integrity(self, relationship: str | None = None) -> DiagnosticResult:
        return self._run(dx.INTEGRITY_NAME, {"relationship": relationship},
                         lambda ctx: dx.check_referential_integrity(ctx, relationship))

    def check_delivery_completeness(self) -> DiagnosticResult:
        return self._run(dx.COMPLETENESS_NAME, {}, dx.check_delivery_completeness)

    def check_delivery_timeline(self) -> DiagnosticResult:
        return self._run(dx.TIMELINE_NAME, {}, dx.check_delivery_timeline)

    def check_missing_values(self, table: str | None = None) -> DiagnosticResult:
        return self._run(dx.MISSING_NAME, {"table": table}, lambda ctx: dx.check_missing_values(ctx, table))

    def check_duplicate_keys(self) -> DiagnosticResult:
        return self._run(dx.DUPLICATES_NAME, {}, dx.check_duplicate_keys)

    def profile_ecommerce_timestamps(self) -> DiagnosticResult:
        return self._run(dx.TIMESTAMPS_NAME, {}, dx.profile_ecommerce_timestamps)

    # ------------------------------------------------------------------------------------------ roll-up
    def run_all(self) -> DiagnosticReport:
        """Every check on ONE verified connection. If verification fails the report holds the single refusal."""
        steps: list[tuple[str, Callable[[dx.Context], DiagnosticResult]]] = [
            (dx.TARGET_NAME, dx.check_ecommerce_target), (dx.INTEGRITY_NAME, dx.check_referential_integrity),
            (dx.COMPLETENESS_NAME, dx.check_delivery_completeness), (dx.TIMELINE_NAME, dx.check_delivery_timeline),
            (dx.MISSING_NAME, dx.check_missing_values), (dx.DUPLICATES_NAME, dx.check_duplicate_keys),
            (dx.TIMESTAMPS_NAME, dx.profile_ecommerce_timestamps),
        ]
        try:
            with self._connect(deadline=self._deadline()) as conn:
                snapshot: list[Any] = []  # [Context] or [exception]: the catalogue and rule file are read once per sweep

                def context() -> dx.Context:
                    if not snapshot:
                        try:
                            snapshot.append(dx.open_context(conn, self._clock))
                        except Exception as exc:  # noqa: BLE001 - every check then reports the same failure
                            snapshot.append(exc)
                    if isinstance(snapshot[0], Exception):
                        raise snapshot[0]
                    return snapshot[0]

                results = []
                for name, fn in steps:
                    if results and results[-1].error_code == dx.ECOM_TIMEOUT:  # budget gone: do not start further checks
                        results.append(self._timeout_error(name, {}))
                    else:
                        results.append(self._safe(name, {}, conn, fn, context))
            self._note_cleanup(conn, results)
        except TimeBudgetExceeded:
            results = [self._timeout_error(dx.ALL_NAME, {})]
        except GuardRefusal as exc:
            results = [self._guard_error(dx.ALL_NAME, {}, exc)]
        except Exception as exc:  # noqa: BLE001
            results = [error_result(dx.ALL_NAME, DIAG_INTERNAL, f"Unexpected internal error ({type(exc).__name__}).", {})]
        stamp = next((r.evaluated_at for r in results if r.evaluated_at), None)
        return EcommerceReport(results, stamp)
