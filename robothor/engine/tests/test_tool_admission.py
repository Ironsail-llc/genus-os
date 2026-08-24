"""Admission control for tool calls — what the loop refuses, and how.

Five independent gates stand between a model asking for a tool and the tool
running: plan mode, the agent's tools_allowed set, a PRE_TOOL_USE lifecycle
hook, the guardrail engine, and the system-run RBAC check. Together they are
~310 lines inside `_run_loop`, and until now not one of them had a test that
drove a real refusal through the loop — the coverage was source-level greps
and unit tests of the checkers in isolation, which is exactly the shape that
lets a correct checker sit behind an inert caller.

These are characterization tests. They pin what each gate does TODAY,
including the parts that look like oversights (two gates do not count toward
escalation; one refusal writes an audit row and the others do not), so that
any change to that behavior has to be deliberate enough to edit a test that
says so.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner


def _tool_call(name: str, args: dict | None = None, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _response(content=None, tool_calls=None, model="test-model"):
    response = MagicMock()
    response.model = model
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "send_email"}},
        ]
        registry.get_tool_names.return_value = ["read_file", "send_email"]
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="gate-agent",
        name="Gate Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )


async def _run_one_tool_call(runner, agent_config, tool_name="send_email", tool_args=None, **kw):
    """Drive exactly one refused-or-allowed tool call through the real loop.

    The model asks for one tool, then (on the next turn) answers with text so
    the run terminates. Whatever the gate did to the first call is visible in
    the recorded steps.
    """
    calls = {"n": 0}

    async def completion(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(tool_calls=[_tool_call(tool_name, tool_args)])
        return _response(content="done")

    executed: list[str] = []
    seen_args: list[dict] = []

    async def execute(name, args, **kwargs):
        executed.append(name)
        seen_args.append(args)
        return {"ok": True}

    runner.registry.execute = AsyncMock(side_effect=execute)

    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        run = await runner.execute("gate-agent", "go", agent_config=agent_config, **kw)
    return run, executed, seen_args


def _restrict_schemas(runner, names: list[str]) -> None:
    """Narrow what the registry builds for this agent, which is the input the
    runtime tools_allowed gate actually reads."""
    runner.registry.build_for_agent.return_value = [
        {"type": "function", "function": {"name": n}} for n in names
    ]
    runner.registry.get_tool_names.return_value = list(names)


def _tool_steps(run, tool_name: str) -> list:
    return [s for s in run.steps if getattr(s, "tool_name", None) == tool_name]


class TestPlanModeGate:
    """Plan mode filters schemas; this is the runtime backstop behind that."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_write_tool_is_refused_and_never_executed(self, runner, agent_config):
        run, executed, _ = await _run_one_tool_call(
            runner, agent_config, tool_name="send_email", readonly_mode=True
        )
        assert executed == [], "plan mode let a write tool through to the registry"

        steps = _tool_steps(run, "send_email")
        assert steps, "the refusal was not recorded as a step"
        assert "not available in plan mode" in (steps[0].error_message or "")
        assert steps[0].tool_output.get("guard") == "plan_mode"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_run_continues_after_a_refusal(self, runner, agent_config):
        """A refused tool is feedback to the model, not a fatal error."""
        run, _, _ = await _run_one_tool_call(
            runner, agent_config, tool_name="send_email", readonly_mode=True
        )
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "done"


class TestToolsAllowedGate:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_tool_outside_the_agents_set_is_refused(self, runner, agent_config):
        # The gate's real input is the schema set the registry built for this
        # agent — that is what `tools_allowed` narrows upstream — so the test
        # has to narrow it here rather than only setting the config field.
        _restrict_schemas(runner, ["read_file"])
        agent_config.tools_allowed = ["read_file"]
        run, executed, _ = await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert executed == []

        steps = _tool_steps(run, "send_email")
        assert steps
        assert "not available to this agent" in (steps[0].error_message or "")
        assert steps[0].tool_output.get("guard") == "tools_allowed"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_an_allowed_tool_still_runs(self, runner, agent_config):
        """The gate must not be so eager that it refuses everything — the
        failure mode that makes a guard look like it works."""
        _restrict_schemas(runner, ["read_file"])
        agent_config.tools_allowed = ["read_file"]
        _, executed, _ = await _run_one_tool_call(runner, agent_config, tool_name="read_file")
        assert executed == ["read_file"]

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_an_empty_set_means_no_restriction(self, runner, agent_config):
        agent_config.tools_allowed = []
        _, executed, _ = await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert executed == ["send_email"]


