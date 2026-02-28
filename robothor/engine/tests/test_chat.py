"""Tests for the chat HTTP endpoints — SSE streaming webchat."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine.chat import _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType


@pytest.fixture
def mock_runner(engine_config):
    """Create a mock AgentRunner."""
    runner = MagicMock()
    runner.config = engine_config
    return runner


@pytest.fixture
def chat_app(engine_config, mock_runner):
    """Create a FastAPI app with chat router mounted."""
    from fastapi import FastAPI

    app = FastAPI()
    init_chat(mock_runner, engine_config)
    app.include_router(router)
    yield app
    # Clean up sessions between tests
    _sessions.clear()


@pytest.fixture
async def client(chat_app):
    """Async HTTP client for testing."""
    transport = ASGITransport(app=chat_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestChatSend:
    @pytest.mark.asyncio
    async def test_streams_sse_delta_and_done(self, client, mock_runner):
        """Verify delta + done events are streamed."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Hello from Robothor!",
            trigger_type=TriggerType.WEBCHAT,
        )

        async def fake_execute(**kwargs):
            on_content = kwargs.get("on_content")
            if on_content:
                await on_content("Hello")
                await on_content("Hello from")
                await on_content("Hello from Robothor!")
            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/send",
            json={"session_key": "test:main:web", "message": "hi"},
        )
        assert res.status_code == 200
        assert res.headers["content-type"] == "text/event-stream; charset=utf-8"

        body = res.text
        events = _parse_sse(body)

        # Should have delta events followed by done
        delta_events = [e for e in events if e["event"] == "delta"]
        done_events = [e for e in events if e["event"] == "done"]

        assert len(delta_events) >= 1
        assert len(done_events) == 1
        assert done_events[0]["data"]["text"] == "Hello from Robothor!"

    @pytest.mark.asyncio
    async def test_missing_fields_returns_400(self, client):
        """Missing session_key or message returns 400."""
        res = await client.post("/chat/send", json={"session_key": "x"})
        assert res.status_code == 400

        res = await client.post("/chat/send", json={"message": "hi"})
        assert res.status_code == 400

    @pytest.mark.asyncio
    async def test_busy_returns_409(self, client, mock_runner):
        """Concurrent request to same session returns 409."""
        # Simulate a slow agent
        slow_event = asyncio.Event()

        async def slow_execute(**kwargs):
            await slow_event.wait()
            return AgentRun(status=RunStatus.COMPLETED, output_text="done")

        mock_runner.execute = AsyncMock(side_effect=slow_execute)

        # Start first request (won't complete until we set the event)
        task = asyncio.create_task(
            client.post(
                "/chat/send",
                json={"session_key": "busy:main:test", "message": "first"},
            )
        )
        # Give it a moment to start
        await asyncio.sleep(0.1)

        # Second request should get 409
        res2 = await client.post(
            "/chat/send",
            json={"session_key": "busy:main:test", "message": "second"},
        )
        assert res2.status_code == 409

        # Clean up
        slow_event.set()
        await task


