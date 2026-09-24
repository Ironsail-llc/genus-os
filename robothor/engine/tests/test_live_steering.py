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


async def test_runner_registers_the_live_session():
    """Registration and cleanup cover the loop, including exceptional exits."""
    from robothor.engine import runner
    from robothor.engine.context_control import control
    from robothor.engine.request_runtime import observe_request

    assert "async with observe_request(session, on_status) as report_status" in inspect.getsource(
        runner
    )
    session = AgentSession(agent_id="a")
    with pytest.raises(RuntimeError):
        async with observe_request(session, None):
            assert session_registry.lookup(session.run_id) is session
            assert control.get() is not None
            raise RuntimeError("loop interrupted")
    assert session_registry.lookup(session.run_id) is None
    assert control.get() is None


def test_the_loop_consumes_steers_and_interrupts():
    """These moved out of `_run_loop` into `loop_guards` when that 1,059-line
    method was decomposed. The check follows the code rather than being
    dropped: what matters is that SOMETHING on the loop's path consumes them,
    not which file it lives in."""
    from robothor.engine import live_inbox, loop_guards

    src = inspect.getsource(loop_guards)
    assert "absorb_live_input(session)" in src
    assert "consume_pending_steer()" in inspect.getsource(live_inbox)
    assert "consume_interrupt()" in src


def test_the_runner_actually_calls_those_guards():
    """The other half: a guard module nothing invokes is the inert-control
    pattern this codebase has shipped six times."""
    from robothor.engine import runner

    assert "check_iteration_guards(" in inspect.getsource(runner)


def test_health_exposes_steer_and_interrupt_routes():
    from robothor.engine import health

    src = inspect.getsource(health)
    assert "/api/runs/{run_id}/steer" in src
    assert "/api/runs/{run_id}/interrupt" in src
