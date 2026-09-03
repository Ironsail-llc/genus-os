"""A sub-agent abandoned by its parent's cancellation must still finalise.

2026-09-03, run 0a78ed9f (crm-hygiene): `spawn_agent` awaits the child INLINE
in the parent's asyncio task, under the parent's 600s per-tool
`asyncio.timeout` (`tools/registry.py`). At 600.001s that deadline cancelled
the shared task. The parent logged "Tool spawn_agent timed out after 600s",
handed its model an error dict and carried on in the same second. The child's
`agent_runs` row stayed `running`, with NULL `error_traceback`, for two hours
— until the reaper tombstoned it and (item 2) mislabelled the cause.

This was not a one-off: every one of the 17 `sub_agent` rows carrying
`reap_category='daemon_restart'` has a NULL cancel diagnostic, and only one of
them had a ~600s `spawn_agent` step. The general form is that ANY cancellation
of the parent — its own watchdog, daemon shutdown, an outer `wait_for` —
silently abandons the inline child.

The investigation could not name the exact frame that diverted the
`CancelledError` around `runner.py`'s cancel handler. So these tests do not
assert on that handler, or on any log line it prints. They assert the
invariant that has to hold **whichever frame routes the cancellation**: by the
time the CancelledError leaves `_handle_spawn_agent`, the child's run has been
put through the finalisation path with a terminal status, a cause that names
the parent, and a `completed_at`.

And the cancellation must still propagate. Absorbing it here would make the
parent's tool deadline a suggestion — the exact bug this repo shipped in
August, when a benchmark case swallowed its runner's 3600s cancel and the
sweep ran three hours past its ceiling.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from robothor.engine.models import (
    AgentConfig,
    DeliveryMode,
    RunStatus,
    SpawnContext,
    TriggerType,
)
from robothor.engine.session import AgentSession


@pytest.fixture
def parent_run_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def spawn_context(parent_run_id: str) -> SpawnContext:
    return SpawnContext(
        parent_run_id=parent_run_id,
        parent_agent_id="auto-researcher",
        correlation_id=str(uuid.uuid4()),
        nesting_depth=1,
        max_nesting_depth=3,
        remaining_token_budget=0,
        remaining_cost_budget_usd=0.0,
    )


@pytest.fixture
def child_config() -> AgentConfig:
    return AgentConfig(
        id="crm-hygiene",
        name="CRM Hygiene",
        model_primary="openrouter/test/model",
        max_iterations=15,
        timeout_seconds=0,  # what crm-hygiene actually declares: no cap
        delivery_mode=DeliveryMode.NONE,
        tools_allowed=["list_tasks"],
    )


class _FakeRunner:
    """A runner whose child registers a real session, then blocks forever.

    ``bypass_own_handler`` reproduces the production signature: the child's
    own cancel handler does NOT run, so nothing but the spawn path can write
    the terminal row. With it False the child finalises itself first, which is
    the case where the spawn path must not write a second, contradictory row.
    """

    def __init__(self, *, bypass_own_handler: bool = True) -> None:
        from unittest.mock import MagicMock

        self.config = MagicMock()
        self.config.manifest_dir = "/tmp/agents"
        self.bypass_own_handler = bypass_own_handler
        self.session: AgentSession | None = None
        self.sessions: list[AgentSession] = []
        self.finished: list = []

    async def execute(self, **kwargs):
        from robothor.engine import session_registry

        session = AgentSession(
            kwargs.get("agent_id", "crm-hygiene"),
            TriggerType.SUB_AGENT,
            kwargs.get("trigger_detail"),
        )
        spawn_ctx = kwargs.get("spawn_context")
        if spawn_ctx is not None:
            session.run.parent_run_id = spawn_ctx.parent_run_id
        session.run.status = RunStatus.RUNNING
        # The real runner registers ~275 lines into execute(), after prompt
        # assembly, the planner call and sandbox start — so with concurrent
        # spawns EVERY sibling's watch is already installed before the first
        # child registers. A fake that registers on its first line hides
        # exactly the collision this file exists to catch.
        await asyncio.sleep(0.01)
        self.session = session
        self.sessions.append(session)
        session_registry.register(session)
        try:
            # The child's next LLM call, which never returns.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not self.bypass_own_handler:
                self._finish_run(
                    session.cancelled(reason="Run cancelled externally"),
                    session=session,
                )
            raise
        finally:
            session_registry.unregister(session)
        raise AssertionError("unreachable")  # pragma: no cover

    def _finish_run(self, run, **kwargs):
        self.finished.append(run)
        return run


async def _spawn_under_parent_timeout(
    runner,
    spawn_ctx: SpawnContext,
    child_config: AgentConfig,
    *,
    tool_timeout: float | None,
) -> None:
    """Drive `_handle_spawn_agent` the way `ToolRegistry.execute_tool` does.

    ``tool_timeout`` None means "cancelled from outside" — no per-tool
    deadline is in scope, which is the other 16 observed cases.
    """
    from unittest.mock import patch

    from robothor.engine.tools import _current_spawn_context, _handle_spawn_agent, set_runner

    set_runner(runner)
    _current_spawn_context.set(spawn_ctx)
    try:
        with (
            patch("robothor.engine.config.load_agent_config", return_value=child_config),
            patch("robothor.engine.dedup.try_acquire", return_value=True),
            patch("robothor.engine.dedup.release"),
        ):
            call = _handle_spawn_agent(
                {"agent_id": "crm-hygiene", "message": "tidy the CRM"},
                agent_id="auto-researcher",
            )
            if tool_timeout is None:
                task = asyncio.create_task(call)
                await asyncio.sleep(0.05)
                task.cancel()
                await task
            else:
                from robothor.engine.spawn_cancel import tool_deadline

                with tool_deadline(tool_timeout):
                    async with asyncio.timeout(tool_timeout):
                        await call
    finally:
        set_runner(None)  # type: ignore[arg-type]
        _current_spawn_context.set(None)


class TestChildFinalisedOnParentCancel:
    @pytest.mark.asyncio
    async def test_parent_tool_timeout_finalises_the_child_as_timeout(
        self, spawn_context, child_config, parent_run_id
    ):
        """The incident, in miniature: the parent's tool deadline fires."""
        runner = _FakeRunner()

        with pytest.raises(TimeoutError):
            await _spawn_under_parent_timeout(runner, spawn_context, child_config, tool_timeout=0.2)

        # Assert on the finalisation call, not on a log line.
        assert runner.finished, (
            "the child's run was never finalised — its agent_runs row is still "
            "'running' and only the reaper will ever touch it"
        )
        run = runner.finished[-1]
        assert run.status is RunStatus.TIMEOUT, run.status
        assert run.completed_at is not None
        assert parent_run_id in (run.error_message or "")
        assert "tool timeout" in (run.error_message or "")
        assert run.error_traceback, "no cancel diagnostic — the incident's NULL signature"

    @pytest.mark.asyncio
    async def test_external_cancel_finalises_the_child_as_cancelled(
        self, spawn_context, child_config, parent_run_id
    ):
        """The general form: the parent's task is cancelled by something else.

        No per-tool deadline is in scope, so this is a cancel, not a timeout —
        and calling it a timeout would corrupt the timeout rate the way
        `cancel_outcome.py` documents.
        """
        runner = _FakeRunner()

        with pytest.raises(asyncio.CancelledError):
            await _spawn_under_parent_timeout(
                runner, spawn_context, child_config, tool_timeout=None
            )

        assert runner.finished
        run = runner.finished[-1]
        assert run.status is RunStatus.CANCELLED, run.status
        assert run.completed_at is not None
        assert parent_run_id in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_the_cancellation_still_propagates(self, spawn_context, child_config):
        """Finalising must not absorb the cancel — the deadline stays real."""
        runner = _FakeRunner()
        with pytest.raises(TimeoutError):
            await _spawn_under_parent_timeout(runner, spawn_context, child_config, tool_timeout=0.2)

    @pytest.mark.asyncio
    async def test_a_child_that_finalised_itself_is_not_overwritten(
        self, spawn_context, child_config
    ):
        """Whichever frame gets there first owns the row.

        When the runner's own cancel handler does run, the spawn path must not
        write a second row that contradicts it.
        """
        runner = _FakeRunner(bypass_own_handler=False)

        with pytest.raises(TimeoutError):
            await _spawn_under_parent_timeout(runner, spawn_context, child_config, tool_timeout=0.2)

        assert len(runner.finished) == 1, "the spawn path wrote a second terminal row"
        assert runner.finished[0].status is RunStatus.CANCELLED


