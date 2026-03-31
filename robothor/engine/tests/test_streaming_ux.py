"""Tests for streaming UX improvements — on_status lifecycle events."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import RunStatus
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


class TestOnStatusLifecycle:
    """Tests for the on_status callback that emits lifecycle events."""

    @pytest.mark.asyncio
    async def test_on_status_emits_iteration_start(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_status callback fires iteration_start at the top of each loop iteration."""
        events = []

        async def on_status(event):
            events.append(event)

        response = mock_litellm_response(content="Hello!")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion",
                        new_callable=AsyncMock,
                        return_value=response,
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_status=on_status,
                        )

        assert run.status == RunStatus.COMPLETED

        iteration_events = [e for e in events if e["event"] == "iteration_start"]
        assert len(iteration_events) >= 1
        assert iteration_events[0]["iteration"] == 1
        assert "safety_cap" in iteration_events[0]

    @pytest.mark.asyncio
    async def test_on_status_emits_tools_start_and_done(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """on_status fires tools_start before and tools_done after tool execution."""
        events = []

        async def on_status(event):
            events.append(event)

        # First response: tool calls
        tc1 = MagicMock()
        tc1.id = "call_1"
        tc1.function.name = "search_memory"
        tc1.function.arguments = json.dumps({"query": "test"})

        tc2 = MagicMock()
        tc2.id = "call_2"
        tc2.function.name = "read_file"
        tc2.function.arguments = json.dumps({"path": "test.txt"})

        response1 = mock_litellm_response(content=None, tool_calls=[tc1, tc2])
        response1.choices[0].message.content = None

        # Second response: final text
        response2 = mock_litellm_response(content="Done.")

        call_count = 0

        async def mock_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            return response1 if call_count == 1 else response2

        runner.registry.execute = AsyncMock(return_value={"result": "ok"})
        runner.registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "search_memory"}},
            {"type": "function", "function": {"name": "read_file"}},
        ]
        runner.registry.get_tool_names.return_value = ["search_memory", "read_file"]

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch("litellm.acompletion", side_effect=mock_completion):
                        run = await runner.execute(
                            "test-agent",
                            "search for something",
                            agent_config=sample_agent_config,
                            on_status=on_status,
                        )

        assert run.status == RunStatus.COMPLETED

        tools_start_events = [e for e in events if e["event"] == "tools_start"]
        tools_done_events = [e for e in events if e["event"] == "tools_done"]
        assert len(tools_start_events) >= 1
        assert len(tools_done_events) >= 1

        # Check tools_start has correct tool names
        ts = tools_start_events[0]
        assert set(ts["tools"]) == {"search_memory", "read_file"}
        assert ts["count"] == 2

        # Verify ordering: tools_start comes before tools_done
        start_idx = events.index(tools_start_events[0])
        done_idx = events.index(tools_done_events[0])
        assert start_idx < done_idx

    @pytest.mark.asyncio
    async def test_on_status_none_no_crash(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Runner works normally when on_status is not provided (default None)."""
        response = mock_litellm_response(content="Hello!")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion",
                        new_callable=AsyncMock,
                        return_value=response,
                    ):
                        # No on_status — should not crash
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                        )

        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_on_status_exception_suppressed(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """Exceptions in on_status callback don't crash the runner."""

        async def bad_status(event):
            raise RuntimeError("boom")

        response = mock_litellm_response(content="Hello!")

        with patch("robothor.engine.runner.create_run"):
            with patch("robothor.engine.runner.update_run"):
                with patch("robothor.engine.runner.create_step"):
                    with patch(
                        "litellm.acompletion",
                        new_callable=AsyncMock,
                        return_value=response,
                    ):
                        run = await runner.execute(
                            "test-agent",
                            "hello",
                            agent_config=sample_agent_config,
                            on_status=bad_status,
                        )

        # Should complete despite callback errors
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_on_status_and_on_tool_ordering(
        self, runner, sample_agent_config, mock_litellm_response
    ):
        """tools_start fires before individual tool events, tools_done after all tool events."""
        events = []

        async def on_status(event):
            events.append(("status", event["event"]))

        async def on_tool(event):
            events.append(("tool", event["event"]))

        # Tool call response
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

        runner.registry.execute = AsyncMock(return_value={"tasks": []})
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
                            "list tasks",
                            agent_config=sample_agent_config,
                            on_status=on_status,
                            on_tool=on_tool,
                        )

        assert run.status == RunStatus.COMPLETED

        # Find the tool-related events
        tool_related = [
            e for e in events if e[1] in ("tools_start", "tools_done", "tool_start", "tool_end")
        ]

        # Ordering: tools_start → tool_start → tool_end → tools_done
        assert tool_related[0] == ("status", "tools_start")
        assert tool_related[-1] == ("status", "tools_done")
        # Individual tool events are in between
        assert ("tool", "tool_start") in tool_related
        assert ("tool", "tool_end") in tool_related
