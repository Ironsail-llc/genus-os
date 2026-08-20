"""create_run failure handling — deterministic IntegrityErrors must escalate.

A CHECK/FK violation on the ``agent_runs`` INSERT is deterministic: it fails
on every run of that trigger type forever (see the TriggerType/constraint
drift that made 100% of channel_event wakes invisible). The runner must
never break the run over tracking, but it must:

1. page the operator via robothor.engine.alerts (not a journald warning),
2. stop attempting dependent writes (steps FK to the missing run row), and
3. hand spawned children an empty parent linkage so they record with
   parent_run_id=NULL instead of losing their whole row to the FK.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

from robothor.engine.models import (
    AgentConfig,
    DeliveryMode,
    RunStatus,
    SpawnContext,
    TriggerType,
)
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession


@pytest.fixture
def runner(engine_config):
    """AgentRunner with mocked tool registry."""
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = []
        mock_registry.get_tool_names.return_value = []
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


@pytest.fixture
def basic_agent_config() -> AgentConfig:
    return AgentConfig(
        id="basic-agent",
        name="Basic Agent",
        model_primary="openrouter/test/model",
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        can_spawn_agents=False,
        planning_enabled=False,
        scratchpad_enabled=False,
    )


def _make_response(content: str):
    response = MagicMock()
    # Match the configured primary so the degraded-model alert path stays
    # quiet — these tests assert on the run-recording alert alone.
    response.model = "openrouter/test/model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    response.usage = usage
    return response


async def _noop_alert(*_args, **_kwargs) -> bool:
    return True


def _alert_mock() -> MagicMock:
    """Records alert() calls; returns a real coroutine so spawn() can close it."""
    return MagicMock(side_effect=lambda *a, **k: _noop_alert())


def _spawn_registry_mock():
    """Task registry whose spawn() just closes the coroutine it is handed."""
    registry = MagicMock()

    def _spawn(coro, name=""):
        coro.close()
        return MagicMock()

    registry.spawn.side_effect = _spawn
    return registry


class TestCreateRunIntegrityFailure:
    @pytest.mark.asyncio
    async def test_check_violation_alerts_and_disables_tracking(self, runner, basic_agent_config):
        alert_mock = _alert_mock()
        registry = _spawn_registry_mock()

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=psycopg2.errors.CheckViolation(
                    'new row violates check constraint "agent_runs_trigger_type_check"'
                ),
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch") as runner_batch,
            patch("robothor.engine.runner.create_step") as runner_step,
            patch("robothor.engine.tracking.create_steps_batch") as tracking_batch,
            patch("robothor.engine.tracking.create_step") as tracking_step,
            patch("robothor.engine.alerts.alert", alert_mock),
            patch(
                "robothor.engine.task_registry.get_task_registry",
                return_value=registry,
            ),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            run = await runner.execute(
                "basic-agent",
                "hello",
                agent_config=basic_agent_config,
            )

        # Tracking failure never breaks the run.
        assert run.status == RunStatus.COMPLETED
        # The run is flagged so dependent writes are skipped.
        assert run.tracking_disabled is True
        # Exactly one page-worthy alert was dispatched.
        assert alert_mock.call_count == 1
        assert alert_mock.call_args.args[0] == "critical"
        # No FK-doomed step inserts were attempted.
        runner_batch.assert_not_called()
        runner_step.assert_not_called()
        tracking_batch.assert_not_called()
        tracking_step.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_failure_still_only_warns(self, runner, basic_agent_config):
        """Non-integrity errors keep the old behavior: warn, no alert, no flag."""
        alert_mock = _alert_mock()

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=psycopg2.OperationalError("connection refused"),
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
            patch("robothor.engine.tracking.create_steps_batch"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.engine.alerts.alert", alert_mock),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            run = await runner.execute(
                "basic-agent",
                "hello",
                agent_config=basic_agent_config,
            )

        assert run.status == RunStatus.COMPLETED
        assert run.tracking_disabled is False
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_context_carries_no_parent_id_when_untracked(
        self, runner, basic_agent_config
    ):
        """A run whose row was never created must not advertise its id as
        parent_run_id — children would fail the FK and lose their whole row."""
        basic_agent_config.can_spawn_agents = True
        registry = _spawn_registry_mock()

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=psycopg2.errors.CheckViolation("constraint"),
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
            patch("robothor.engine.tracking.create_steps_batch"),
            patch("robothor.engine.tracking.create_step"),
            patch("robothor.engine.alerts.alert", _alert_mock()),
            patch(
                "robothor.engine.task_registry.get_task_registry",
                return_value=registry,
            ),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            await runner.execute(
                "basic-agent",
                "hello",
                agent_config=basic_agent_config,
            )

        from robothor.engine.tools import _current_spawn_context

        ctx = _current_spawn_context.get()
        assert ctx is not None
        assert ctx.parent_run_id == ""

    @pytest.mark.asyncio
    async def test_child_of_untracked_parent_records_null_parent(self, runner, basic_agent_config):
        """A child spawned from an untracked parent (empty parent_run_id in the
        SpawnContext) inserts its own row with parent_run_id=NULL."""
        spawn_context = SpawnContext(
            parent_run_id="",
            parent_agent_id="parent-agent",
            correlation_id=str(uuid.uuid4()),
            nesting_depth=0,
        )
        captured: dict[str, object] = {}

        def fake_create_run(run):
            captured["run"] = run
            return run.id

        with (
            patch("robothor.engine.runner.create_run", side_effect=fake_create_run),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.runner.create_steps_batch"),
            patch("robothor.engine.tracking.create_steps_batch"),
            patch("robothor.engine.tracking.create_step"),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            run = await runner.execute(
                "basic-agent",
                "hello",
                trigger_type=TriggerType.SUB_AGENT,
                agent_config=basic_agent_config,
                spawn_context=spawn_context,
            )

        assert run.status == RunStatus.COMPLETED
        assert "run" in captured, "child run was never recorded"
        assert captured["run"].parent_run_id is None


class TestStepFlushSkipsUntrackedRun:
    def test_flush_new_steps_sync_skips_when_tracking_disabled(self):
        session = AgentSession(
            agent_id="basic-agent",
            trigger_type=TriggerType.MANUAL,
            tenant_id="test-tenant",
        )
        session.start(
            system_prompt="sys",
            user_message="hi",
            tools_provided=[],
            delivery_mode="none",
        )
        session.record_llm_call(model="test-model", input_tokens=1, output_tokens=1)
        assert session.run.steps, "expected a recorded step"
        session.run.tracking_disabled = True

        with (
            patch("robothor.engine.tracking.create_steps_batch") as batch,
            patch("robothor.engine.tracking.create_step") as single,
        ):
            flushed = session.flush_new_steps_sync()

        assert flushed == 0
        batch.assert_not_called()
        single.assert_not_called()