class TestConcurrentChildrenOfOneParent:
    """`spawn_agents` explicitly supports the same agent id twice.

    `_handle_spawn_agents` gathers N `_handle_spawn_agent` coroutines, and the
    "wide research pattern" the dedup key was namespaced for is precisely the
    same agent spawned with different messages. A watch that identifies its
    child by (agent_id, parent_run_id) cannot tell those siblings apart: the
    first registration satisfies every unfilled watch, so one child is claimed
    twice and the other is claimed by nobody and abandoned exactly as before.

    Identify the child POSITIONALLY instead — each spawn mints a token, sets
    it for the duration of its own `runner.execute`, and the watch claims only
    the registration that carries its own token.
    """

    @pytest.mark.asyncio
    async def test_both_concurrent_children_are_finalised(self, spawn_context, child_config):
        from unittest.mock import patch

        from robothor.engine.spawn_cancel import tool_deadline
        from robothor.engine.tools import (
            _current_spawn_context,
            _handle_spawn_agents,
            set_runner,
        )

        runner = _FakeRunner()
        set_runner(runner)
        _current_spawn_context.set(spawn_context)
        try:
            with (
                patch("robothor.engine.config.load_agent_config", return_value=child_config),
                patch("robothor.engine.dedup.try_acquire", return_value=True),
                patch("robothor.engine.dedup.release"),
            ):
                call = _handle_spawn_agents(
                    {
                        "agents": [
                            {"agent_id": "crm-hygiene", "message": "sweep the tasks"},
                            {"agent_id": "crm-hygiene", "message": "sweep the people"},
                        ]
                    },
                    agent_id="auto-researcher",
                )
                with pytest.raises(TimeoutError), tool_deadline(0.3):
                    async with asyncio.timeout(0.3):
                        await call
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

        assert len(runner.sessions) == 2, "both children should have started"
        finalised = {r.id for r in runner.finished}
        started = {s.run.id for s in runner.sessions}
        assert finalised == started, (
            f"every started child must be finalised — abandoned: {sorted(started - finalised)}"
        )
        assert all(r.status is RunStatus.TIMEOUT for r in runner.finished)


