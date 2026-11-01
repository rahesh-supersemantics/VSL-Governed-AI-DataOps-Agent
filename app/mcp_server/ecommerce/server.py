"""The Ecommerce MCP server: eight read-only, counts-only tools over ``dataops_ecommerce_demo``.

Separate from the simulation server (``app.mcp_server``): different server name, different tools, different target. Every
tool is a one-line adapter over ``EcommerceDiagnosticsService``, which opens a *verified* read-only connection per call.
There is deliberately no SQL, shell, file, write or recovery tool, and no tool takes a database name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable

import anyio
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from app.diagnostics import DiagnosticReport, DiagnosticResult
from app.ecommerce import diagnostics as dx
from app.ecommerce.service import EcommerceDiagnosticsService, EcommerceReport
from app.ecommerce.target import (
    HARD_LIMIT_MARGIN_SECONDS,
    MAX_IN_FLIGHT_WORKERS,
    SERVER_NAME,
    SERVER_VERSION,
    TOOL_BUDGET_SECONDS,
    validate_timeouts,
)
from app.mcp_server.ecommerce.schemas import (
    EcommerceReportModel,
    EcommerceResultModel,
    RelationshipArg,
    TableArg,
)
from app.mcp_server.server import READ_ONLY, _RejectUnknownArguments

logger = logging.getLogger("app.mcp_server.ecommerce")

INSTRUCTIONS = (
    "Read-only, counts-only diagnostics for the Olist e-commerce demonstration database (dataops_ecommerce_demo). "
    "Every call first verifies the database, the Windows account and its read-only permissions; if that fails the tool "
    "returns an ECOM-GUARD-REFUSED error and nothing is queried. Results contain table names, rule ids and counts, never "
    "record identifiers or text. A count is a confirmed fact; why it happens is a hypothesis. Late delivery is a business "
    "signal, not data corruption. Duplicate review_id groups are a pending investigation, not a confirmed defect. "
    "Start with check_ecommerce_target, then drill in, or use run_ecommerce_diagnostics for an overview."
)

TOOL_CATALOGUE = {
    dx.TARGET_NAME: "Verified target database, account, read-only status, table presence and row counts.",
    dx.INTEGRITY_NAME: "Orphaned references: orders, items, payments and reviews against their parents.",
    dx.COMPLETENESS_NAME: "Delivered orders missing a customer or carrier delivery date.",
    dx.TIMELINE_NAME: "Delivery before purchase (integrity) and delivery later than estimated (business signal).",
    dx.MISSING_NAME: "Null key columns (violations) and null counts per column (observations).",
    dx.DUPLICATES_NAME: "Candidate-key uniqueness; review_id duplicates reported as a pending investigation.",
    dx.TIMESTAMPS_NAME: "Aggregate minimum/maximum dates of the timestamp columns (no freshness verdict).",
    dx.ALL_NAME: "All of the above on one verified connection, as a roll-up.",
}


def _text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


def _wrap(result: DiagnosticResult) -> CallToolResult:
    data = result.to_dict()
    return CallToolResult(content=[TextContent(text=_text(data))], structured_content=data, is_error=result.status == "error")


def _wrap_report(report: DiagnosticReport) -> CallToolResult:
    data = report.to_dict()
    return CallToolResult(content=[TextContent(text=_text(data))], structured_content=data,
                          is_error=report.overall_status == "error")


class WorkerGate:
    """Caps the diagnostic workers that are running at once.

    A slot is taken before a worker thread is started and given back by the worker itself when its function returns or
    raises. It is NOT given back when the caller stops waiting: Python cannot cancel a thread, so an abandoned worker keeps
    its slot until it really ends. When no slot is free the call is refused and nothing is started.
    """

    def __init__(self, capacity: int = MAX_IN_FLIGHT_WORKERS) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("invalid Ecommerce worker capacity: it must be a whole number of at least 1")
        self.capacity = capacity
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self.capacity:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)


async def run_in_worker(fn: Callable[[], Any], gate: WorkerGate) -> Any:
    """Run ``fn`` on its own daemon thread. The caller may stop waiting (cancellation); the thread then keeps running to its
    own end, holds its gate slot until then, and its result is discarded. The caller must have acquired a slot."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def deliver(result: Any, error: BaseException | None) -> None:
        if future.cancelled():  # the caller already gave up: a late result must not reach anyone
            return
        future.set_exception(error) if error is not None else future.set_result(result)

    def work() -> None:
        result, error = None, None
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting caller if it is still there
            error = exc
        finally:
            gate.release()  # the slot is freed only here, when the worker has really finished
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:  # the event loop is already closed
            pass

    try:
        threading.Thread(target=work, name="ecommerce-diagnostic-worker", daemon=True).start()
    except BaseException:
        gate.release()  # the thread never started, so nothing holds the slot
        raise
    return await future


