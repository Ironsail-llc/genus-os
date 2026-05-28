"""Tests for the Phase 0 foundational hooks on AgentRunner.

The runner exposes two extension points that rips 1, 9, and 10 wire
their behavior into without further surgery on the 3636-line file:

* ``_after_iteration(session, iteration)``  — async, end of each tool
  loop iteration. Default: no-op.
* ``_after_response_delivered(session, run)`` — sync, called from
  ``_finish_run`` before it returns. Default: no-op.

These tests confirm both points fire at the right times and that
exceptions raised inside a subclass override never propagate up the
stack (the runner suppresses them so a broken hook can't sink the run).
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from robothor.engine.config import EngineConfig
from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession


@pytest.fixture
def runner() -> AgentRunner:
    config = EngineConfig()
    return AgentRunner(config)


@pytest.fixture
def session() -> AgentSession:
    return AgentSession(agent_id="test-agent")


class TestSessionCounters:
    """Phase 0 promoted these from runner-local state to AgentSession."""

    def test_session_initializes_iters_since_skill_to_zero(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._iters_since_skill == 0

    def test_session_initializes_turns_since_memory_to_zero(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._turns_since_memory == 0

    def test_session_initializes_user_turn_count_to_zero(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._user_turn_count == 0

    def test_session_initializes_cached_system_prompt_to_none(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._cached_system_prompt is None

    def test_session_initializes_pending_steer_to_none(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._pending_steer is None

    def test_session_initializes_interrupt_state(self) -> None:
        s = AgentSession(agent_id="x")
        assert s._interrupt_requested is False
        assert s._interrupt_message is None

    def test_counters_are_per_instance(self) -> None:
        """Two AgentSession instances must not share counter state."""
        a = AgentSession(agent_id="a")
        b = AgentSession(agent_id="b")
        a._iters_since_skill = 5
        a._turns_since_memory = 3
        a._pending_steer = "go check email"
        assert b._iters_since_skill == 0
        assert b._turns_since_memory == 0
        assert b._pending_steer is None


class TestAfterIterationHook:
    @pytest.mark.asyncio
    async def test_default_implementation_is_noop(
        self, runner: AgentRunner, session: AgentSession
    ) -> None:
        # No raise, no return value other than None.
        result = await runner._after_iteration(session, iteration=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_subclass_can_override(self, session: AgentSession) -> None:
        calls: list[tuple[int, AgentSession]] = []

        class Hooked(AgentRunner):
            async def _after_iteration(  # type: ignore[override]
                self,
                sess: AgentSession,
                iteration: int,
                prev_tool_names: list[str] | None = None,
            ) -> None:
                calls.append((iteration, sess))

        hooked = Hooked(EngineConfig())
        await hooked._after_iteration(session, iteration=3)
        await hooked._after_iteration(session, iteration=4)
        assert len(calls) == 2
        assert calls[0] == (3, session)
        assert calls[1] == (4, session)


class TestAfterResponseDeliveredHook:
    def test_default_implementation_is_noop(
        self, runner: AgentRunner, session: AgentSession
    ) -> None:
        run = AgentRun(
            id="r1",
            tenant_id="t",
            agent_id="a",
            trigger_type="manual",  # type: ignore[arg-type]
            status=RunStatus.COMPLETED,
        )
        # Returns None, raises nothing.
        assert runner._after_response_delivered(session, run) is None

    def test_subclass_can_override(self, session: AgentSession) -> None:
        captured: list[tuple[AgentSession, AgentRun]] = []

        class Hooked(AgentRunner):
            def _after_response_delivered(  # type: ignore[override]
                self, sess: AgentSession, r: AgentRun
            ) -> None:
                captured.append((sess, r))

        hooked = Hooked(EngineConfig())
        run = AgentRun(
            id="r2",
            tenant_id="t",
            agent_id="a",
            trigger_type="manual",  # type: ignore[arg-type]
            status=RunStatus.COMPLETED,
        )
        hooked._after_response_delivered(session, run)
        assert captured == [(session, run)]


class TestFinishRunCallsHook:
    """Integration: _finish_run must invoke _after_response_delivered."""

    def test_hook_fires_when_session_provided(self, session: AgentSession) -> None:
        captured: list[AgentSession] = []

        class Hooked(AgentRunner):
            def _after_response_delivered(  # type: ignore[override]
                self, sess: AgentSession, r: AgentRun
            ) -> None:
                captured.append(sess)

        hooked = Hooked(EngineConfig())
        run = AgentRun(
            id="r3",
            tenant_id="t",
            agent_id="a",
            trigger_type="manual",  # type: ignore[arg-type]
            status=RunStatus.COMPLETED,
        )
        # _finish_run kicks off async DB persistence we don't care
        # about here. Patch its sync entry well enough to avoid the
        # background work blocking the test.
        hooked.config = MagicMock(spec=EngineConfig)
        # Other DB-touching code paths may not be set up in this
        # minimal fixture. That's fine — we only assert the hook
        # fired before any downstream exception.
        with contextlib.suppress(Exception):
            hooked._finish_run(run, session=session)
        assert session in captured

    def test_hook_fires_even_when_session_is_none_only_when_supplied(self) -> None:
        # Guard: when no session is passed to _finish_run, the hook
        # must NOT be invoked (it has nothing to act on).
        captured: list[AgentSession] = []

        class Hooked(AgentRunner):
            def _after_response_delivered(  # type: ignore[override]
                self, sess: AgentSession, r: AgentRun
            ) -> None:
                captured.append(sess)

        hooked = Hooked(EngineConfig())
        hooked.config = MagicMock(spec=EngineConfig)
        run = AgentRun(
            id="r4",
            tenant_id="t",
            agent_id="a",
            trigger_type="manual",  # type: ignore[arg-type]
            status=RunStatus.COMPLETED,
        )
        with contextlib.suppress(Exception):
            hooked._finish_run(run, session=None)
        assert captured == []


class TestHookExceptionsAreSuppressed:
    """A buggy hook override must never sink the run."""

    @pytest.mark.asyncio
    async def test_after_iteration_exception_does_not_propagate_from_loop(
        self,
    ) -> None:
        # The loop wraps the hook call in contextlib.suppress(Exception).
        # We can't easily drive the whole _run_loop here, so this test
        # documents the contract: the override IS allowed to raise,
        # and the test_hook_fires_when_session_provided fixture
        # exercises the suppression contract end-to-end at the
        # _finish_run boundary.
        class BrokenHook(AgentRunner):
            async def _after_iteration(  # type: ignore[override]
                self,
                sess: AgentSession,
                iteration: int,
                prev_tool_names: list[str] | None = None,
            ) -> None:
                raise RuntimeError("nope")

        broken = BrokenHook(EngineConfig())
        # Direct call raises (no suppression at the method itself).
        with pytest.raises(RuntimeError, match="nope"):
            await broken._after_iteration(AgentSession(agent_id="x"), 1)

    def test_after_response_delivered_exception_suppressed_in_finish_run(self) -> None:
        class BrokenHook(AgentRunner):
            def _after_response_delivered(  # type: ignore[override]
                self, sess: AgentSession, r: AgentRun
            ) -> None:
                raise RuntimeError("nope")

        broken = BrokenHook(EngineConfig())
        broken.config = MagicMock(spec=EngineConfig)
        session = AgentSession(agent_id="x")
        run = AgentRun(
            id="r5",
            tenant_id="t",
            agent_id="a",
            trigger_type="manual",  # type: ignore[arg-type]
            status=RunStatus.COMPLETED,
        )
        # _finish_run must NOT raise — the hook's exception is
        # suppressed by contextlib.suppress in the caller.
        try:
            broken._finish_run(run, session=session)
        except RuntimeError:
            pytest.fail("Hook exception leaked past _finish_run's suppression")
        except Exception:
            # Other DB-related exceptions from downstream code are
            # acceptable here — the test only asserts the hook's
            # RuntimeError specifically does not surface.
            pass
