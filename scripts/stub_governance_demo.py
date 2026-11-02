"""STUBBED browser verification of the real Operations Dashboard governance panel. NOT live, NOT end to end.

    python -m scripts.stub_governance_demo            # stub API on 8600, real dashboard code on 8601

It starts (1) a tiny READ-ONLY stub API of canned, clearly marked "[STUB]" incidents and (2) the real ``dashboard/ops_app.py``
pointed at that stub, on ports of its own. It never touches the real API (8000), dashboard (8501) or explorer (8502), any database,
approval store, ledger, ``.env`` or model, and it refuses to start if a port is busy (it kills nothing). Every write (POST) is
refused with 405. The governance views are built by the real ``build_governance_view`` from SYNTHETIC ledger outcomes, so what the
panel shows is the real derivation applied to invented inputs; no result here describes a real recovery.
Stop with Ctrl+C. Open http://127.0.0.1:8601 and choose Recovery & approvals.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
STUB_TOKEN = "stub-token-not-a-credential"
REAL_PORTS = (8000, 8501, 8502)
O = lambda *names: [{"outcome": n} for n in names]  # noqa: E731  (synthetic ledger payloads)

PROPOSED = ("recovery_proposed", "approval_requested")
GRANTED = (*PROPOSED, "approval_granted", "authorised")
ATTEMPT = (*GRANTED, "recovery_attempt", "idempotency_claim_acquired")
# id -> (title, recovery status, verified_outcome, approval status, expiry offset minutes, outcomes, view mode, extra)
SCENARIOS: dict[str, dict[str, Any]] = {
    "stub-1": dict(title="[STUB] Verified success", status="executed", verified="verified", approval="consumed", expires=+20,
                   outcomes=(*ATTEMPT, "recovery_succeeded"), decided_by="stub.approver"),
    "stub-2": dict(title="[STUB] Pending, nothing authorised or attempted", status="awaiting_approval", verified="not_applicable",
                   approval="pending", expires=+25, outcomes=PROPOSED),
    "stub-3": dict(title="[STUB] Ran, verification FAILED, escalated", status="recovery_verification_failed", verified="failed",
                   approval="consumed", expires=+20, outcomes=(*ATTEMPT, "recovery_verification_failed", "compensation_plan_proposed",
                                                              "recovery_escalated"), decided_by="stub.approver"),
    "stub-4": dict(title="[STUB] Outcome UNKNOWN (uncertain)", status="recovery_uncertain", verified="unknown", approval="consumed",
                   expires=+20, outcomes=(*ATTEMPT, "recovery_uncertain", "recovery_escalated"), decided_by="stub.approver"),
    "stub-5": dict(title="[STUB] Expired before any decision (stored as pending)", status="awaiting_approval", verified="not_applicable",
                   approval="pending", expires=-120, outcomes=PROPOSED),
    "stub-6": dict(title="[STUB] EMPTY governance response", status="awaiting_approval", verified="not_applicable", approval="pending",
                   expires=+25, outcomes=PROPOSED, view="empty"),
    "stub-7": dict(title="[STUB] PARTIAL governance response (lifecycle only)", status="awaiting_approval", verified="not_applicable",
                   approval="pending", expires=+25, outcomes=PROPOSED, view="partial"),
    "stub-8": dict(title="[STUB] Blocked: duplicate operation", status="blocked", verified="not_applicable", approval="approved",
                   expires=+20, outcomes=(*GRANTED, "recovery_attempt", "recovery_blocked"), decided_by="stub.approver",
                   payloads=[{"outcome": o} for o in (*GRANTED, "recovery_attempt")]
                   + [{"outcome": "recovery_blocked", "idempotency_key": "idem-stub"}]),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _approval(sid: str) -> dict[str, Any]:
    s = SCENARIOS[sid]
    from app.recovery.expiry import effective_status
    raw = {"request_id": f"apr-{sid}", "incident_id": sid, "action_type": "remove_injected_condition",
           "parameters": {"scenario_key": "stub-scenario"}, "target": "scenario:stub-scenario", "evidence_refs": ["E1"],
           "reason": "STUB DATA - not a real request", "expected_effect": "(stub)", "risk": "high", "governance_state": "ok",
           "report_status": "issue_identified", "binding_digest": "0" * 64, "created_at": _ts(_now() - timedelta(minutes=5)),
           "expires_at": _ts(_now() + timedelta(minutes=s["expires"])), "status": s["approval"], "decided_by": s.get("decided_by"),
           "decision_reason": "stub" if s.get("decided_by") else None}
    from app.api.v1.schemas import ApprovalOut
    return ApprovalOut(**raw, effective_status=effective_status(raw, _now())).model_dump()


def _recovery(sid: str, *, detail: bool = True) -> dict[str, Any]:
    s = SCENARIOS[sid]
    from app.api.v1.schemas import RecoveryOut
    from app.services.status import recovery_phase
    approval = _approval(sid)
    row = {"status": s["status"], "verified_outcome": s["verified"]}
    phase = recovery_phase(row, approval_expired=approval["effective_status"] == "expired" and s["status"] == "awaiting_approval")
    return RecoveryOut(recovery_id=f"rec-{sid}", incident_id=sid, investigation_id=None, status=s["status"], phase=phase,
                       execution_status="not_run" if s["status"] in ("awaiting_approval", "blocked") else s["status"],
                       verified_outcome=s["verified"], action_type="remove_injected_condition", parameters={"scenario_key": "stub-scenario"},
                       reasons=["duplicate_or_unreconciled_operation"] if s["status"] == "blocked" else [], approval_request_id=f"apr-{sid}",
                       decision_id=f"dec-{sid}", requested_by="stub.operator", created_at=_ts(_now() - timedelta(minutes=5)),
                       updated_at=_ts(_now()), approval=approval if detail else None).model_dump()


def _incident(sid: str) -> dict[str, Any]:
    from app.api.v1.schemas import IncidentOut
    rec = _recovery(sid, detail=False)
    return IncidentOut(incident_id=sid, external_ref=None, title=SCENARIOS[sid]["title"], description="Canned stub incident. Not real data.",
                       incident_type="stub", severity="high", status="investigated", workflow_status=rec["phase"], created_by="stub",
                       created_at=_ts(_now() - timedelta(minutes=10)), updated_at=_ts(_now())).model_dump()


def _governance(sid: str) -> dict[str, Any]:
    from app.recovery.governance_view import build_governance_view
    s = SCENARIOS[sid]
    rec = _recovery(sid)
    if s.get("view") == "empty":
        return {}
    payloads = s.get("payloads") or [{"outcome": o} for o in s["outcomes"]]
    view = build_governance_view(rec, rec["approval"], [{"outcome": p["outcome"]} for p in payloads], None, payloads, now=_now())
    if s.get("view") == "partial":
        return {"recovery_id": view["recovery_id"], "lifecycle": view["lifecycle"]}
    return view


def route(path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
    p = path.rstrip("/")
    if p in ("/api/v1/health", "/api/v1/ready"):
        return 200, {"status": "ok", "service": "STUB API"}
    if p == "/api/v1/incidents":
        items = [_incident(k) for k in SCENARIOS]
        return 200, {"items": items, "total": len(items), "limit": len(items), "offset": 0}
    parts = p.split("/")  # '', api, v1, ...
    if len(parts) == 6 and parts[3] == "incidents" and parts[5] == "recoveries" and parts[4] in SCENARIOS:
        return 200, [_recovery(parts[4], detail=False)]
    if len(parts) == 5 and parts[3] == "recoveries" and parts[4].startswith("rec-") and parts[4][4:] in SCENARIOS:
        return 200, _recovery(parts[4][4:])
    if len(parts) == 6 and parts[3] == "recoveries" and parts[5] == "governance" and parts[4][4:] in SCENARIOS:
        return 200, _governance(parts[4][4:])
    return 404, {"error": {"code": "not_found", "message": "Not part of the stub.", "request_id": "stub"}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {STUB_TOKEN}":
            return self._send(401, {"error": {"code": "unauthenticated", "message": "Stub: token required.", "request_id": "stub"}})
        url = urlparse(self.path)
        self._send(*route(url.path, parse_qs(url.query)))

    def _refuse(self) -> None:
        self._send(405, {"error": {"code": "stub_read_only", "message": "The stub API is read-only; nothing was changed.", "request_id": "stub"}})

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

    def log_message(self, *args: Any) -> None:  # quiet, and never logs headers
        return


def busy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.stub_governance_demo", description=__doc__.splitlines()[0])
    ap.add_argument("--stub-port", type=int, default=8600)
    ap.add_argument("--ui-port", type=int, default=8601)
    a = ap.parse_args()
    for port in (a.stub_port, a.ui_port):
        if port in REAL_PORTS:
            print(f"Refusing: port {port} belongs to the real stack.")
            return 2
        if busy(port):
            print(f"Refusing: port {port} is already in use (nothing was stopped). Choose another with --stub-port / --ui-port.")
            return 2
    server = ThreadingHTTPServer(("127.0.0.1", a.stub_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {**os.environ, "DASHBOARD_API_BASE_URL": f"http://127.0.0.1:{a.stub_port}/api/v1", "DASHBOARD_API_TOKEN": STUB_TOKEN,
           "PYTHONPATH": str(ROOT), "OPS_DASHBOARD_URL": f"http://127.0.0.1:{a.ui_port}"}
    for key in ("DASHBOARD_INVESTIGATE_TOKEN", "LEDGER_EXPLORER_API_TOKEN", "OPENAI_API_KEY"):
        env.pop(key, None)
    cmd = [sys.executable, "-m", "streamlit", "run", "dashboard/ops_app.py", "--server.port", str(a.ui_port), "--server.address", "127.0.0.1",
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    print(f"STUB API on 127.0.0.1:{a.stub_port} (read-only, canned). Real dashboard code on http://127.0.0.1:{a.ui_port}. Ctrl+C to stop.")
    child = subprocess.Popen(cmd, cwd=ROOT, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
