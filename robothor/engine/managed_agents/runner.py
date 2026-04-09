"""Standalone execution function for Claude Managed Agents.

``run_on_managed_agents()`` is the primary entry point.  It creates an MA
session, sends the user message, consumes the SSE event stream (executing
custom tools locally via the existing tool dispatch), and returns an
``MARunResult``.

This module does NOT import from or modify ``robothor.engine.runner``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from robothor.engine.managed_agents.client import (
    MAClientError,
    MAUnavailableError,
    get_ma_client,
)
from robothor.engine.managed_agents.models import MARunResult, MASessionConfig
from robothor.engine.managed_agents.outcomes import build_outcome_event

logger = logging.getLogger(__name__)


async def run_on_managed_agents(
    agent_id: str,
    message: str,
    *,
    system_prompt: str = "",
    model: str = "claude-sonnet-4-6",
    tools: list[dict[str, Any]] | None = None,
    tool_names: list[str] | None = None,
    enable_builtin_sandbox: bool = False,
    memory_store_ids: list[str] | None = None,
    environment_id: str | None = None,
    outcome_rubric: str | None = None,
    outcome_max_iterations: int = 5,
    tenant_id: str = "robothor-primary",
    on_content: Callable[[str], Awaitable[None]] | None = None,
    on_tool: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    persist: bool = True,
) -> MARunResult:
    """Run an agent on Claude Managed Agents infrastructure.

    This is a **standalone function** — it does not use ``AgentRunner``,
    modify any existing engine state, or touch any existing tables.

    Custom tool calls emitted by the MA agent are executed locally via
    ``robothor.engine.tools.dispatch._execute_tool`` (read-only access
    to the tool registry, same ``ToolContext`` pattern).

    Parameters
    ----------
    agent_id
        The Robothor agent ID (used to load the system prompt and filter
        tools from the registry when *tools* is not provided).
    message
        The user message to send to the agent.
    system_prompt
        Override system prompt.  If empty, built from the agent's
        instruction file via ``build_system_prompt()``.
    model
        Claude model ID (e.g. ``"claude-sonnet-4-6"``).
    tools
        Pre-built MA tool list.  When *None*, tools are derived from
        the agent's manifest via ``build_ma_tools_for_agent()``.
    tool_names
        Explicit tool name list (alternative to *tools*).  Converted
        via ``build_ma_tools_from_names()``.
    enable_builtin_sandbox
        When *True*, MA built-in tools (bash, read, write, etc.) run
        inside the cloud container instead of locally.
    memory_store_ids
        MA memory store IDs to attach as session resources.
    environment_id
        MA environment ID.  Created automatically via ``TenantMapper``
        if not provided.
    outcome_rubric
        Markdown rubric for outcome-based evaluation.  When provided,
        the agent iterates until the rubric is satisfied.
    outcome_max_iterations
        Max evaluation cycles for outcome mode (1–20).
    tenant_id
        Robothor tenant ID — scopes custom tool execution and
        MA resource creation.
    on_content
        Async callback invoked with text chunks as they stream.
    on_tool
        Async callback invoked with tool status updates.
    persist
        Whether to persist the run to the ``ma_runs`` table.

    Returns
    -------
    MARunResult
        Session output, token counts, tool calls, outcome result.
    """
    client = get_ma_client()

    # ── 1. Build tool list ────────────────────────────────────────────
    if tools is None:
        tools = _build_tools(agent_id, tool_names, enable_builtin_sandbox)

    # ── 2. Resolve system prompt ──────────────────────────────────────
    if not system_prompt:
        system_prompt = _load_system_prompt(agent_id)

    # ── 3. Resolve MA resources via tenant mapper ─────────────────────
    from robothor.engine.managed_agents.tenant_mapper import get_tenant_mapper

    mapper = get_tenant_mapper()

    ma_agent = await mapper.get_or_create_agent(tenant_id, agent_id, model, system_prompt, tools)

    if not environment_id:
        environment_id = await mapper.get_or_create_environment(tenant_id, "default")

    # ── 4. Build session resources ────────────────────────────────────
    resources: list[dict[str, Any]] = [
        {"type": "memory_store", "memory_store_id": sid, "access": "read_write"}
        for sid in (memory_store_ids or [])
    ]

    # ── 5. Create session ─────────────────────────────────────────────
    session_resp = await client.create_session(
        MASessionConfig(
            agent_id=ma_agent["id"],
            agent_version=ma_agent.get("version", 1),
            environment_id=environment_id,
            resources=resources,
            title=f"{tenant_id}/{agent_id}",
        )
    )
    session_id = session_resp["id"]

    # ── 6. Send initial events ────────────────────────────────────────
    events: list[dict[str, Any]] = [
        {
            "type": "user.message",
            "content": [{"type": "text", "text": message}],
        }
    ]
    if outcome_rubric:
        events.append(
            build_outcome_event(message, outcome_rubric, max_iterations=outcome_max_iterations)
        )
    await client.send_events(session_id, events)

    # ── 7. Consume SSE stream ─────────────────────────────────────────
    result = MARunResult(session_id=session_id)
    accumulated: list[str] = []
    t_start = time.monotonic()

    workspace = str(Path.home() / "robothor")

    try:
        async for event in client.stream_session(session_id):
            result.events.append(event)
            event_type = event.get("type", "")

            if event_type == "agent.message":
                for block in event.get("content", []):
                    text = block.get("text", "")
                    if text:
                        accumulated.append(text)
                        if on_content:
                            await on_content(text)

            elif event_type == "agent.custom_tool_use":
                await _handle_custom_tool(
                    client,
                    session_id,
                    event,
                    result,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    workspace=workspace,
                    on_tool=on_tool,
                )

            elif event_type == "agent.tool_use":
                # Built-in tool executed inside the MA container
                result.tool_calls.append(
                    {
                        "name": event.get("name", ""),
                        "builtin": True,
                    }
                )
                if on_tool:
                    await on_tool(
                        {"name": event.get("name", ""), "status": "running", "builtin": True}
                    )

            elif event_type == "agent.tool_result":
                if on_tool:
                    await on_tool({"name": "", "status": "done", "builtin": True})

            elif event_type == "span.outcome_evaluation_end":
                result.outcome_result = event.get("result")
                result.outcome_explanation = event.get("explanation")

            elif event_type in ("session.status_idle", "session.status_terminated"):
                break

    except MAUnavailableError:
        logger.warning("MA stream disconnected for session %s", session_id)
        result.error = "Stream disconnected"
    except MAClientError as exc:
        logger.error("MA client error during stream: %s", exc)
        result.error = str(exc)

    result.output_text = "".join(accumulated)
    result.duration_ms = int((time.monotonic() - t_start) * 1000)

    # Extract usage from session metadata if available
    _extract_usage(session_resp, result)

    # ── 8. Persist ────────────────────────────────────────────────────
    if persist:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _persist,
            result,
            agent_id,
            tenant_id,
            message,
        )

    logger.info(
        "MA run complete: agent=%s session=%s duration=%dms tokens=%d+%d outcome=%s",
        agent_id,
        session_id,
        result.duration_ms,
        result.input_tokens,
        result.output_tokens,
        result.outcome_result or "n/a",
    )
    return result


# ── Internal helpers ──────────────────────────────────────────────────


async def _handle_custom_tool(
    client: Any,
    session_id: str,
    event: dict[str, Any],
    result: MARunResult,
    *,
    agent_id: str,
    tenant_id: str,
    workspace: str,
    on_tool: Callable[[dict[str, Any]], Awaitable[None]] | None,
) -> None:
    """Execute a custom tool locally and send the result back to MA."""
    tool_name = event.get("name", "")
    tool_input = event.get("input", {})
    tool_use_id = event.get("id", "")

    if on_tool:
        await on_tool({"name": tool_name, "status": "running"})

    from robothor.engine.tools.dispatch import _execute_tool

    try:
        tool_result = await _execute_tool(
            tool_name,
            tool_input,
            agent_id=agent_id,
            tenant_id=tenant_id,
            workspace=workspace,
        )
        is_error = False
    except Exception as exc:
        tool_result = {"error": str(exc)}
        is_error = True
        logger.warning("Custom tool %s failed: %s", tool_name, exc)

    # Serialize result for MA
    if isinstance(tool_result, str):
        result_text = tool_result
    else:
        try:
            result_text = json.dumps(tool_result, default=str)
        except (TypeError, ValueError):
            result_text = str(tool_result)

    # Send result back to MA session
    await client.send_events(
        session_id,
        [
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": tool_use_id,
                "is_error": is_error,
                "content": [{"type": "text", "text": result_text}],
            }
        ],
    )

    result.tool_calls.append(
        {
            "name": tool_name,
            "input": tool_input,
            "output": tool_result,
            "is_error": is_error,
        }
    )

    if on_tool:
        await on_tool({"name": tool_name, "status": "done"})


def _build_tools(
    agent_id: str,
    tool_names: list[str] | None,
    enable_builtin_sandbox: bool,
) -> list[dict[str, Any]]:
    """Build MA tools from the engine registry."""
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()

    if tool_names:
        from robothor.engine.managed_agents.tool_bridge import build_ma_tools_from_names

        return build_ma_tools_from_names(
            registry, tool_names, enable_builtin_sandbox=enable_builtin_sandbox
        )

    # Load agent config to get tool filtering
    try:
        from robothor.engine.config import load_agent_config

        agent_config = load_agent_config(agent_id)
    except Exception:
        logger.warning("Could not load agent config for %s, using empty tools", agent_id)
        tools: list[dict[str, Any]] = []
        if enable_builtin_sandbox:
            tools.append({"type": "agent_toolset_20260401"})
        return tools

    from robothor.engine.managed_agents.tool_bridge import build_ma_tools_for_agent

    return build_ma_tools_for_agent(
        registry, agent_config, enable_builtin_sandbox=enable_builtin_sandbox
    )


def _load_system_prompt(agent_id: str) -> str:
    """Load system prompt from agent manifest."""
    try:
        from robothor.engine.config import build_system_prompt, load_agent_config

        workspace = Path.home() / "robothor"
        config = load_agent_config(agent_id)
        parts = build_system_prompt(config, workspace)
        return parts.full_text()
    except Exception:
        logger.warning("Could not load system prompt for %s", agent_id)
        return ""


def _extract_usage(session_resp: dict[str, Any], result: MARunResult) -> None:
    """Extract token usage from session response if available."""
    usage = session_resp.get("usage", {})
    if usage:
        result.input_tokens = usage.get("input_tokens", 0)
        result.output_tokens = usage.get("output_tokens", 0)
        result.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
        result.cache_read_tokens = usage.get("cache_read_input_tokens", 0)


def _persist(
    result: MARunResult,
    agent_id: str,
    tenant_id: str,
    input_message: str,
) -> None:
    """Synchronous persistence wrapper (run in executor)."""
    from robothor.engine.managed_agents.persistence import persist_ma_run

    persist_ma_run(result, agent_id, tenant_id, input_message=input_message)
