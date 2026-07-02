"""Tests for Rip 9 interrupt/steer + session_registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import session_registry
from robothor.engine.interrupt_api import interrupt_session, steer_session
from robothor.engine.models import RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Tests share module-level state; reset between cases."""
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)
    yield
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)


class TestSessionRegistry:
    def test_register_and_lookup(self) -> None:
        s = AgentSession(agent_id="test")
        session_registry.register(s)
        assert session_registry.lookup(s.run_id) is s

    def test_unregister_idempotent(self) -> None:
        s = AgentSession(agent_id="test")
        session_registry.register(s)
        session_registry.unregister(s)
        session_registry.unregister(s)  # no error
        assert session_registry.lookup(s.run_id) is None

    def test_unregister_by_run_id_string(self) -> None:
        s = AgentSession(agent_id="test")
        session_registry.register(s)
        session_registry.unregister(s.run_id)
        assert session_registry.lookup(s.run_id) is None

    def test_active_count(self) -> None:
        assert session_registry.active_count() == 0
        a = AgentSession(agent_id="a")
        b = AgentSession(agent_id="b")
        session_registry.register(a)
        session_registry.register(b)
        assert session_registry.active_count() == 2


class TestAgentSessionInterrupt:
    def test_default_no_interrupt(self) -> None:
        s = AgentSession(agent_id="test")
        assert s._interrupt_requested is False
        assert s.consume_interrupt() is None

    def test_interrupt_with_message(self) -> None:
        s = AgentSession(agent_id="test")
        s.interrupt("stop and check email")
        assert s._interrupt_requested is True
        msg = s.consume_interrupt()
        assert msg == "stop and check email"
        # Flag clears after consume.
        assert s._interrupt_requested is False
        assert s.consume_interrupt() is None

    def test_interrupt_without_message(self) -> None:
        s = AgentSession(agent_id="test")
        s.interrupt()
        assert s._interrupt_requested is True
        # Empty-string sentinel — consumed but caller sees ''.
        msg = s.consume_interrupt()
        assert msg == ""

    def test_interrupt_idempotent_with_newer_message(self) -> None:
        s = AgentSession(agent_id="test")
        s.interrupt("first")
        s.interrupt("second")
        # The newer message wins.
        assert s.consume_interrupt() == "second"


class TestAgentSessionSteer:
    def test_default_no_steer(self) -> None:
        s = AgentSession(agent_id="test")
        assert s.consume_pending_steer() is None

    def test_steer_single(self) -> None:
        s = AgentSession(agent_id="test")
        s.steer("focus on the budget question")
        assert s.consume_pending_steer() == "focus on the budget question"
        assert s.consume_pending_steer() is None

    def test_steer_concatenates_when_pending(self) -> None:
        s = AgentSession(agent_id="test")
        s.steer("first guidance")
        s.steer("more context")
        text = s.consume_pending_steer()
        assert "first guidance" in text
        assert "more context" in text

    def test_empty_steer_is_noop(self) -> None:
        s = AgentSession(agent_id="test")
        s.steer("")
        assert s.consume_pending_steer() is None

    def test_steer_is_orthogonal_to_interrupt(self) -> None:
        s = AgentSession(agent_id="test")
        s.steer("course-correct")
        assert s._interrupt_requested is False
        assert s.consume_pending_steer() == "course-correct"


class TestPublicHelpers:
    def test_interrupt_session_for_active_run(self) -> None:
        s = AgentSession(agent_id="test")
        session_registry.register(s)
        ok = interrupt_session(s.run_id, "halt")
        assert ok is True
        assert s.consume_interrupt() == "halt"

    def test_interrupt_session_for_unknown_run(self) -> None:
        ok = interrupt_session("no-such-run", "halt")
        assert ok is False

    def test_steer_session_for_active_run(self) -> None:
        s = AgentSession(agent_id="test")
        session_registry.register(s)
        ok = steer_session(s.run_id, "nudge")
        assert ok is True
        assert s.consume_pending_steer() == "nudge"

    def test_steer_session_for_unknown_run(self) -> None:
        ok = steer_session("no-such-run", "nudge")
        assert ok is False


class TestInterruptTerminalState:
    def test_session_cancelled_helper(self) -> None:
        s = AgentSession(agent_id="test")
        assert s.was_interrupted is False
        s.mark_interrupted("operator stop")
        assert s.was_interrupted is True
        run = s.cancelled(s._interrupt_note)
        assert run.status == RunStatus.CANCELLED
        assert "operator stop" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_execute_finalizes_interrupt_as_cancelled(
        self, engine_config, sample_agent_config, mock_litellm_response
    ):
        """When the loop honors an interrupt, execute() returns a CANCELLED run
        and skips the verifier — not a COMPLETED run."""
        with patch("robothor.engine.runner.get_registry") as mock_reg:
            reg = MagicMock()
            reg.build_for_agent.return_value = []
            reg.get_tool_names.return_value = []
            mock_reg.return_value = reg
            runner = AgentRunner(engine_config)
            runner.registry = reg

        async def _loop_that_gets_interrupted(session, *a, **k):
            session.mark_interrupted("operator stop")

        verify_spy = AsyncMock(return_value="verified")

        with (
            patch.object(runner, "_run_loop", _loop_that_gets_interrupted),
            patch.object(runner, "_run_verification", verify_spy),
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_step"),
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                return_value=mock_litellm_response(content="x"),
            ),
        ):
            run = await runner.execute("test-agent", "go", agent_config=sample_agent_config)

        assert run.status == RunStatus.CANCELLED
        verify_spy.assert_not_called()  # verifier skipped for an interrupted run
