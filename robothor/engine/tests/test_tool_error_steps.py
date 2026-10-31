"""A tool call that returns an error is recorded as a failed step.

``analytics.tool_success_rate`` counts a ``tool_call`` step as OK exactly when
its ``error_message`` is NULL. So the step row is the whole contract: a handler
that answers ``{"error": ...}`` must leave a step whose ``error_message`` says
why, as text, or the agent's tool health reads 100% while the tool fails on
every call.

Driven through the real tool loop, with only the registry and the model faked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, StepType


def _tool_call(name: str, args: dict | None = None, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args or {})
    return tc


def _response(content=None, tool_calls=None):
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


TOOL = "record_resolution"


@pytest.fixture
def runner(engine_config):
    from robothor.engine.runner import AgentRunner

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [{"type": "function", "function": {"name": TOOL}}]
        registry.get_tool_names.return_value = [TOOL]
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="example-agent",
        name="Example Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=30,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        error_feedback=False,
    )


async def _run_once(runner, agent_config, tool_result: dict):
    """One turn calling TOOL (which answers ``tool_result``), then plain text."""
    turns = {"n": 0}

    async def completion(**kwargs):
        turns["n"] += 1
        if turns["n"] == 1:
            return _response(tool_calls=[_tool_call(TOOL, {"open_item": "", "outcome": ""})])
        return _response(content="done")

    runner.registry.execute = AsyncMock(return_value=tool_result)
    with (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=completion),
    ):
        run = await runner.execute("example-agent", "go", agent_config=agent_config)
    steps = [s for s in run.steps if s.step_type == StepType.TOOL_CALL and s.tool_name == TOOL]
    assert len(steps) == 1
    return steps[0]


@pytest.mark.usefixtures("_mock_run_persistence")
class TestErrorResultsAreFailedSteps:
    @pytest.mark.asyncio
    async def test_an_error_string_is_the_steps_error_message(self, runner, agent_config):
        """AC1: a handler returning ``{"error": "..."}`` leaves a failed step."""
        step = await _run_once(
            runner, agent_config, {"error": "open_item and outcome are required"}
        )
        assert step.error_message == "open_item and outcome are required"

    @pytest.mark.asyncio
    async def test_a_structured_error_is_recorded_as_text(self, runner, agent_config):
        """AC1: an ``error`` that is an object, not a string, still fails the step.

        ``agent_run_steps.error_message`` is a text column; a dict there is
        either a failed insert (the step is lost) or, worse, unreadable.
        """
        step = await _run_once(
            runner,
            agent_config,
            {"error": {"message": "upstream refused the write", "code": 400}},
        )
        assert isinstance(step.error_message, str)
        assert "upstream refused the write" in step.error_message

    @pytest.mark.asyncio
    async def test_a_successful_result_leaves_no_error(self, runner, agent_config):
        step = await _run_once(runner, agent_config, {"resolution_id": 7, "superseded_ids": []})
        assert step.error_message is None


class TestErrorText:
    """The one reading of "did this tool call fail, and why" — shared by a
    turn's calls and a snippet's proxied calls, so the two cannot disagree."""

    def test_a_string_error_is_returned_as_is(self):
        from robothor.engine.tool_outcome import error_text

        assert error_text({"error": "bridge service offline"}) == "bridge service offline"

    def test_an_object_error_uses_its_message(self):
        from robothor.engine.tool_outcome import error_text

        assert error_text({"error": {"message": "refused", "code": 400}}) == "refused"

    def test_an_object_error_without_a_message_is_its_json(self):
        from robothor.engine.tool_outcome import error_text

        text = error_text({"error": {"code": 400}})
        assert isinstance(text, str) and "400" in text

    def test_a_bare_true_error_is_still_a_failure(self):
        from robothor.engine.tool_outcome import error_text

        text = error_text({"error": True, "message": "quota exceeded"})
        assert text == "quota exceeded"
        assert error_text({"error": True})

    @pytest.mark.parametrize("result", [{"ok": True}, {"error": None}, {"error": ""}, "text", None])
    def test_no_error_is_none(self, result):
        from robothor.engine.tool_outcome import error_text

        assert error_text(result) is None