def create_ecommerce_server(service: EcommerceDiagnosticsService | None = None, *,
                            hard_limit_seconds: float = TOOL_BUDGET_SECONDS + HARD_LIMIT_MARGIN_SECONDS,
                            safety_margin_seconds: float = HARD_LIMIT_MARGIN_SECONDS,
                            max_in_flight: int = MAX_IN_FLIGHT_WORKERS) -> MCPServer:
    """``hard_limit_seconds`` is the server's own end-to-end bound on how long a CALLER waits. The service stops
    cooperatively at its time budget; this limit answers with an explicit ECOM-TIMEOUT error even if a statement ignores
    its timeout. It does not cancel anything: the worker keeps running to its own end and holds one of ``max_in_flight``
    slots until then. Construction fails unless ``service budget + safety_margin_seconds <= hard_limit_seconds``. The
    hard limit must also stay below the agent's tool timeout (the default is 60 s)."""
    service = service or EcommerceDiagnosticsService()
    validate_timeouts(service.budget_seconds, hard_limit_seconds, safety_margin_seconds)
    gate = WorkerGate(max_in_flight)
    guard = _RejectUnknownArguments()
    server = MCPServer(SERVER_NAME, title="DataOps Ecommerce Diagnostics (read-only)", instructions=INSTRUCTIONS,
                       version=SERVER_VERSION, middleware=[guard])
    guard.server = server
    server.worker_gate = gate  # type: ignore[attr-defined]  # exposed for tests and diagnostics

    async def run(name: str, call: Callable[[EcommerceDiagnosticsService], Any], *, report: bool = False) -> CallToolResult:
        def wrap_error(result: DiagnosticResult) -> CallToolResult:
            return _wrap_report(EcommerceReport([result], result.evaluated_at)) if report else _wrap(result)

        if not gate.try_acquire():  # capacity exhausted by workers that are still running: start nothing
            logger.warning("ecommerce tool %s refused: %d of %d workers still running", name, gate.in_flight, gate.capacity)
            return wrap_error(service.busy_result(name, in_flight=gate.in_flight, capacity=gate.capacity))
        try:
            with anyio.fail_after(hard_limit_seconds):
                outcome = await run_in_worker(lambda: call(service), gate)
        except TimeoutError:
            # The caller stops waiting. The worker is NOT cancelled: it can only finish its read-only statement(s) within
            # their own capped timeouts, keeps its slot until it ends, and its result is discarded.
            logger.warning("ecommerce tool %s: caller stopped waiting at the hard limit; worker still running (%d of %d slots)",
                           name, gate.in_flight, gate.capacity)
            return wrap_error(service.timeout_result(name, hard_limit_seconds=hard_limit_seconds))
        return _wrap_report(outcome) if report else _wrap(outcome)

    def tool(name: str):
        return server.tool(name=name, description=None, annotations=READ_ONLY, structured_output=True)

    @tool(dx.TARGET_NAME)
    async def check_ecommerce_target() -> EcommerceResultModel:
        """Confirm the target before anything else: database, verified read-only Windows account, table presence, row counts.

        Run this first. If it reports ECOM-GUARD-REFUSED, no other tool will work and no data was read.
        """
        return await run("check_ecommerce_target", lambda s: s.check_ecommerce_target())

    @tool(dx.INTEGRITY_NAME)
    async def check_referential_integrity(relationship: RelationshipArg | None = None) -> EcommerceResultModel:
        """Count rows that reference a missing parent: orders->customers, items->orders/products/sellers, payments->orders, reviews->orders.

        Counts only. NULL references are not counted as orphans.
        """
        return await run("check_referential_integrity", lambda s: s.check_referential_integrity(relationship))

    @tool(dx.COMPLETENESS_NAME)
    async def check_delivery_completeness() -> EcommerceResultModel:
        """Count delivered orders with no customer delivery date and delivered orders with no carrier delivery date."""
        return await run("check_delivery_completeness", lambda s: s.check_delivery_completeness())

    @tool(dx.TIMELINE_NAME)
    async def check_delivery_timeline() -> EcommerceResultModel:
        """Count orders delivered before they were purchased (integrity) and delivered orders that arrived after the
        estimated date (a business signal, reported as a warning, never as an integrity failure)."""
        return await run("check_delivery_timeline", lambda s: s.check_delivery_timeline())

    @tool(dx.MISSING_NAME)
    async def check_missing_values(table: TableArg | None = None) -> EcommerceResultModel:
        """Count null key columns (violations) and report null counts per column (observations, not violations)."""
        return await run("check_missing_values", lambda s: s.check_missing_values(table))

    @tool(dx.DUPLICATES_NAME)
    async def check_duplicate_keys() -> EcommerceResultModel:
        """Count duplicate groups on each table's candidate key. Duplicate review_id groups are reported as a
        pending investigation (the rule stays disabled in the configuration), not as a failure."""
        return await run("check_duplicate_keys", lambda s: s.check_duplicate_keys())

    @tool(dx.TIMESTAMPS_NAME)
    async def profile_ecommerce_timestamps() -> EcommerceResultModel:
        """Minimum and maximum of each timestamp column. Aggregate dates only; no freshness verdict (historical dataset)."""
        return await run("profile_ecommerce_timestamps", lambda s: s.profile_ecommerce_timestamps())

    @tool(dx.ALL_NAME)
    async def run_ecommerce_diagnostics() -> EcommerceReportModel:
        """Run every Ecommerce check on one verified connection and return a roll-up with every individual result.

        It counts findings; it never names a root cause.
        """
        return await run(dx.ALL_NAME, lambda s: s.run_all(), report=True)

    return server
