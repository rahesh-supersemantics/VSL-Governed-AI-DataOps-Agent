"""T-15 (offline mode makes no external model call) and T-20 preparation: model selection is mocked, nothing is sent. Offline."""
import socket

import pytest

import app.agent.llm as llm_module
from app.agent import OfflineHeuristicModel
from tests import offline_network_guard
from tests.agent_support import apply_scenario, run_agent, server_for


def test_an_offline_investigation_opens_no_network_connection_and_never_builds_the_live_client(monkeypatch):
    offline_network_guard.install(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("the live OpenAI client was constructed in offline mode")
    monkeypatch.setattr(llm_module.OpenAIChatModel, "__init__", boom)
    from app.simulation.service import SimulationService  # noqa: F401  (the same simulation the other governance tests use)
    from tests.recovery_support import make_env
    import tempfile, pathlib
    env = make_env(pathlib.Path(tempfile.mkdtemp()))
    apply_scenario(env.sim, "duplicate-records")
    report = run_agent(server_for(env.sim), "orders look wrong", OfflineHeuristicModel())
    assert report.mode == "offline" and report.model


def test_the_guard_really_refuses_an_outbound_connection(monkeypatch):
    offline_network_guard.install(monkeypatch)
    with pytest.raises(AssertionError, match=offline_network_guard.NETWORK_ATTEMPT):
        socket.create_connection(("203.0.113.1", 443), timeout=1)


def test_the_smoke_script_sends_only_a_fixed_prompt_and_is_refused_without_confirmation(monkeypatch, capsys):
    import sys
    import scripts.openai_smoke as smoke
    from app.agent.prompts import SMOKE_SYSTEM
    constructed = []
    monkeypatch.setattr(smoke, "OpenAIChatModel", lambda cfg: constructed.append(cfg))
    monkeypatch.setattr(sys, "argv", ["openai_smoke"])
    assert smoke.main() != 0 and constructed == []  # no confirmation flag: no client, no request
    assert SMOKE_SYSTEM == 'Reply with the JSON object {"ok": true}.'
    src = open(smoke.__file__, encoding="utf-8").read()
    assert 'complete_json("smoke", SMOKE_SYSTEM, {"instruction": "Reply with ok=true."}, SMOKE_SCHEMA)' in src  # the only payload
