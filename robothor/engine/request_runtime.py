"""Loop setup and the lifetime of per-request observation state."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from robothor.engine import session_registry
from robothor.engine.context_control import ContextControl, control
from robothor.engine.performance import periodic_progress, record_compactions

logger = logging.getLogger(__name__)


@asynccontextmanager
async def observe_request(session: Any, on_status: Any):
    session_registry.register(session, on_status=on_status)
    state = ContextControl(active_request=getattr(session, "originating_message", ""))
    token = control.set(state)
    task = asyncio.create_task(periodic_progress(session, on_status))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        try:
            record_compactions(session, state.measurements)
        finally:
            control.reset(token)
            session_registry.unregister(session)


def prepare_loop(
    runner: Any, session: Any, agent_config: Any, route: Any, resumed_scratchpad: Any
) -> tuple[Any, Any, Any, Any]:
    # ── v2: Initialize enhancement objects ──
    scratchpad = runner._create_scratchpad(agent_config, route, resumed_scratchpad)
    escalation = runner._create_escalation(agent_config)
    checkpoint = runner._create_checkpoint(agent_config, route, session.run_id)
    guardrail_engine = runner._create_guardrails(agent_config)

    # ── v2: Initialize in-conversation todo list ──
    if agent_config.todo_list_enabled:
        from robothor.engine.todolist import TodoList

        session.todo_list = TodoList(items=[])

    # Inject guardrail awareness into system prompt so LLM self-regulates
    if guardrail_engine and guardrail_engine.enabled_policies:
        from robothor.engine.guardrails import guardrail_summary

        gr_text = guardrail_summary(guardrail_engine.enabled_policies)
        if gr_text and session.messages and session.messages[0].get("role") == "system":
            session.messages[0]["content"] += f"\n\n---\n\n{gr_text}"

    return scratchpad, escalation, checkpoint, guardrail_engine


async def start_hooks(agent_config: Any, session: Any) -> Any:
    # ── v2: Lifecycle hooks ──
    from robothor.engine.hook_registry import (
        HookContext,
        HookEvent,
        get_hook_registry,
    )

    hook_registry = get_hook_registry()

    # Dispatch AGENT_START hook
    if hook_registry:
        try:
            start_ctx = HookContext(
                event=HookEvent.AGENT_START,
                agent_id=agent_config.id,
                run_id=session.run.id,
            )
            await hook_registry.dispatch(HookEvent.AGENT_START, start_ctx)
        except Exception as e:
            logger.warning("AGENT_START hook error: %s", type(e).__name__)

    return hook_registry