class TestGuardrailGate:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_blocking_guardrail_refuses_and_names_itself(self, runner, agent_config):
        from robothor.engine.guardrails import GuardrailResult

        agent_config.guardrails = ["no_secrets_in_output"]

        blocked = GuardrailResult(
            allowed=False,
            action="blocked",
            reason="looks like a secret",
            guardrail_name="no_secrets_in_output",
        )
        with patch(
            "robothor.engine.guardrails.GuardrailEngine.check_pre_execution",
            return_value=blocked,
        ):
            run, executed, _ = await _run_one_tool_call(
                runner, agent_config, tool_name="send_email"
            )

        assert executed == []
        steps = _tool_steps(run, "send_email")
        assert steps
        # The agent is told WHICH control stopped it — a bare "blocked" is
        # unactionable both for the model and for the operator reading later.
        assert "no_secrets_in_output" in (steps[0].error_message or "")
        assert steps[0].tool_output.get("guardrail") == "no_secrets_in_output"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_block_writes_an_audit_row(self, runner, agent_config):
        """A control that fires and leaves no trace cannot be trusted later.

        This is the one refusal path that writes to agent_guardrail_events —
        the table the health dashboard reads.
        """
        from robothor.engine.guardrails import GuardrailResult

        agent_config.guardrails = ["no_secrets_in_output"]
        blocked = GuardrailResult(
            allowed=False,
            action="blocked",
            reason="nope",
            guardrail_name="no_secrets_in_output",
        )
        with (
            patch(
                "robothor.engine.guardrails.GuardrailEngine.check_pre_execution",
                return_value=blocked,
            ),
            patch("robothor.engine.tracking.log_guardrail_event") as log_event,
        ):
            await _run_one_tool_call(runner, agent_config, tool_name="send_email")

        assert log_event.called, "a guardrail block left no audit row"
        kwargs = log_event.call_args.kwargs
        assert kwargs["action"] == "blocked"
        assert kwargs["mode"] == "enforce"
        assert kwargs["guardrail_name"] == "no_secrets_in_output"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_an_observed_guardrail_allows_the_tool_and_still_records(
        self, runner, agent_config
    ):
        """Observe mode is a soak, and a soak that records nothing cannot
        tell 'clean' from 'blind'."""
        from robothor.engine.guardrails import GuardrailResult

        agent_config.guardrails = ["no_secrets_in_output"]
        observed = GuardrailResult(
            allowed=True,
            action="observed",
            reason="would have blocked",
            guardrail_name="no_secrets_in_output",
        )
        with (
            patch(
                "robothor.engine.guardrails.GuardrailEngine.check_pre_execution",
                return_value=observed,
            ),
            patch("robothor.engine.tracking.log_guardrail_event") as log_event,
        ):
            _, executed, _ = await _run_one_tool_call(runner, agent_config, tool_name="send_email")

        assert executed == ["send_email"], "observe mode must not block"
        assert log_event.called
        assert log_event.call_args.kwargs["mode"] == "observe"


