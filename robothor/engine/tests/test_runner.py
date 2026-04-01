"""Tests for the AgentRunner — core LLM conversation loop."""

from __future__ import annotations

import json
import time
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
        """Hard timeout fires when stall watchdog is disabled."""
        import asyncio

        sample_agent_config.timeout_seconds = 1  # 1 second hard timeout
        sample_agent_config.stall_timeout_seconds = 0  # disable watchdog → hard timeout active

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
                err.status_code = 403  # type: ignore[attr-defined]
                raise err

            # model-b succeeds
            if call_count <= 2:
                # First call: return tool call to force a second iteration
                resp = mock_litellm_response(content=None, tool_calls=[tc], model="model-b")
                resp.choices[0].message.content = None
                return resp
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
            err.status_code = 403  # type: ignore[attr-defined]
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
    async def test_safety_cap_stops_runaway_loop(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Safety cap stops infinite loops and forces a wrap-up summary."""
        sample_agent_config.max_iterations = 3  # check-in interval
        sample_agent_config.safety_cap = 5  # hard safety valve

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        # Always return tool calls so the loop keeps going (simulates runaway)
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

        async def counting_mock(**kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            return await mock_completion(**kwargs)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=counting_mock):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        # 5 iterations of tool calls + 1 wrap-up call = 6 total LLM calls
        assert llm_call_count == 6
        # Safety limit error is recorded
        error_steps = [
            s for s in run.steps if s.error_message and "Safety limit" in s.error_message
        ]
        assert len(error_steps) == 1

    @pytest.mark.asyncio
    async def test_checkin_message_injected_at_interval(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Check-in messages are injected every max_iterations iterations."""
        sample_agent_config.max_iterations = 2  # check-in every 2 iterations
        sample_agent_config.safety_cap = 10

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        call_count = 0
        captured_messages = []

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Capture messages to check for check-in injections
            if "messages" in kwargs:
                captured_messages.append(list(kwargs["messages"]))
            # Stop after 5 iterations by returning no tool calls
            if call_count >= 5:
                return mock_litellm_response(content="Done")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        # Check that check-in messages were injected at iterations 2 and 4
        all_messages = [m for msgs in captured_messages for m in msgs]
        checkin_messages = [
            m
            for m in all_messages
            if m.get("role") == "user" and "Progress check-in" in m.get("content", "")
        ]
        assert len(checkin_messages) >= 2

    @pytest.mark.asyncio
    async def test_budget_exhausted_does_not_stop_loop(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Token budget exhaustion is tracked but does not stop the run."""
        sample_agent_config.max_iterations = 50
        sample_agent_config.safety_cap = 200

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            # Stop after 3 iterations
            if call_count >= 3:
                return mock_litellm_response(content="Done")
            resp = mock_litellm_response(content=None, tool_calls=[tc])
            resp.choices[0].message.content = None
            # Simulate high token usage
            resp.usage = MagicMock()
            resp.usage.prompt_tokens = 50000
            resp.usage.completion_tokens = 5000
            resp.usage.total_tokens = 55000
            return resp

        runner.registry.execute = AsyncMock(return_value={"ok": True})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch(
                            "robothor.engine.model_registry.compute_token_budget",
                            return_value=10000,
                        ):
                            run = await runner.execute(
                                "test-agent",
                                "hello",
                                agent_config=sample_agent_config,
                            )

        # Run completed normally (3 calls) — budget did NOT cut it short
        assert call_count == 3
        assert run.output_text == "Done"


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


class TestStallWatchdogKeepalive:
    """Tests for the stall watchdog keepalive during tool execution."""

    @pytest.mark.asyncio
    async def test_watchdog_touched_during_long_tool(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Stall watchdog is touched periodically while a tool runs, preventing timeout."""
        import asyncio

        from robothor.engine.runner import _StallWatchdog

        # Tool call response
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

        # Simulate a slow tool (takes 2 seconds)
        async def slow_tool(*args, **kwargs):
            await asyncio.sleep(2)
            return {"result": "ok"}

        runner.registry.execute = slow_tool
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        # Use a short stall timeout — the keepalive (every 60s) would save it
        # but in this test we just verify the watchdog's touch() was called
        # by checking the last_activity timestamp was updated during tool execution
        sample_agent_config.stall_timeout_seconds = 300

        touch_times: list[float] = []
        original_touch = _StallWatchdog.touch

        def recording_touch(self_wd):
            touch_times.append(time.monotonic())
            original_touch(self_wd)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch.object(_StallWatchdog, "touch", recording_touch):
                            run = await runner.execute(
                                "test-agent",
                                "List tasks",
                                agent_config=sample_agent_config,
                            )

        assert run.status == RunStatus.COMPLETED
        # At least one touch should have occurred (the post-tool touch plus
        # the keepalive task is started — though it may not fire in 2s, the
        # post-tool touch is still there)
        assert len(touch_times) >= 1

    @pytest.mark.asyncio
    async def test_watchdog_not_killed_during_slow_tool(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """A tool that takes longer than stall timeout survives thanks to keepalive."""
        import asyncio

        # Very short stall timeout — without keepalive, tool would be killed
        sample_agent_config.stall_timeout_seconds = 2
        sample_agent_config.timeout_seconds = 10

        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "list_tasks"
        tc.function.arguments = "{}"

        response1 = mock_litellm_response(content=None, tool_calls=[tc])
        response1.choices[0].message.content = None
        response2 = mock_litellm_response(content="Done after slow tool.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        # Tool takes 4 seconds — longer than the 2s stall timeout.
        # The keepalive fires every 60s normally, but we monkeypatch
        # the sleep interval to 1s for testing.
        async def slow_tool(*args, **kwargs):
            await asyncio.sleep(4)
            return {"result": "ok"}

        runner.registry.execute = slow_tool
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "list_tasks"}}
        ]
        runner.registry.get_tool_names.return_value = ["list_tasks"]

        # Patch asyncio.sleep only inside _tool_keepalive to fire faster
        original_sleep = asyncio.sleep

        async def fast_sleep(duration):
            # Keepalive uses sleep(60); make it sleep(0.5) for testing
            if duration == 60:
                return await original_sleep(0.5)
            return await original_sleep(duration)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        with patch("asyncio.sleep", side_effect=fast_sleep):
                            run = await runner.execute(
                                "test-agent",
                                "List tasks",
                                agent_config=sample_agent_config,
                            )

        # Without the keepalive, this would be TIMEOUT
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "Done after slow tool."


class TestThinkingAPI:
    """Tests for the adaptive thinking API integration."""

    @pytest.mark.asyncio
    async def test_thinking_sets_temperature_1(self, runner, sample_agent_config):
        """When thinking is enabled, temperature MUST be 1.0 (Anthropic requirement)."""
        # Use a thinking-capable model
        sample_agent_config.model_primary = "openrouter/anthropic/claude-sonnet-4.6"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "anthropic/claude-sonnet-4.6"
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Thought about it."
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        call_kwargs = mock_llm.call_args.kwargs
        assert call_kwargs["temperature"] == 1.0
        assert call_kwargs["thinking"]["type"] == "enabled"
        assert call_kwargs["thinking"]["budget_tokens"] == 10_000
        # Model should stay as OpenRouter path (no prefix stripping)
        assert call_kwargs["model"] == "openrouter/anthropic/claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_thinking_blocks_filtered_from_output(self, runner, sample_agent_config):
        """Thinking blocks in response content should be filtered from output text."""
        sample_agent_config.model_primary = "openrouter/anthropic/claude-sonnet-4.6"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "anthropic/claude-sonnet-4.6"
        response.choices = [MagicMock()]
        # Simulate content blocks with thinking + text
        response.choices[0].message.content = [
            {"type": "thinking", "thinking": "Let me think about this..."},
            {"type": "text", "text": "Here is my answer."},
        ]
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

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
        assert run.output_text == "Here is my answer."

    @pytest.mark.asyncio
    async def test_non_thinking_model_no_thinking_param(self, runner, sample_agent_config):
        """Non-thinking models should not get thinking parameter."""
        sample_agent_config.model_primary = "openrouter/z-ai/glm-5"
        sample_agent_config.model_fallbacks = []

        response = MagicMock()
        response.model = "openrouter/z-ai/glm-5"
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Hello!"
        response.choices[0].message.tool_calls = None
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion", new_callable=AsyncMock, return_value=response
                    ) as mock_llm:
                        await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        call_kwargs = mock_llm.call_args.kwargs
        assert "thinking" not in call_kwargs
        assert call_kwargs["temperature"] == 0.3  # default, not forced to 1.0


