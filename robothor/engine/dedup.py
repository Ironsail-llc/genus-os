"""
Cross-trigger dedup — prevents concurrent runs of the same agent.

Uses a module-level set guarded by an asyncio.Lock for safety.
Shared between scheduler and hooks so both respect the same lock.

The lock is defensive: pure asyncio is single-threaded, but if the codebase
ever uses run_in_executor or threading for dedup checks, the lock prevents
race conditions.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_running: set[str] = set()
_lock = asyncio.Lock()


async def try_acquire(agent_id: str) -> bool:
    """Attempt to acquire the agent lock. Returns True if acquired."""
    async with _lock:
        if agent_id in _running:
            logger.debug("Dedup: %s already running, skipping", agent_id)
            return False
        _running.add(agent_id)
        return True


async def release(agent_id: str) -> None:
    """Release the agent lock."""
    async with _lock:
        _running.discard(agent_id)


def release_sync(agent_id: str) -> None:
    """Release the agent lock (sync version for non-async contexts like run_in_executor)."""
    _running.discard(agent_id)


def is_running(agent_id: str) -> bool:
    """Check if an agent is currently running."""
    return agent_id in _running


def running_agents() -> set[str]:
    """Return a copy of the currently running agent IDs."""
    return _running.copy()


def clear() -> None:
    """Clear all locks. Only for testing."""
    _running.clear()
