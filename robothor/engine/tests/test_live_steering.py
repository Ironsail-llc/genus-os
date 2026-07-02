"""Live steering wired into the runner (Wave-1 hardening, PR-14).

The interrupt/steer machinery (session_registry + AgentSession.consume_*) was
fully built but the runner never registered a session or consumed steer/interrupt,
so every external lookup returned None. This wires register/consume into the run
loop and exposes /api/runs/{id}/steer + /interrupt.
"""

from __future__ import annotations

import inspect

import pytest

from robothor.engine import session_registry
from robothor.engine.interrupt_api import interrupt_session, steer_session
from robothor.engine.session import AgentSession


@pytest.fixture(autouse=True)
def _clear_registry():
    for rid in session_registry.active_run_ids():
        session_registry.unregister(rid)
    yield
    for rid in session_registry.active_run_ids():
        session_registry.unregister(rid)


def test_steer_chain_injects_text():
    s = AgentSession(agent_id="a")
    session_registry.register(s)
    assert steer_session(s.run_id, "focus on X") is True
    assert s.consume_pending_steer() == "focus on X"


def test_interrupt_chain_sets_message():
    s = AgentSession(agent_id="a")
    session_registry.register(s)
    assert interrupt_session(s.run_id, "stop now") is True
    assert s.consume_interrupt() == "stop now"


def test_steer_missing_session_returns_false():
    assert steer_session("no-such-run", "x") is False


def test_runner_registers_and_consumes():
    from robothor.engine import runner

    src = inspect.getsource(runner)
    assert "session_registry.register(session)" in src
    assert "session_registry.unregister(session)" in src
    assert "consume_pending_steer()" in src
    assert "consume_interrupt()" in src


def test_health_exposes_steer_and_interrupt_routes():
    from robothor.engine import health

    src = inspect.getsource(health)
    assert "/api/runs/{run_id}/steer" in src
    assert "/api/runs/{run_id}/interrupt" in src