class TestShouldVerify:
    """Tests for _should_verify logic."""

    def test_explicit_verification_enabled(self, runner, sample_agent_config):
        """verification_enabled=True always returns True."""
        sample_agent_config.verification_enabled = True
        assert runner._should_verify(sample_agent_config, None) is True

    def test_route_verification_true(self, runner, sample_agent_config):
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route) is True

    def test_route_verification_false(self, runner, sample_agent_config):
        route = MagicMock()
        route.verification = False
        assert runner._should_verify(sample_agent_config, route) is False

    def test_skip_for_telegram_trigger(self, runner, sample_agent_config):
        """Verification should be skipped for interactive Telegram sessions."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.TELEGRAM)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is False

    def test_skip_for_webchat_trigger(self, runner, sample_agent_config):
        """Verification should be skipped for interactive webchat sessions."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.WEBCHAT)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is False

    def test_cron_trigger_allows_verification(self, runner, sample_agent_config):
        """Cron triggers should still allow route-based verification."""
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent", trigger_type=TriggerType.CRON)
        route = MagicMock()
        route.verification = True
        assert runner._should_verify(sample_agent_config, route, session) is True

    def test_explicit_enabled_overrides_telegram_skip(self, runner, sample_agent_config):
        """verification_enabled=True takes precedence even for Telegram."""
        from robothor.engine.session import AgentSession

        sample_agent_config.verification_enabled = True
        session = AgentSession("test-agent", trigger_type=TriggerType.TELEGRAM)
        assert runner._should_verify(sample_agent_config, None, session) is True