class TestHookGate:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_blocking_pre_tool_hook_refuses(self, runner, agent_config):
        from robothor.engine.hook_registry import HookAction, HookResult

        registry = MagicMock()
        registry.dispatch = AsyncMock(
            return_value=HookResult(action=HookAction.BLOCK, reason="policy says no")
        )
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry):
            run, executed, _ = await _run_one_tool_call(
                runner, agent_config, tool_name="send_email"
            )

        assert executed == []
        steps = _tool_steps(run, "send_email")
        assert steps
        assert "policy says no" in (steps[0].error_message or "")
        assert steps[0].tool_output.get("hook") == "pre_tool_use"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_hook_can_rewrite_the_arguments(self, runner, agent_config):
        from robothor.engine.hook_registry import HookAction, HookResult

        registry = MagicMock()
        registry.dispatch = AsyncMock(
            return_value=HookResult(
                action=HookAction.MODIFY, modified_args={"to": "redacted@example.com"}
            )
        )
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry):
            _, executed, seen = await _run_one_tool_call(
                runner, agent_config, tool_name="send_email", tool_args={"to": "board@example.com"}
            )

        assert executed == ["send_email"]
        assert seen and seen[0] == {"to": "redacted@example.com"}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_broken_hook_does_not_block_the_tool(self, runner, agent_config):
        """A hook that raises is a broken hook, not a denial. Failing closed
        here would let one bad third-party hook halt every tool call."""
        registry = MagicMock()
        registry.dispatch = AsyncMock(side_effect=RuntimeError("hook exploded"))
        with patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry):
            _, executed, _ = await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert executed == ["send_email"]


class TestRefusalAccounting:
    """The differences between gates, pinned deliberately.

    Two of the five gates do NOT count their refusal toward the escalation
    threshold. Whether that is intentional is not recorded anywhere; git
    blame shows the escalating and non-escalating gates were written in the
    same weeks by different changes. These tests make the asymmetry visible
    so a future change to it is a decision rather than an accident.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_plan_mode_refusal_does_not_count_toward_escalation(self, runner, agent_config):
        agent_config.error_feedback = True
        with patch("robothor.engine.escalation.EscalationManager.record_error") as rec:
            await _run_one_tool_call(
                runner, agent_config, tool_name="send_email", readonly_mode=True
            )
        assert not rec.called

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_tools_allowed_refusal_does_not_count_toward_escalation(
        self, runner, agent_config
    ):
        agent_config.error_feedback = True
        _restrict_schemas(runner, ["read_file"])
        agent_config.tools_allowed = ["read_file"]
        with patch("robothor.engine.escalation.EscalationManager.record_error") as rec:
            await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert not rec.called

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_hook_block_does_count_toward_escalation(self, runner, agent_config):
        from robothor.engine.hook_registry import HookAction, HookResult

        agent_config.error_feedback = True
        registry = MagicMock()
        registry.dispatch = AsyncMock(return_value=HookResult(action=HookAction.BLOCK, reason="no"))
        with (
            patch("robothor.engine.hook_registry.get_hook_registry", return_value=registry),
            patch("robothor.engine.escalation.EscalationManager.record_error") as rec,
        ):
            await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert rec.called

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_guardrail_block_does_count_toward_escalation(self, runner, agent_config):
        from robothor.engine.guardrails import GuardrailResult

        agent_config.error_feedback = True
        agent_config.guardrails = ["no_secrets_in_output"]
        blocked = GuardrailResult(
            allowed=False, action="blocked", reason="no", guardrail_name="no_secrets_in_output"
        )
        with (
            patch(
                "robothor.engine.guardrails.GuardrailEngine.check_pre_execution",
                return_value=blocked,
            ),
            patch("robothor.engine.escalation.EscalationManager.record_error") as rec,
        ):
            await _run_one_tool_call(runner, agent_config, tool_name="send_email")
        assert rec.called


class TestGatesRunInOrder:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_plan_mode_wins_over_the_guardrail_engine(self, runner, agent_config):
        """Order is a security property: the cheapest, most absolute gate
        answers first, and a later gate must never get the chance to approve
        what an earlier one refused."""
        from robothor.engine.guardrails import GuardrailResult

        agent_config.guardrails = ["no_secrets_in_output"]
        allowed = GuardrailResult(allowed=True)
        with patch(
            "robothor.engine.guardrails.GuardrailEngine.check_pre_execution",
            return_value=allowed,
        ):
            run, executed, _ = await _run_one_tool_call(
                runner, agent_config, tool_name="send_email", readonly_mode=True
            )

        assert executed == []
        steps = _tool_steps(run, "send_email")
        assert steps[0].tool_output.get("guard") == "plan_mode"
