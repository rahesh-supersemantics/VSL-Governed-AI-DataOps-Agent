"""The recovery action allowlist, parameter schemas, executor and independent verifiers.

Only the actions in ``ACTIONS`` exist. Each has a strict parameter schema (unknown fields are rejected), a
risk class, a required approver permission, a precondition, the executor step and an independent
postcondition check that re-reads state instead of trusting the executor's return value.

There is no shell, no dynamic code, no free SQL and no MCP tool call here. The executor only calls two
``SimulationService`` methods. It never receives the read-only MCP tools, and those gain no write access.

Database safety: the executor refuses any repository that is not in-memory or a SQL repository whose
database name ends with ``_test``. ``dataops_sim`` (suffix ``_sim``) is never writable from here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from vsl_core.exceptions import AutomationDeniedException

from app.recovery.auth import approve_permission
from app.simulation.scenarios import SCENARIOS
from app.simulation.service import SimulationService

PipelineId = Literal["order_ingestion", "order_validation", "reporting_prep"]
ScenarioKey = Literal["pipeline-failure", "data-quality", "duplicate-records", "stale-data",
                      "incomplete-processing", "schema-mismatch"]


class RerunPipelineParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    pipeline_id: PipelineId


class RemoveInjectedConditionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scenario_key: ScenarioKey


class ActionRejected(Exception):
    """The proposed action or its parameters are not acceptable. The message is safe to show."""


class PreconditionFailed(Exception):
    """Raised BEFORE any side effect: the action cannot meaningfully run. Nothing was changed.
    ``check`` names the stage that refused: "target" (PN-04) or "precondition" (PN-06)."""

    def __init__(self, message: str = "", check: str = "precondition") -> None:
        super().__init__(message)
        self.check = check


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    params_model: type[BaseModel]
    risk: str  # low | medium | high
    description: str
    expected_effect: str
    target_of: Callable[[BaseModel], str]

    @property
    def permission(self) -> str:
        return approve_permission(self.action_type)


ACTIONS: dict[str, ActionSpec] = {s.action_type: s for s in [
    ActionSpec("rerun_pipeline", RerunPipelineParams, "medium",
               "Re-run one simulated pipeline for the current batch.",
               "A new run is recorded for the pipeline and finishes with status 'success'.",
               lambda p: f"pipeline:{p.pipeline_id}"),
    ActionSpec("remove_injected_condition", RemoveInjectedConditionParams, "high",
               "Restore one simulated failure scenario (remove its injected rows or clock shift).",
               "The scenario is no longer active and the rows it injected are gone.",
               lambda p: f"scenario:{p.scenario_key}"),
]}
RECOVERY_ACTIONS = frozenset(ACTIONS)


@dataclass(frozen=True)
class ValidatedAction:
    action_type: str
    parameters: dict[str, Any]
    target: str
    risk: str
    expected_effect: str


def validate_action(action_type: object, parameters: object) -> ValidatedAction:
    """Default deny: unknown action, non-object parameters, extra/missing/invalid fields are all rejected."""
    if not isinstance(action_type, str) or action_type not in ACTIONS:
        raise ActionRejected("unknown or non-allowlisted action type")
    if not isinstance(parameters, dict):
        raise ActionRejected("parameters must be an object")
    spec = ACTIONS[action_type]
    try:
        model = spec.params_model.model_validate(parameters)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "?" for e in exc.errors()})
        raise ActionRejected("invalid parameters: " + ", ".join(fields)) from None
    return ValidatedAction(action_type, model.model_dump(), spec.target_of(model), spec.risk, spec.expected_effect)


# --------------------------------------------------------------------------------------------- executor
class UnsafeRecoveryTarget(Exception):
    """The repository behind the simulation is not a permitted recovery target."""


PROTECTED_DATABASES = frozenset({"dataops_sim", "master", "model", "msdb", "tempdb"})
_DATABASE_KEYS = ("database", "initial catalog")
_SERVER_KEYS = ("server", "data source", "address", "addr", "network address")


def _odbc_fields(connection_string: str) -> dict[str, set[str]]:
    """Key -> every distinct value in an ODBC connection string (keys lower-cased, braces stripped)."""
    found: dict[str, set[str]] = {}
    for part in connection_string.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            found.setdefault(key.strip().lower(), set()).add(value.strip().strip("{}").strip().lower())
    return found


def resolve_connection_target(engine: Any) -> tuple[str, str]:
    """(server, database), lower-cased, taken from what the engine will really connect to.

    The application builds ``mssql+pyodbc:///?odbc_connect=<ODBC string>``; SQLAlchemy keeps that string opaque, so
    ``engine.url.database`` is ``None`` for it. The database therefore has to be read from the ODBC string itself.
    Ambiguous input (the same key given two different values) is refused rather than guessed at.
    """
    url = getattr(engine, "url", None)
    database = (getattr(url, "database", None) or "").strip().lower()
    server = (getattr(url, "host", None) or "").strip().lower()
    odbc = (getattr(url, "query", None) or {}).get("odbc_connect")
    if isinstance(odbc, str) and odbc:
        fields = _odbc_fields(odbc)
        for keys, current in ((_DATABASE_KEYS, "database"), (_SERVER_KEYS, "server")):
            values = {v for k in keys for v in fields.get(k, ())}
            if len(values) > 1:
                raise UnsafeRecoveryTarget(f"the connection string names more than one {current}")
            if values:
                value = values.pop()
                if current == "database":
                    if database and database != value:
                        raise UnsafeRecoveryTarget("the URL and the connection string name different databases")
                    database = value
                else:
                    server = server or value
    return server, database


def normalise_allowlist(entries: Iterable[str] | None) -> frozenset[str]:
    """Allowlist entries are ``database`` or ``server/database`` (lower-cased). Blank entries are dropped."""
    return frozenset(e.strip().lower() for e in (entries or ()) if e and e.strip())


def assert_recovery_target(service: SimulationService, allowed_targets: Iterable[str] | None = None) -> None:
    """Allow in-memory repositories, and SQL repositories only when BOTH hold for the database the engine really uses:
    the name ends with ``_test`` AND it is explicitly allowlisted (``database`` or ``server/database``).

    Fails closed: no allowlist, an unresolvable name, a protected name or any ambiguity raises."""
    repo = service.repo
    engine = getattr(repo, "engine", None)
    if engine is None:
        if type(repo).__module__.startswith("app.db"):
            raise UnsafeRecoveryTarget("SQL repository without an engine")
        return  # in-memory / fake repository
    server, database = resolve_connection_target(engine)
    if not database:
        raise UnsafeRecoveryTarget("the database this connection uses could not be determined")
    if database in PROTECTED_DATABASES or not database.endswith("_test"):
        raise UnsafeRecoveryTarget("recovery writes are only permitted on a database whose name ends with _test")
    allowed = normalise_allowlist(allowed_targets)
    if not allowed:
        raise UnsafeRecoveryTarget("no recovery target is allowlisted (set RECOVERY_TARGET_ALLOWLIST)")
    if database not in allowed and not (server and f"{server}/{database}" in allowed):
        raise UnsafeRecoveryTarget("the database is not on the recovery target allowlist")


def assert_live_target(service: SimulationService) -> None:
    """Immediately before a side effect: ask the server which database the connection is really on and require it to be
    the one that was validated. Repositories that cannot answer (in-memory fakes) are skipped."""
    probe = getattr(service.repo, "current_database", None)
    engine = getattr(service.repo, "engine", None)
    if probe is None or engine is None:
        return
    _, expected = resolve_connection_target(engine)
    try:
        actual = (probe() or "").strip().lower()
    except AutomationDeniedException:
        raise
    except Exception:  # noqa: BLE001 - cannot verify, so do not act
        raise PreconditionFailed("the connected database could not be verified", "target") from None
    if not actual or actual != expected:
        raise PreconditionFailed("the connected database does not match the validated recovery target", "target")


@dataclass
class ExecutionResult:
    detail: dict[str, Any]
    verified: bool
    verification: str


class RecoveryExecutor:
    """Runs one validated action against the simulation and verifies the postcondition independently."""

    def __init__(self, service: SimulationService, allowed_targets: Iterable[str] | None = None) -> None:
        assert_recovery_target(service, allowed_targets)
        self.service = service
        self.allowed_targets = normalise_allowlist(allowed_targets)

    def check_precondition(self, action: ValidatedAction) -> dict[str, Any]:
        """Read-only. Returns facts needed by ``verify``; raises ``PreconditionFailed`` if the action cannot run."""
        svc = self.service
        try:
            assert_recovery_target(svc, self.allowed_targets)  # revalidated at execution time, not only at start-up
        except UnsafeRecoveryTarget:
            raise PreconditionFailed("the recovery target is no longer permitted", "target") from None
        assert_live_target(svc)
        if svc.repo.get_state() is None:
            raise PreconditionFailed("the simulation is not seeded")
        if action.action_type == "rerun_pipeline":
            if svc.repo.get_pipeline(action.parameters["pipeline_id"]) is None:
                raise PreconditionFailed("pipeline does not exist")
            return {}
        scenario = SCENARIOS[action.parameters["scenario_key"]]
        row = svc.repo.get_scenarios().get(scenario.scenario_id)
        if not row or row["status"] != "active":
            raise PreconditionFailed("the scenario is not active, so there is nothing to remove")
        details = json.loads(row["details_json"])
        return {"scenario_id": scenario.scenario_id, "tagged_orders_before": svc.repo.tagged_row_counts()["orders"],
                "injected": int(details.get("injected_orders", 0))}

    def execute(self, action: ValidatedAction, facts: dict[str, Any]) -> dict[str, Any]:
        svc = self.service
        if action.action_type == "rerun_pipeline":
            outcome = svc.run_pipeline(action.parameters["pipeline_id"])
            return {"run_id": outcome["run"]["run_id"]}
        result = svc.restore(action.parameters["scenario_key"])
        return {"changed": result.changed}

    def verify(self, action: ValidatedAction, facts: dict[str, Any], detail: dict[str, Any]) -> ExecutionResult:
        """Independent postcondition: re-read state; do not trust ``execute``'s return value."""
        svc = self.service
        if action.action_type == "rerun_pipeline":
            run = svc.repo.get_run(detail.get("run_id", ""))
            ok = bool(run) and run["pipeline_id"] == action.parameters["pipeline_id"] and run["status"] == "success"
            return ExecutionResult(detail, ok, f"run status is {run['status'] if run else 'missing'}")
        scenario_id = facts["scenario_id"]
        still_active = scenario_id in svc.active_scenario_ids()
        tagged_after = svc.repo.tagged_row_counts()["orders"]
        ok = (not still_active) and tagged_after == facts["tagged_orders_before"] - facts["injected"]
        return ExecutionResult(detail, ok, "scenario inactive and injected rows removed" if ok
                               else "scenario still active or injected rows remain")
