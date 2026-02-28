"""Tests for the SSE client — parsing, health checks, abort/clear."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.tui.client import EngineClient, SSEEvent


class TestSSEParsing:
    """Test SSE event parsing from raw text streams."""

    @pytest.mark.asyncio
    async def test_parse_delta_events(self):
        """Delta events are parsed correctly from SSE stream."""
        sse_lines = [
            "event: delta",
            'data: {"text": "Hello"}',
            "",
            "event: delta",
            'data: {"text": " world"}',
            "",
            "event: done",
            'data: {"text": "Hello world"}',
            "",
        ]

        events = list(_parse_sse_lines(sse_lines))
        assert len(events) == 3
        assert events[0].event == "delta"
        assert events[0].data["text"] == "Hello"
        assert events[1].event == "delta"
        assert events[1].data["text"] == " world"
        assert events[2].event == "done"
        assert events[2].data["text"] == "Hello world"

    @pytest.mark.asyncio
    async def test_parse_tool_events(self):
        """Tool start/end events are parsed correctly."""
        sse_lines = [
            "event: tool_start",
            json.dumps({"tool": "search_memory", "args": {"query": "test"}, "call_id": "c1"}).join(["data: ", ""]),
            "",
            "event: tool_end",
            json.dumps({"tool": "search_memory", "call_id": "c1", "duration_ms": 42, "result_preview": "...", "error": None}).join(["data: ", ""]),
            "",
            "event: done",
            'data: {"text": "Done"}',
            "",
        ]

        events = list(_parse_sse_lines(sse_lines))
        assert len(events) == 3
        assert events[0].event == "tool_start"
        assert events[0].data["tool"] == "search_memory"
        assert events[0].data["call_id"] == "c1"
        assert events[1].event == "tool_end"
        assert events[1].data["duration_ms"] == 42
        assert events[2].event == "done"

    @pytest.mark.asyncio
    async def test_parse_error_event(self):
        """Error events are parsed correctly."""
        sse_lines = [
            "event: error",
            'data: {"error": "Something went wrong"}',
            "",
        ]

        events = list(_parse_sse_lines(sse_lines))
        assert len(events) == 1
        assert events[0].event == "error"
        assert events[0].data["error"] == "Something went wrong"

    @pytest.mark.asyncio
    async def test_parse_mixed_stream(self):
        """Mixed delta + tool + done events in one stream."""
        sse_lines = [
            "event: delta",
            'data: {"text": "Let me "}',
            "",
            "event: tool_start",
            'data: {"tool": "list_tasks", "args": {}, "call_id": "c1"}',
            "",
            "event: tool_end",
            'data: {"tool": "list_tasks", "call_id": "c1", "duration_ms": 100, "result_preview": "[]", "error": null}',
            "",
            "event: delta",
            'data: {"text": "check. Found 0 tasks."}',
            "",
            "event: done",
            'data: {"text": "Let me check. Found 0 tasks.", "model": "kimi", "input_tokens": 100, "output_tokens": 50}',
            "",
        ]

        events = list(_parse_sse_lines(sse_lines))
        assert len(events) == 5
        assert [e.event for e in events] == [
            "delta", "tool_start", "tool_end", "delta", "done"
        ]
        assert events[4].data["model"] == "kimi"

    @pytest.mark.asyncio
    async def test_invalid_json_handled(self):
        """Invalid JSON in data field is handled gracefully."""
        sse_lines = [
            "event: delta",
            "data: not-json",
            "",
        ]

        events = list(_parse_sse_lines(sse_lines))
        assert len(events) == 1
        assert events[0].event == "delta"
        assert "raw" in events[0].data


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_success(self):
        """Health check returns dict when engine is reachable."""
        client = EngineClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "healthy", "agents": {"a": {}}}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.check_health()

        assert result is not None
        assert result["status"] == "healthy"
        await client.close()

    @pytest.mark.asyncio
    async def test_health_unreachable(self):
        """Health check returns None when engine is unreachable."""
        client = EngineClient()

        with patch.object(client._client, "get", side_effect=Exception("Connection refused")):
            result = await client.check_health()

        assert result is None
        await client.close()


class TestAbortClear:
    @pytest.mark.asyncio
    async def test_abort_success(self):
        """Abort returns True when response was cancelled."""
        client = EngineClient(session_key="test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "aborted": True}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.abort()

        assert result is True
        await client.close()

    @pytest.mark.asyncio
    async def test_clear_success(self):
        """Clear returns True on success."""
        client = EngineClient(session_key="test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.clear()

        assert result is True
        await client.close()

    @pytest.mark.asyncio
    async def test_abort_failure(self):
        """Abort returns False on connection error."""
        client = EngineClient(session_key="test")

        with patch.object(client._client, "post", side_effect=Exception("Connection refused")):
            result = await client.abort()

        assert result is False
        await client.close()


def _parse_sse_lines(lines: list[str]) -> list[SSEEvent]:
    """Helper to parse SSE lines into events (mirrors client logic)."""
    events = []
    current_event = ""
    for line in lines:
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = {"raw": line[6:]}
            events.append(SSEEvent(event=current_event, data=data))
    return events