class _PlannerBlockedRunner(_FakeRunner):
    """A child cancelled BEFORE the runner reaches `session_registry.register`.

    The real `execute()` starts the session, then spends ~275 lines assembling
    the prompt, calling the planner and starting the sandbox before it
    registers. A cancellation anywhere in that stretch leaves the incident's
    exact signature — `running`, NULL traceback, zero steps — and a watch that
    only hears about registrations has nothing to finalise.
    """

    async def execute(self, **kwargs):
        session = AgentSession(
            kwargs.get("agent_id", "crm-hygiene"),
            TriggerType.SUB_AGENT,
            kwargs.get("trigger_detail"),
        )
        spawn_ctx = kwargs.get("spawn_context")
        if spawn_ctx is not None:
            session.run.parent_run_id = spawn_ctx.parent_run_id
        self.session = session
        self.sessions.append(session)
        session.start("sys", kwargs.get("message", ""), [])
        # The planner LLM call, which never returns. `register` is never
        # reached, exactly as in the pre-loop window.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


class TestCancelBeforeTheLoopStarts:
    @pytest.mark.asyncio
    async def test_a_child_cancelled_in_its_planner_call_is_finalised(
        self, spawn_context, child_config
    ):
        runner = _PlannerBlockedRunner()

        with pytest.raises(TimeoutError):
            await _spawn_under_parent_timeout(runner, spawn_context, child_config, tool_timeout=0.2)

        assert runner.finished, (
            "a child cancelled before the run loop is the incident signature: "
            "running, NULL traceback, zero steps"
        )
        run = runner.finished[-1]
        assert run.status is RunStatus.TIMEOUT
        assert run.completed_at is not None
        assert run.error_traceback


