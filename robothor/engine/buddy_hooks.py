"""Buddy lifecycle hooks — feed the task counter from real agent events.

Registers a single AGENT_END Python handler that increments the daily
tasks_completed counter on buddy_stats / agent_buddy_stats. Achievement
scoring happens in BuddyEngine.refresh_daily() via goals.py, not here.

Usage (in daemon.py hook registry setup):
    from robothor.engine.buddy_hooks import register_buddy_hooks
    register_buddy_hooks(hook_registry)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.hook_registry import HookRegistry

logger = logging.getLogger(__name__)


def _on_agent_end(context: Any) -> dict[str, Any]:
    """AGENT_END hook handler — increment buddy task counter (global + per-agent).

    Args:
        context: HookContext dataclass from the hook registry.

    Returns:
        Hook result dict (always ALLOW — this is observational, never blocks).
    """
    # HookContext is a dataclass — access status from metadata dict
    metadata = getattr(context, "metadata", {}) or {}
    status = metadata.get("status", "")
    if status != "completed":
        return {"action": "allow"}

    agent_id = metadata.get("agent_id") or getattr(context, "agent_id", None)

    try:
        from robothor.engine.buddy import BuddyEngine

        BuddyEngine().increment_task_count(agent_id=agent_id)
    except Exception as e:
        logger.debug("Buddy hook: failed to increment task count: %s", e)

    return {"action": "allow"}


def register_buddy_hooks(registry: HookRegistry) -> None:
    """Register buddy lifecycle hooks with the hook registry."""
    from robothor.engine.hook_registry import HookEvent, LifecycleHook

    hook = LifecycleHook(
        event=HookEvent.AGENT_END,
        handler_type="python",
        handler="robothor.engine.buddy_hooks._on_agent_end",
        blocking=False,
        priority=900,  # low priority — runs after important hooks
        scope="global",
    )
    registry.register(hook)
    logger.info("Buddy lifecycle hooks registered")
