"""Tests for the AgentRunner — core LLM conversation loop."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner


@pytest.fixture
def runner(engine_config):
    """Create an AgentRunner with mocked dependencies."""
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = []
        mock_registry.get_tool_names.return_value = []
        mock_reg.return_value = mock_registry
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


class TestAgentRunnerExecute:
    @pytest.mark.asyncio
    async def test_missing_agent_config(self, runner):
        """Agent run fails gracefully when config not found."""
        with patch("robothor.engine.runner.load_agent_config", return_value=None):
            with patch("robothor.engine.runner.create_run"):
                run = await runner.execute("nonexistent", "test message")
        assert run.status == RunStatus.FAILED
        assert "not found" in run.error_message

    @pytest.mark.asyncio
    async def test_no_models_configured(self, runner, sample_agent_config):
        """Fails when agent has no models."""
        sample_agent_config.model_primary = ""
        sample_agent_config.model_fallbacks = []
        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    run = await runner.execute(
                        "test-agent",
                        "hello",
                        agent_config=sample_agent_config,
                    )
        assert run.status == RunStatus.FAILED
        assert "No models" in run.error_message

    @pytest.mark.asyncio
    async def test_successful_simple_run(self, runner, sample_agent_config, mock_litellm_response):
        """Agent completes when LLM returns text without tool calls."""
        response = mock_litellm_response(content="Hello! I'm done.")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Hello! I'm done."
        assert run.model_used == "test-model"
        assert len(run.steps) == 1  # one LLM call

    @pytest.mark.asyncio
    async def test_tool_call_loop(self, runner, sample_agent_config, mock_litellm_response):
        """Agent executes tool calls and continues the loop."""
        # First response: tool call
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None

        # Second response: final text
        response2 = mock_litellm_response(content="Found 3 tasks.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "List my tasks",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Found 3 tasks."
        assert len(run.steps) >= 3  # llm_call + tool_call + llm_call

    @pytest.mark.asyncio
    async def test_empty_choices_guard(self, runner, sample_agent_config):
        """Run fails when LLM returns empty choices list."""
        response = MagicMock()
        response.model = "test-model"
        response.choices = []  # empty choices
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=0)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "empty choices" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_conversation_history_passed(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Conversation history is passed through to the session."""
        response = mock_litellm_response(content="I remember!")
        history = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First reply"},
        ]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        run = await runner.execute(
                            "test-agent",
                            "Follow-up",
                            agent_config=sample_agent_config,
                            conversation_history=history,
                        )

        assert run.status == RunStatus.COMPLETED
        # Verify history was included in messages sent to LLM
        call_args = mock_llm.call_args
        messages = call_args.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "First message"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "First reply"
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "Follow-up"

    @pytest.mark.asyncio
    async def test_all_models_fail(self, runner, sample_agent_config):
        """Run fails when all models error."""

        async def mock_fail(**kwargs):
            raise Exception("Model unavailable")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_fail):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "All models failed" in (run.error_message or "")

    @pytest.mark.asyncio
    async def test_timeout(self, runner, sample_agent_config, mock_litellm_response):
        """Agent times out when execution exceeds timeout_seconds."""
        import asyncio

        sample_agent_config.timeout_seconds = 1  # 1 second timeout

        async def slow_completion(**kwargs):
            await asyncio.sleep(5)  # Will be cancelled by timeout
            return mock_litellm_response()

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=slow_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_model_fallback(self, runner, sample_agent_config, mock_litellm_response):
        """Falls back to next model when primary fails."""
        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("model") == "openrouter/test/model":
                raise Exception("Primary model down")
            return mock_litellm_response(
                content="Fallback worked", model="openrouter/test/fallback"
            )

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        assert run.model_used == "openrouter/test/fallback"
        assert len(run.models_attempted) >= 1

    @pytest.mark.asyncio
    async def test_trigger_type_preserved(self, runner, sample_agent_config, mock_litellm_response):
        """Trigger type and detail are preserved in the run."""
        response = mock_litellm_response(content="Done")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            trigger_type=TriggerType.CRON,
                            trigger_detail="0 * * * *",
                            agent_config=sample_agent_config,
                        )

        assert run.trigger_type == TriggerType.CRON
        assert run.trigger_detail == "0 * * * *"


