"""A run created inside a tenant_scope must record under that tenant.

Found 2026-08-22 while re-measuring agent-architect. A benchmark task declaring
``state_checks`` is executed inside ``tenant_scope(benchmark-sandbox)``. The
outer run is passed the tenant explicitly and records fine. A run created
*nested* inside that scope calls ``runner.execute`` with no tenant, so the
resolution fell back to ``self.config.tenant_id`` (robothor-primary) while the
connection was still bound to the sandbox. The RLS WITH CHECK on ``agent_runs``
requires ``tenant_id = current_setting('app.tenant_id')``, so the INSERT was
refused:

    Failed to record run start: new row violates row-level security policy
    Failed to record step: violates FK — Key (run_id)=(...) not present

Reproduced as ``robothor_app``; invisible as ``philip``, who is a superuser and
bypasses RLS unconditionally.

Two defects, one symptom:

1. The nested run resolved the wrong tenant. It must inherit the enclosing
   scope when the caller names no tenant.
2. The rejection was swallowed. ``InsufficientPrivilege`` is a
   ``ProgrammingError``, not an ``IntegrityError``, so it missed the escalation
   path that exists for deterministic rejections and never set
   ``tracking_disabled`` — leaving every subsequent step insert to fail FK, one
   log line at a time. It is exactly as deterministic as a CHECK violation and
   must escalate the same way.

Why it matters beyond bookkeeping: ``expected.tools_used`` grades against
``agent_run_steps``, and a missing trace fails every ``tools_used`` assertion by
contract. The cases pairing ``state_checks`` with ``tools_used`` were the ones
losing their trace.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

from robothor.db.connection import current_tenant_scope, tenant_scope
from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner


@pytest.fixture
def runner(engine_config):
    """AgentRunner with mocked tool registry (mirrors test_run_recording_failure)."""
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


class TestCurrentTenantScope:
    def test_none_outside_any_scope(self) -> None:
        assert current_tenant_scope() is None

    def test_reports_the_enclosing_scope(self) -> None:
        with tenant_scope("benchmark-sandbox"):
            assert current_tenant_scope() == "benchmark-sandbox"

    def test_restored_on_exit(self) -> None:
        with tenant_scope("benchmark-sandbox"):
            pass
        assert current_tenant_scope() is None

    def test_nested_scopes_report_the_innermost(self) -> None:
        with tenant_scope("outer"), tenant_scope("inner"):
            assert current_tenant_scope() == "inner"

    def test_empty_scope_is_none_not_empty_string(self) -> None:
        """``tenant_scope("")`` means "no binding" — the permissive branch."""
        with tenant_scope(""):
            assert current_tenant_scope() is None


class TestNestedRunInheritsTheScope:
    @pytest.mark.asyncio
    async def test_run_without_an_explicit_tenant_adopts_the_scope(
        self, runner, basic_agent_config
    ) -> None:
        """The defect: this recorded as config.tenant_id and RLS refused it."""
        recorded: list[str] = []

        def _capture(run):
            recorded.append(run.tenant_id)
            return str(run.id)

        with (
            patch("robothor.engine.runner.create_run", side_effect=_capture),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_steps_batch"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
            tenant_scope("benchmark-sandbox"),
        ):
            run = await runner.execute("basic-agent", "hello", agent_config=basic_agent_config)

        assert recorded == ["benchmark-sandbox"]
        assert run.tenant_id == "benchmark-sandbox"

    @pytest.mark.asyncio
    async def test_an_explicit_tenant_still_wins_over_the_scope(
        self, runner, basic_agent_config
    ) -> None:
        recorded: list[str] = []

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=lambda run: (recorded.append(run.tenant_id), str(run.id))[1],
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_steps_batch"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
            tenant_scope("benchmark-sandbox"),
        ):
            await runner.execute(
                "basic-agent",
                "hello",
                agent_config=basic_agent_config,
                tenant_id="explicit-tenant",
            )

        assert recorded == ["explicit-tenant"]

    @pytest.mark.asyncio
    async def test_outside_a_scope_the_config_default_still_applies(
        self, runner, basic_agent_config
    ) -> None:
        recorded: list[str] = []

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=lambda run: (recorded.append(run.tenant_id), str(run.id))[1],
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_steps_batch"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            await runner.execute("basic-agent", "hello", agent_config=basic_agent_config)

        assert recorded and recorded[0] != "benchmark-sandbox"


class TestRlsRejectionEscalates:
    @pytest.mark.asyncio
    async def test_rls_rejection_disables_tracking_and_pages(
        self, runner, basic_agent_config
    ) -> None:
        """An RLS refusal is as deterministic as a CHECK violation.

        Before this, it fell to the generic handler: one warning, no flag, and
        then every dependent step insert attempted and failed FK in turn.
        """
        alert_mock = _alert_mock()
        registry = _spawn_registry_mock()

        with (
            patch(
                "robothor.engine.runner.create_run",
                side_effect=psycopg2.errors.InsufficientPrivilege(
                    'new row violates row-level security policy for table "agent_runs"'
                ),
            ),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_steps_batch") as runner_batch,
            patch("robothor.engine.run_finalizer.create_step") as runner_step,
            patch("robothor.engine.tracking.create_steps_batch") as tracking_batch,
            patch("robothor.engine.tracking.create_step") as tracking_step,
            patch("robothor.engine.alerts.alert", alert_mock),
            patch(
                "robothor.engine.task_registry.get_task_registry",
                return_value=registry,
            ),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
        ):
            run = await runner.execute("basic-agent", "hello", agent_config=basic_agent_config)

        assert run.status == RunStatus.COMPLETED, "tracking must never break the run"
        assert run.tracking_disabled is True
        assert alert_mock.call_count == 1
        assert alert_mock.call_args.args[0] == "critical"
        runner_batch.assert_not_called()
        runner_step.assert_not_called()
        tracking_batch.assert_not_called()
        tracking_step.assert_not_called()


# --- helpers mirrored from test_run_recording_failure.py --------------------


def _make_response(content: str):
    response = MagicMock()
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


def _alert_mock():
    async def _noop(*args, **kwargs):
        return None

    return MagicMock(side_effect=_noop)


def _spawn_registry_mock():
    registry = MagicMock()

    def _spawn(coro, name=None):
        coro.close()
        return MagicMock()

    registry.spawn.side_effect = _spawn
    return registry


class TestTheRecordingThreadKeepsTheBinding:
    @pytest.mark.asyncio
    async def test_create_run_runs_with_the_tenant_override_intact(
        self, runner, basic_agent_config
    ) -> None:
        """`run_in_executor` does not copy ContextVars; `asyncio.to_thread` does.

        `create_run` runs off the event loop. If the tenant override does not
        travel with it, `_apply_tenant_scope` binds that thread's connection to
        the DEFAULT tenant while the row carries the scope's tenant, and the RLS
        WITH CHECK refuses it. Measured 2026-08-22 inside
        `tenant_scope("benchmark-sandbox")`:

            in the coroutine        -> override=benchmark-sandbox, binding=benchmark-sandbox
            through run_in_executor -> override=None,              binding=robothor-primary

        So the row tenant was right all along and the *connection binding* was
        wrong. This asserts the override reaches the recording thread.
        """
        seen: list[str | None] = []

        def _capture(run):
            seen.append(current_tenant_scope())
            return str(run.id)

        with (
            patch("robothor.engine.runner.create_run", side_effect=_capture),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_steps_batch"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=[_make_response("done")]),
            tenant_scope("benchmark-sandbox"),
        ):
            await runner.execute("basic-agent", "hello", agent_config=basic_agent_config)

        assert seen == ["benchmark-sandbox"], (
            "the tenant override did not reach the thread that records the run"
        )