class TestChatHistory:
    @pytest.mark.asyncio
    async def test_empty_history(self, client):
        """Empty session returns empty messages."""
        res = await client.get("/chat/history?session_key=new-session")
        assert res.status_code == 200
        data = res.json()
        assert data["sessionKey"] == "new-session"
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_history_after_send(self, client, mock_runner):
        """History includes messages after a chat.send."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="I'm here!",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        # Send a message
        await client.post(
            "/chat/send",
            json={"session_key": "hist:main:test", "message": "hello"},
        )

        # Check history
        res = await client.get("/chat/history?session_key=hist:main:test")
        data = res.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"
        assert data["messages"][1]["role"] == "assistant"
        assert data["messages"][1]["content"] == "I'm here!"

    @pytest.mark.asyncio
    async def test_missing_session_key_returns_400(self, client):
        """Missing session_key returns 400."""
        res = await client.get("/chat/history")
        assert res.status_code == 400


class TestChatInject:
    @pytest.mark.asyncio
    async def test_inject_adds_system_message(self, client):
        """Inject adds a system message to history."""
        res = await client.post(
            "/chat/inject",
            json={
                "session_key": "inject:main:test",
                "message": "You have a canvas",
                "label": "canvas-init",
            },
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True

        # Verify it's in history
        hist = await client.get("/chat/history?session_key=inject:main:test")
        messages = hist.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You have a canvas"


class TestChatAbort:
    @pytest.mark.asyncio
    async def test_abort_with_no_active_task(self, client):
        """Abort when nothing is running returns ok but aborted=False."""
        res = await client.post(
            "/chat/abort",
            json={"session_key": "abort:main:test"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["aborted"] is False


class TestChatClear:
    @pytest.mark.asyncio
    async def test_clear_resets_session(self, client):
        """Clear removes all history."""
        # Add some messages
        await client.post(
            "/chat/inject",
            json={"session_key": "clear:main:test", "message": "msg1"},
        )
        await client.post(
            "/chat/inject",
            json={"session_key": "clear:main:test", "message": "msg2"},
        )

        # Verify they exist
        hist = await client.get("/chat/history?session_key=clear:main:test")
        assert len(hist.json()["messages"]) == 2

        # Clear
        res = await client.post(
            "/chat/clear",
            json={"session_key": "clear:main:test"},
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True

        # Verify cleared
        hist = await client.get("/chat/history?session_key=clear:main:test")
        assert len(hist.json()["messages"]) == 0


class TestToolEvents:
    @pytest.mark.asyncio
    async def test_sse_includes_tool_start_and_end(self, client, mock_runner):
        """Verify tool_start and tool_end events appear in the SSE stream."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Found 3 tasks.",
            trigger_type=TriggerType.WEBCHAT,
            model_used="test-model",
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
        )

        async def fake_execute(**kwargs):
            on_content = kwargs.get("on_content")
            on_tool = kwargs.get("on_tool")

            # Simulate tool call
            if on_tool:
                await on_tool(
                    {
                        "event": "tool_start",
                        "tool": "list_tasks",
                        "args": {"status": "TODO"},
                        "call_id": "call_1",
                    }
                )
                await on_tool(
                    {
                        "event": "tool_end",
                        "tool": "list_tasks",
                        "call_id": "call_1",
                        "duration_ms": 42,
                        "result_preview": "[]",
                        "error": None,
                    }
                )

            # Simulate content
            if on_content:
                await on_content("Found 3 tasks.")

            return run

        mock_runner.execute = AsyncMock(side_effect=fake_execute)

        res = await client.post(
            "/chat/send",
            json={"session_key": "tool:main:test", "message": "list tasks"},
        )
        assert res.status_code == 200

        events = _parse_sse(res.text)

        tool_start = [e for e in events if e["event"] == "tool_start"]
        tool_end = [e for e in events if e["event"] == "tool_end"]
        done = [e for e in events if e["event"] == "done"]

        assert len(tool_start) == 1
        assert tool_start[0]["data"]["tool"] == "list_tasks"
        assert tool_start[0]["data"]["call_id"] == "call_1"

        assert len(tool_end) == 1
        assert tool_end[0]["data"]["duration_ms"] == 42

        # Done event should include metadata
        assert len(done) == 1
        assert done[0]["data"]["model"] == "test-model"
        assert done[0]["data"]["input_tokens"] == 100
        assert done[0]["data"]["output_tokens"] == 50
        assert done[0]["data"]["duration_ms"] == 500

    @pytest.mark.asyncio
    async def test_done_event_enriched_with_metadata(self, client, mock_runner):
        """Done event includes model, tokens, and duration."""
        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="Hello!",
            trigger_type=TriggerType.WEBCHAT,
            model_used="openrouter/kimi/k2.5",
            input_tokens=200,
            output_tokens=100,
            duration_ms=1234,
        )

        mock_runner.execute = AsyncMock(return_value=run)

        res = await client.post(
            "/chat/send",
            json={"session_key": "meta:main:test", "message": "hi"},
        )

        events = _parse_sse(res.text)
        done = [e for e in events if e["event"] == "done"]
        assert len(done) == 1
        data = done[0]["data"]
        assert data["model"] == "openrouter/kimi/k2.5"
        assert data["input_tokens"] == 200
        assert data["output_tokens"] == 100
        assert data["duration_ms"] == 1234


class TestHistoryTrimming:
    @pytest.mark.asyncio
    async def test_max_history_cap(self, client, mock_runner):
        """History is trimmed to MAX_HISTORY entries."""
        from robothor.engine.chat import MAX_HISTORY, _get_session

        session = _get_session("trim:main:test")

        # Manually fill history beyond limit
        for i in range(MAX_HISTORY + 10):
            session.history.append({"role": "user", "content": f"msg {i}"})

        run = AgentRun(
            status=RunStatus.COMPLETED,
            output_text="reply",
            trigger_type=TriggerType.WEBCHAT,
        )
        mock_runner.execute = AsyncMock(return_value=run)

        # Send triggers trim
        await client.post(
            "/chat/send",
            json={"session_key": "trim:main:test", "message": "trigger"},
        )

        assert len(session.history) <= MAX_HISTORY


def _parse_sse(body: str) -> list[dict]:
    """Parse SSE text into a list of {event, data} dicts."""
    events = []
    current_event = ""
    for line in body.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append({"event": current_event, "data": data})
    return events