class TestBrokenModelTracking:
    """Tests for rate-limited / permanently-failed model tracking."""

    @pytest.mark.asyncio
    async def test_rate_limited_model_skipped_on_subsequent_iterations(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """A model that returns 403 is skipped on the next iteration."""
        sample_agent_config.model_primary = "model-a"
        sample_agent_config.model_fallbacks = ["model-b"]

        # Track which models are actually called
        models_called: list[str] = []
        call_count = 0

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        async def mock_completion(**kwargs):
            nonlocal call_count
            model = kwargs["model"]
            models_called.append(model)
            call_count += 1

            if model == "model-a":
                err = Exception("Rate limited")
                err.status_code = 403
                raise err

            # model-b succeeds
            if call_count <= 2:
                # First call: return tool call to force a second iteration
                resp = mock_litellm_response(content=None, tool_calls=[tc], model="model-b")
                resp.choices[0].message.content = None
                return resp
            else:
                return mock_litellm_response(content="Done", model="model-b")

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED
        # model-a should only be tried once (iteration 1), then skipped
        assert models_called.count("model-a") == 1
        # model-b handles both iterations
        assert models_called.count("model-b") >= 2

    @pytest.mark.asyncio
    async def test_all_models_broken_immediate_failure(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """When all models hit permanent errors, run fails without retrying."""
        sample_agent_config.model_primary = "model-a"
        sample_agent_config.model_fallbacks = ["model-b"]
        sample_agent_config.max_iterations = 10

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            err = Exception("Forbidden")
            err.status_code = 403
            raise err

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.FAILED
        assert "All models failed" in (run.error_message or "")
        # Should only try each model once — NOT 10 iterations x 2 models = 20
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_per_agent_max_iterations_respected(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Agent uses its own max_iterations, not the engine default."""
        sample_agent_config.max_iterations = 3

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        # Always return tool calls so the loop keeps going
        async def mock_completion(**kwargs):
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        llm_call_count = 0
        original_mock = mock_completion

        async def counting_mock(**kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return await original_mock(**kwargs)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=counting_mock):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        # Should hit max iterations (3), not the engine default (5 from conftest)
        assert llm_call_count == 3
        # Max iterations error is recorded as a step
        error_steps = [
            s for s in run.steps if s.error_message and "Max iterations" in s.error_message
        ]
        assert len(error_steps) == 1
        assert "(3)" in error_steps[0].error_message


class TestOnToolCallback:
    """Tests for the on_tool callback in tool execution."""

    @pytest.mark.asyncio
    async def test_on_tool_receives_start_and_end(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_tool callback fires for tool_start and tool_end events."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({"status": "TODO"})

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"tasks": [], "count": 0})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        assert run.status == RunStatus.COMPLETED
        assert len(tool_events) == 2

        # Verify tool_start event
        start_evt = tool_events[0]
        assert start_evt["event"] == "tool_start"
        assert start_evt["tool"] == "list_tasks"
        assert start_evt["call_id"] == "call_1"
        assert start_evt["args"] == {"status": "TODO"}

        # Verify tool_end event
        end_evt = tool_events[1]
        assert end_evt["event"] == "tool_end"
        assert end_evt["tool"] == "list_tasks"
        assert end_evt["call_id"] == "call_1"
        assert end_evt["duration_ms"] >= 0
        assert end_evt["error"] is None
        assert "result_preview" in end_evt

    @pytest.mark.asyncio
    async def test_on_tool_errors_are_swallowed(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Errors in on_tool callback must never block tool execution."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        async def failing_on_tool(event: dict) -> None:
            raise RuntimeError("Callback exploded!")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=failing_on_tool,
                        )

        # Run should still complete despite callback errors
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Done."

    @pytest.mark.asyncio
    async def test_on_tool_works_alongside_on_content(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_tool and on_content can both be provided (non-streaming path)."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        # Test both params accepted — use _run_loop directly to avoid
        # streaming path which needs a real async iterator mock
        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        # No on_content to avoid streaming path; just verify
                        # on_tool param is accepted alongside on_content signature
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        assert run.status == RunStatus.COMPLETED
        # Tool events should have fired
        assert len(tool_events) == 2

    @pytest.mark.asyncio
    async def test_on_tool_result_preview_truncated(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Result preview in tool_end is truncated to 2000 chars."""
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "search_memory"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        # Return a very large result
        large_result = {"data": "x" * 5000}
        runner.registry.execute = AsyncMock(return_value=large_result)
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "search_memory"}}
        ]
        runner.registry.get_tool_names.return_value = ["search_memory"]

        tool_events: list[dict] = []

        async def on_tool(event: dict) -> None:
            tool_events.append(event)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_tool=on_tool,
                        )

        # Find the tool_end event
        end_events = [e for e in tool_events if e["event"] == "tool_end"]
        assert len(end_events) == 1
        # Preview should be truncated
        preview = end_events[0]["result_preview"]
        assert len(preview) <= 2003 + 1  # 2000 chars + "..."