class TestTheRegistryEndIsWired:
    """The deadline the finaliser reads must come from the REAL tool path.

    Every test above hand-rolls `tool_deadline`, so deleting the
    `with tool_deadline(timeout)` in `ToolRegistry.execute` would break none of
    them and the wiring would rot silently — the shape of inert control this
    repo keeps re-learning. These drive `ToolRegistry.execute("spawn_agent")`
    itself, so the deadline has to be set where production sets it.
    """

    @staticmethod
    def _patches(child_config):
        from unittest.mock import patch

        return (
            patch("robothor.engine.config.load_agent_config", return_value=child_config),
            patch("robothor.engine.dedup.try_acquire", return_value=True),
            patch("robothor.engine.dedup.release"),
            # RBAC is not what this test is about, and it is database-backed.
            patch("robothor.engine.permissions.check_tool_permission", return_value=None),
        )

    @pytest.mark.asyncio
    async def test_the_per_tool_deadline_makes_the_child_a_timeout(
        self, spawn_context, child_config
    ):
        from contextlib import ExitStack

        from robothor.engine.tools import _current_spawn_context, set_runner
        from robothor.engine.tools.registry import ToolRegistry

        runner = _FakeRunner()
        set_runner(runner)
        _current_spawn_context.set(spawn_context)
        try:
            with ExitStack() as stack:
                for p in self._patches(child_config):
                    stack.enter_context(p)
                result = await ToolRegistry().execute(
                    "spawn_agent",
                    {"agent_id": "crm-hygiene", "message": "tidy the CRM"},
                    agent_id="auto-researcher",
                    timeout=1,
                )
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

        # The parent's side is unchanged: it gets an error dict and carries on.
        assert "timed out" in result.get("error", "")
        # The child's side is the fix.
        assert runner.finished, "the child was abandoned by the real tool path"
        assert runner.finished[-1].status is RunStatus.TIMEOUT
        assert "tool timeout" in (runner.finished[-1].error_message or "")

    @pytest.mark.asyncio
    async def test_an_uncapped_tool_call_cancelled_from_outside_is_a_cancel(
        self, spawn_context, child_config
    ):
        """timeout=0 is "no cap", so a cancel there is nobody's deadline."""
        from contextlib import ExitStack

        from robothor.engine.tools import _current_spawn_context, set_runner
        from robothor.engine.tools.registry import ToolRegistry

        runner = _FakeRunner()
        set_runner(runner)
        _current_spawn_context.set(spawn_context)
        try:
            with ExitStack() as stack:
                for p in self._patches(child_config):
                    stack.enter_context(p)
                task = asyncio.create_task(
                    ToolRegistry().execute(
                        "spawn_agent",
                        {"agent_id": "crm-hygiene", "message": "tidy the CRM"},
                        agent_id="auto-researcher",
                        timeout=0,
                    )
                )
                await asyncio.sleep(0.1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            set_runner(None)  # type: ignore[arg-type]
            _current_spawn_context.set(None)

        assert runner.finished
        assert runner.finished[-1].status is RunStatus.CANCELLED


class TestTheSeamIsCountable:
    """A control that silently does nothing is this repo's recurring failure.

    `finalize_abandoned_child` returns None both when it did its job earlier
    (some other frame already owns the row) and when it had no row at all —
    the watch never claimed a session. The second is a dead seam and must be
    visible without adding a log line to trust, so it is counted.
    """

    @pytest.mark.asyncio
    async def test_an_unclaimed_cancellation_is_counted(self, parent_run_id):
        from robothor.engine.spawn_cancel import finalisation_stats, finalize_abandoned_child

        before = finalisation_stats()
        finalize_abandoned_child(None, None, parent_run_id=parent_run_id, elapsed_s=1.0)
        after = finalisation_stats()

        assert after["unclaimed"] == before["unclaimed"] + 1
        assert after["finalised"] == before["finalised"]

    @pytest.mark.asyncio
    async def test_a_real_finalisation_is_counted(self, spawn_context, child_config):
        from robothor.engine.spawn_cancel import finalisation_stats

        runner = _FakeRunner()
        before = finalisation_stats()
        with pytest.raises(TimeoutError):
            await _spawn_under_parent_timeout(runner, spawn_context, child_config, tool_timeout=0.2)
        after = finalisation_stats()

        assert after["finalised"] == before["finalised"] + 1
        assert after["unclaimed"] == before["unclaimed"]
