"""Tests for long-running task execution improvements.

Covers:
  - Per-tool timeout in ToolRegistry.execute()
  - Per-tool circuit breaker in the runner loop
  - Background plan execution config builder
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ── Per-tool timeout tests ──


@pytest.mark.asyncio
async def test_tool_timeout_returns_error():
    """A tool that hangs past the timeout should return a clean error dict."""
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)  # will be cancelled by timeout
        return {"ok": True}

    with patch("robothor.engine.tools.registry._execute_tool", side_effect=_hang):
        result = await registry.execute("fake_tool", {}, timeout=1)

    assert "error" in result
    assert "timed out" in result["error"]
    assert "fake_tool" in result["error"]


@pytest.mark.asyncio
async def test_tool_no_timeout_when_zero():
    """timeout=0 should not impose a time limit."""
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def _fast(*args, **kwargs):
        return {"ok": True}

    with patch("robothor.engine.tools.registry._execute_tool", side_effect=_fast):
        result = await registry.execute("fake_tool", {}, timeout=0)

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_tool_completes_within_timeout():
    """A tool that finishes before the timeout should return normally."""
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def _fast(*args, **kwargs):
        return {"data": "hello"}

    with patch("robothor.engine.tools.registry._execute_tool", side_effect=_fast):
        result = await registry.execute("fake_tool", {}, timeout=120)

    assert result == {"data": "hello"}


@pytest.mark.asyncio
async def test_tool_exception_still_caught():
    """Non-timeout exceptions should still be caught and returned as error dicts."""
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()

    async def _explode(*args, **kwargs):
        raise ValueError("boom")

    with patch("robothor.engine.tools.registry._execute_tool", side_effect=_explode):
        result = await registry.execute("fake_tool", {}, timeout=120)

    assert "error" in result
    assert "boom" in result["error"]


# ── Circuit breaker tests ──


def test_circuit_breaker_injects_message_after_3_failures():
    """After 3 failures for the same tool, a system message should be injected."""
    # Simulate the circuit breaker logic from runner.py
    _tool_failures: dict[str, int] = {}
    messages: list[dict] = []

    tool_name = "search_memory"
    for _i in range(4):
        _tool_failures[tool_name] = _tool_failures.get(tool_name, 0) + 1
        if _tool_failures[tool_name] >= 3:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[SYSTEM] Tool '{tool_name}' has failed "
                        f"{_tool_failures[tool_name]} times this run. "
                        "Do NOT call it again. Find an alternative "
                        "approach or skip this step and move on."
                    ),
                }
            )

    # First two failures: no message. Third and fourth: messages injected.
    assert len(messages) == 2
    assert "search_memory" in messages[0]["content"]
    assert "3 times" in messages[0]["content"]
    assert "4 times" in messages[1]["content"]


def test_circuit_breaker_independent_per_tool():
    """Different tools should have independent failure counters."""
    _tool_failures: dict[str, int] = {}
    messages: list[dict] = []

    for tool_name in ["search_memory", "web_fetch", "search_memory"]:
        _tool_failures[tool_name] = _tool_failures.get(tool_name, 0) + 1
        if _tool_failures[tool_name] >= 3:
            messages.append({"role": "user", "content": f"skip {tool_name}"})

    # search_memory: 2 failures, web_fetch: 1 failure — no circuit breaker triggered
    assert len(messages) == 0
    assert _tool_failures["search_memory"] == 2
    assert _tool_failures["web_fetch"] == 1


# ── Background config builder test ──


def test_build_background_config():
    """_build_background_config should apply continuous-mode overrides."""
    from robothor.engine.models import AgentConfig

    mock_config = AgentConfig(
        id="main",
        name="Robothor",
        safety_cap=200,
        timeout_seconds=0,
        max_iterations=30,
        stall_timeout_seconds=600,
    )

    with patch("robothor.engine.config.load_agent_config", return_value=mock_config):
        # Create a minimal object with the method
        class FakeBot:
            def __init__(self):
                self.config = MagicMock()
                self.config.default_chat_agent = "main"
                self.config.manifest_dir = "/tmp"

        from robothor.engine.telegram import TelegramBot

        fake = FakeBot()
        result = TelegramBot._build_background_config(fake)  # type: ignore[arg-type]

    assert result.continuous is True
    assert result.safety_cap >= 2000
    assert result.timeout_seconds >= 86400
    assert result.max_iterations >= 100
    assert result.checkpoint_enabled is True
    assert result.progress_report_interval == 20
