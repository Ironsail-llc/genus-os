"""Tests that dispatch.py catches unhandled handler exceptions.

Before this change, an unhandled exception in a tool handler (e.g. the
HTTPStatusError the `log_interaction` handler raises when the orchestrator
returns 500) propagated out of `_execute_tool`, crashed the runner, and
left the agent_runs row stuck in 'running' for the 30-minute reaper to
reclassify. Now the exception becomes a structured tool_crashed error.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.tools import dispatch


async def _boom_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    raise RuntimeError("orchestrator down")


async def _http_boom_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    # Simulate the real bug seen at Apr 23 10:02 on buddy agent
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:9100/log-interaction")
    response = httpx.Response(500, request=request)
    raise httpx.HTTPStatusError("Server error '500'", request=request, response=response)


async def _ok_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {"ok": True}


@pytest.mark.asyncio
async def test_handler_exception_becomes_tool_crashed() -> None:
    with patch.object(dispatch, "_get_handlers", return_value={"boom": _boom_handler}):
        result = await dispatch._execute_tool("boom", {}, user_role="service")
    assert result.get("tool_crashed") is True
    assert "RuntimeError" in result.get("error", "")
    assert "orchestrator down" in result.get("error", "")


@pytest.mark.asyncio
async def test_httpstatus_error_does_not_propagate() -> None:
    with patch.object(
        dispatch, "_get_handlers", return_value={"log_interaction": _http_boom_handler}
    ):
        # Must not raise — the whole point of the guard
        result = await dispatch._execute_tool("log_interaction", {}, user_role="service")
    # Transport/HTTP failures from backing services are mapped to a short
    # structured error, not a tool crash, and never echo internal URLs.
    assert result.get("error") == "backing service error (HTTP 500)"
    assert result.get("retryable") is True
    assert result.get("tool_crashed") is None
    assert "127.0.0.1" not in str(result)


async def _connect_error_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    import httpx

    raise httpx.ConnectError("All connection attempts failed")


async def _read_timeout_handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    # The store_memory path when Ollama embeddings are down: the ReadTimeout
    # escapes robothor.memory.facts.store_fact and reaches dispatch.
    import httpx

    raise httpx.ReadTimeout("embedding fetch timed out")


@pytest.mark.asyncio
async def test_transport_error_maps_to_short_unreachable_error() -> None:
    with patch.object(dispatch, "_get_handlers", return_value={"look": _connect_error_handler}):
        result = await dispatch._execute_tool("look", {}, user_role="service")
    assert result.get("error") == "backing service unreachable: ConnectError"
    assert result.get("retryable") is True
    assert result.get("tool_crashed") is None


@pytest.mark.asyncio
async def test_embedding_timeout_maps_to_short_structured_error() -> None:
    with patch.object(
        dispatch, "_get_handlers", return_value={"store_memory": _read_timeout_handler}
    ):
        result = await dispatch._execute_tool("store_memory", {}, user_role="service")
    assert result.get("error") == "backing service unreachable: ReadTimeout"
    assert result.get("retryable") is True
    assert result.get("tool_crashed") is None
    assert "traceback" not in str(result).lower()


@pytest.mark.asyncio
async def test_success_path_unaffected() -> None:
    with patch.object(dispatch, "_get_handlers", return_value={"ok": _ok_handler}):
        result = await dispatch._execute_tool("ok", {}, user_role="service")
    assert result == {"ok": True}
    assert "tool_crashed" not in result


@pytest.mark.asyncio
async def test_handler_returning_error_dict_not_tagged_as_crash() -> None:
    # A handler that returns {"error": ...} without raising is a clean error,
    # not a crash — tool_crashed flag should NOT be set.
    async def _controlled(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"error": "bad args"}

    with patch.object(dispatch, "_get_handlers", return_value={"controlled": _controlled}):
        result = await dispatch._execute_tool("controlled", {}, user_role="service")
    assert result.get("error") == "bad args"
    assert "tool_crashed" not in result
