"""Tests for managed_agents.runner — the MA execution function."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.managed_agents.runner import run_on_managed_agents


def _mock_stream_events(events: list[dict[str, Any]]) -> Any:
    """Create a mock stream that yields the given events."""

    async def _stream(session_id: str):
        for event in events:
            yield event

    return _stream


@pytest.fixture()
def mock_client():
    client = AsyncMock()
    client.create_session = AsyncMock(return_value={"id": "session_test", "usage": {}})
    client.send_events = AsyncMock()
    return client


@pytest.fixture()
def mock_mapper():
    mapper = AsyncMock()
    mapper.get_or_create_agent = AsyncMock(return_value={"id": "agent_ma", "version": 1})
    mapper.get_or_create_environment = AsyncMock(return_value="env_ma")
    return mapper


class TestRunOnManagedAgents:
    @pytest.mark.asyncio
    async def test_basic_text_response(self, mock_client, mock_mapper):
        """Agent returns a simple text message, then goes idle."""
        mock_client.stream_session = _mock_stream_events(
            [
                {
                    "type": "agent.message",
                    "content": [{"type": "text", "text": "Hello, I can help!"}],
                },
                {"type": "session.status_idle"},
            ]
        )

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[{"type": "agent_toolset_20260401"}],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="You are a test agent",
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main",
                "Hello",
                tools=[{"type": "agent_toolset_20260401"}],
                system_prompt="Test",
                persist=False,
            )

        assert result.output_text == "Hello, I can help!"
        assert result.session_id == "session_test"

    @pytest.mark.asyncio
    async def test_custom_tool_roundtrip(self, mock_client, mock_mapper):
        """Agent calls a custom tool, we execute it and send the result."""
        mock_client.stream_session = _mock_stream_events(
            [
                {
                    "type": "agent.custom_tool_use",
                    "id": "tu_1",
                    "name": "search_memory",
                    "input": {"query": "test"},
                },
                {
                    "type": "agent.message",
                    "content": [{"type": "text", "text": "Found results."}],
                },
                {"type": "session.status_idle"},
            ]
        )

        async def mock_execute_tool(name, args, **kwargs):
            return {"results": [{"fact": "Test fact"}]}

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.tools.dispatch._execute_tool",
                side_effect=mock_execute_tool,
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main",
                "Search for test",
                tools=[],
                system_prompt="Test",
                persist=False,
            )

        assert result.output_text == "Found results."
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "search_memory"
        assert result.tool_calls[0]["is_error"] is False

        # Verify the result was sent back to MA
        send_calls = mock_client.send_events.call_args_list
        # First call: user.message, second call: custom_tool_result
        assert len(send_calls) >= 2
        result_event = send_calls[1][0][1][0]
        assert result_event["type"] == "user.custom_tool_result"
        assert result_event["custom_tool_use_id"] == "tu_1"

    @pytest.mark.asyncio
    async def test_custom_tool_error(self, mock_client, mock_mapper):
        """Custom tool raises an exception — error sent back to MA."""
        mock_client.stream_session = _mock_stream_events(
            [
                {
                    "type": "agent.custom_tool_use",
                    "id": "tu_2",
                    "name": "broken_tool",
                    "input": {},
                },
                {"type": "session.status_idle"},
            ]
        )

        async def mock_execute_tool(name, args, **kwargs):
            raise RuntimeError("Tool exploded")

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.tools.dispatch._execute_tool",
                side_effect=mock_execute_tool,
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main", "test", tools=[], system_prompt="", persist=False
            )

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["is_error"] is True

        # Verify error was sent to MA
        result_event = mock_client.send_events.call_args_list[1][0][1][0]
        assert result_event["is_error"] is True

    @pytest.mark.asyncio
    async def test_outcome_evaluation(self, mock_client, mock_mapper):
        """Outcome events are captured in the result."""
        mock_client.stream_session = _mock_stream_events(
            [
                {
                    "type": "agent.message",
                    "content": [{"type": "text", "text": "Done."}],
                },
                {
                    "type": "span.outcome_evaluation_end",
                    "result": "satisfied",
                    "explanation": "All criteria met",
                    "iteration": 1,
                },
                {"type": "session.status_idle"},
            ]
        )

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main",
                "Build report",
                tools=[],
                system_prompt="",
                outcome_rubric="## Quality\n- Correct",
                persist=False,
            )

        assert result.outcome_result == "satisfied"
        assert result.outcome_explanation == "All criteria met"

    @pytest.mark.asyncio
    async def test_builtin_tool_use_recorded(self, mock_client, mock_mapper):
        """Built-in MA tool calls are recorded but not executed locally."""
        mock_client.stream_session = _mock_stream_events(
            [
                {"type": "agent.tool_use", "name": "bash", "id": "tu_3"},
                {
                    "type": "agent.tool_result",
                    "tool_use_id": "tu_3",
                    "content": [{"type": "text", "text": "ok"}],
                },
                {"type": "session.status_idle"},
            ]
        )

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main", "run ls", tools=[], system_prompt="", persist=False
            )

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["builtin"] is True
        assert result.tool_calls[0]["name"] == "bash"

    @pytest.mark.asyncio
    async def test_on_content_callback(self, mock_client, mock_mapper):
        """on_content callback receives streamed text chunks."""
        mock_client.stream_session = _mock_stream_events(
            [
                {"type": "agent.message", "content": [{"type": "text", "text": "Hello "}]},
                {"type": "agent.message", "content": [{"type": "text", "text": "world"}]},
                {"type": "session.status_idle"},
            ]
        )

        chunks: list[str] = []

        async def capture(text: str):
            chunks.append(text)

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main",
                "hi",
                tools=[],
                system_prompt="",
                on_content=capture,
                persist=False,
            )

        assert chunks == ["Hello ", "world"]
        assert result.output_text == "Hello world"

    @pytest.mark.asyncio
    async def test_session_terminated_breaks_loop(self, mock_client, mock_mapper):
        """session.status_terminated also stops the stream consumer."""
        mock_client.stream_session = _mock_stream_events(
            [
                {"type": "agent.message", "content": [{"type": "text", "text": "Partial"}]},
                {"type": "session.status_terminated", "stop_reason": {"type": "error"}},
            ]
        )

        with (
            patch(
                "robothor.engine.managed_agents.runner.get_ma_client",
                return_value=mock_client,
            ),
            patch(
                "robothor.engine.managed_agents.tenant_mapper.get_tenant_mapper",
                return_value=mock_mapper,
            ),
            patch(
                "robothor.engine.managed_agents.runner._build_tools",
                return_value=[],
            ),
            patch(
                "robothor.engine.managed_agents.runner._load_system_prompt",
                return_value="",
            ),
            patch(
                "robothor.engine.managed_agents.runner._persist",
            ),
        ):
            result = await run_on_managed_agents(
                "main", "test", tools=[], system_prompt="", persist=False
            )

        assert result.output_text == "Partial"
