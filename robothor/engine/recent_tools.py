"""The tools an agent actually used lately: the per-agent half of its core.

Under deferred tool loading (Rip 16) a broad agent is shown ``CORE_TOOLS`` and
the three meta-tools; everything else costs a ``tool_search`` →
``tool_call`` round-trip. ``CORE_TOOLS`` is one list for every agent, so a tool
one agent calls on every run — Gmail for an inbox agent, the CRM verbs for a
steward — paid those extra round-trips every time. This module reads which
tools the agent called in its latest runs (``agent_run_steps``, with
``tool_call`` unwrapped to the tool it ran) so the registry can advertise them
directly.

Best-effort and bounded, because it sits on the path of every run: the query
runs in a worker thread (never on the event loop), the wait is capped at
:data:`_LOOKUP_TIMEOUT_S`, the answer is cached per agent and tenant, and any
failure is the empty set — which is the static core, i.e. what deferral did
before this existed.
"""

from __future__ import annotations

import asyncio
import logging
import time

from robothor.db.connection import get_connection
from robothor.engine.tools.constants import TOOLSEARCH_TOOLS

logger = logging.getLogger(__name__)

__all__ = ["RECENT_TOOLS_MAX", "clear_cache", "fetch_recent_tool_names", "recent_tool_names"]

#: At most this many recent tools join an agent's core, most-used first. The
#: core is meant to stay small; an agent that touched forty tools in ten runs
#: is not saved forty round-trips by advertising all forty on every call.
RECENT_TOOLS_MAX = 16

#: How long one lookup's answer is reused. An agent's habits change over days,
#: not minutes, and a run must not pay a query for a hint.
_CACHE_TTL_S = 600.0

#: A failed or timed-out lookup is remembered for this long, so a database
#: that is down costs one bounded wait a minute rather than one per run.
_FAILURE_TTL_S = 60.0

#: How long a run waits for the answer before it proceeds with the static core.
_LOOKUP_TIMEOUT_S = 1.0

#: Server-side bound on the same query, for a connection that answers the
#: handshake and then stalls (lock contention, a saturated pool).
_STATEMENT_TIMEOUT_MS = 500

_cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}

_SQL = f"""
SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS};
SELECT used.name, COUNT(*) AS uses
FROM (
    SELECT CASE WHEN s.tool_name = 'tool_call' THEN s.tool_input->>'name'
                ELSE s.tool_name END AS name
    FROM agent_run_steps s
    JOIN (
        SELECT id FROM agent_runs
        WHERE agent_id = %s AND tenant_id = %s
        ORDER BY created_at DESC
        LIMIT %s
    ) r ON r.id = s.run_id
    WHERE s.step_type = 'tool_call' AND s.tool_name IS NOT NULL
) used
WHERE used.name IS NOT NULL
GROUP BY used.name
ORDER BY uses DESC, used.name
LIMIT %s
"""


def fetch_recent_tool_names(agent_id: str, tenant_id: str, runs: int) -> list[str]:
    """Tool names the agent called in its last ``runs`` runs, most-used first.

    Blocking (one DB round-trip): call it from a worker thread. The meta-tools
    are dropped — they are advertised to every deferred agent anyway.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(_SQL, (agent_id, tenant_id, runs, RECENT_TOOLS_MAX + len(TOOLSEARCH_TOOLS)))
        rows = cur.fetchall()
    names = [str(row[0]) for row in rows if row and row[0]]
    return [n for n in names if n not in TOOLSEARCH_TOOLS][:RECENT_TOOLS_MAX]


async def recent_tool_names(agent_id: str, tenant_id: str) -> frozenset[str]:
    """The cached, bounded, off-loop answer. Never raises; ``frozenset()`` on failure."""
    from robothor.engine.feature_flags import deferred_tools_recent_runs

    runs = deferred_tools_recent_runs()
    if runs <= 0 or not agent_id:
        return frozenset()
    key = (agent_id, tenant_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        names = await asyncio.wait_for(
            asyncio.to_thread(fetch_recent_tool_names, agent_id, tenant_id, runs),
            timeout=_LOOKUP_TIMEOUT_S,
        )
    except TimeoutError:
        logger.info(
            "Recent-tools lookup for %s timed out after %.1fs; using the static core",
            agent_id,
            _LOOKUP_TIMEOUT_S,
        )
        _cache[key] = (now + _FAILURE_TTL_S, frozenset())
        return frozenset()
    except Exception as e:  # noqa: BLE001 - a prompt-size hint must not fail a run
        logger.warning("Recent-tools lookup for %s skipped: %s", agent_id, type(e).__name__)
        _cache[key] = (now + _FAILURE_TTL_S, frozenset())
        return frozenset()
    found = frozenset(names)
    _cache[key] = (now + _CACHE_TTL_S, found)
    return found


def clear_cache() -> None:
    """Forget every cached answer (test isolation, and after a bulk import)."""
    _cache.clear()
