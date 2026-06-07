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
import os

logger = logging.getLogger(__name__)

_running: set[str] = set()
_lock = asyncio.Lock()

# HA mode: back dedup with a Redis lease so concurrent runs of the same agent are
# prevented ACROSS engine replicas, not just in one process. Gated by
# ROBOTHOR_HA_DEDUP_ENABLED; default off keeps the in-process behavior. Falls
# back to in-process automatically if Redis is unavailable.
_HA_TTL_MS = 7_200_000  # 2h — covers the longest run; a crashed holder's lease expires
_owners: dict[str, str] = {}  # agent_id → lease owner token (HA mode)


def _ha_enabled() -> bool:
    return os.environ.get("ROBOTHOR_HA_DEDUP_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _dedup_key(agent_id: str) -> str:
    return f"robothor:dedup:{agent_id}"


async def try_acquire(agent_id: str) -> bool:
    """Attempt to acquire the agent lock. Returns True if acquired."""
    if _ha_enabled():
        try:
            from robothor.engine import redis_lease

            token = await asyncio.to_thread(redis_lease.acquire, _dedup_key(agent_id), _HA_TTL_MS)
            if token is None:
                logger.debug("Dedup(HA): %s already running on another node", agent_id)
                return False
            _owners[agent_id] = token
            async with _lock:
                _running.add(agent_id)
            return True
        except Exception as e:
            logger.warning("HA dedup acquire failed; falling back to in-process: %s", e)

    async with _lock:
        if agent_id in _running:
            logger.debug("Dedup: %s already running, skipping", agent_id)
            return False
        _running.add(agent_id)
        return True


async def release(agent_id: str) -> None:
    """Release the agent lock."""
    token = _owners.pop(agent_id, None)
    if token is not None:
        try:
            from robothor.engine import redis_lease

            await asyncio.to_thread(redis_lease.release, _dedup_key(agent_id), token)
        except Exception as e:
            logger.debug("HA dedup release failed: %s", e)
    async with _lock:
        _running.discard(agent_id)


def release_sync(agent_id: str) -> None:
    """Release the agent lock (sync version for non-async contexts like run_in_executor)."""
    token = _owners.pop(agent_id, None)
    if token is not None:
        try:
            from robothor.engine import redis_lease

            redis_lease.release(_dedup_key(agent_id), token)
        except Exception as e:
            logger.debug("HA dedup release_sync failed: %s", e)
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
