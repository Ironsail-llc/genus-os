"""Goal pursuit is optional. The core task inbox is not.

``pursuit_task_runnable`` ships in ``crm/migrations/126_goal_pursuit.sql``.
``robothor.crm.dal.list_agent_tasks`` and the thread-claim query in
``robothor.engine.thread_pool`` — the task inbox every agent reads — called it
unconditionally, so at tail 125, or after a rollback of 126, both were BROKEN
rather than degraded: the statement fails and every agent loses its task list.
``robothor.engine.daemon`` deliberately boots with pending migrations (an
instance that will not start because of an additive object is the worse
failure), so "126 is not applied here" is a reachable state, not a
hypothetical.

The guard cannot live in SQL. ``to_regprocedure('pursuit_task_runnable(uuid,
text)') IS NULL OR pursuit_task_runnable(...)`` reads like a short circuit but
is not one: PostgreSQL resolves every function name at PARSE time, so the
statement still fails before a single row is considered.

So the probe is here, in Python, on the connection the caller already holds —
no second connection, and no round trip at all after the first call, because
the answer is cached for the life of the process. A database cannot grow or
drop the predicate without a migration, and a process that applies one in-band
calls ``reset_probe()``.
"""

from __future__ import annotations

import time
from typing import Any

_installed: bool | None = None

_PROBE = "SELECT to_regprocedure('pursuit_task_runnable(uuid,text)') IS NOT NULL AS present"


def pursuit_installed(cur: Any) -> bool:
    """Whether migration 126's task predicate exists, probed once per process.

    ``cur`` is the caller's open cursor: the probe is a plain ``SELECT`` on a
    text literal, so it can neither fail nor disturb the caller's transaction.
    """
    global _installed
    if _installed is None:
        cur.execute(_PROBE)
        row = cur.fetchone()
        # The caller's cursor may or may not use a dict factory.
        _installed = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
    return _installed


def task_gate(cur: Any, task: str, tenant: str) -> str:
    """SQL predicate excluding tasks owned by an inactive goal.

    Falls back to the pre-feature behaviour — every task is runnable — when
    migration 126 is absent, so the core inbox degrades instead of breaking.
    """
    return f"pursuit_task_runnable({task},{tenant})" if pursuit_installed(cur) else "TRUE"


_LINKED_TTL_SECONDS = 30
_linked: dict[str, tuple[float, bool]] = {}


def tenant_links_tasks(tenant: str) -> bool:
    """Whether this tenant has any goal-linked task at all.

    ``admit_tool`` ran ``pursuit_task_runnable`` — a database round trip — for
    every tool call of every agent whose run carries a task id, on every
    instance, whether or not a single goal existed. A task can only be gated
    if something linked it, so a tenant with no links can skip the check
    entirely.

    Cached for thirty seconds, and invalidated in-process the moment this
    process links a task. The only staleness that survives that is another
    process creating a tenant's FIRST link, which delays the gate by up to the
    TTL; every link after it is covered, because the answer has already
    flipped to True and stays there.
    """
    cached = _linked.get(tenant)
    now = time.monotonic()
    if cached and now - cached[0] < _LINKED_TTL_SECONDS:
        return cached[1]
    try:
        from robothor.goals.store import transaction

        with transaction() as cur:
            if not pursuit_installed(cur):
                return False
            cur.execute("SELECT 1 FROM pursuit_goal_tasks WHERE tenant_id=%s LIMIT 1", (tenant,))
            answer = cur.fetchone() is not None
    except Exception:  # noqa: BLE001 - an unanswered question is not a licence to skip
        return True
    _linked[tenant] = (now, answer)
    return answer


def note_task_linked(tenant: str) -> None:
    """A link just happened in this process: stop skipping the gate now."""
    _linked[tenant] = (time.monotonic(), True)


def reset_probe() -> None:
    """Forget the cached answers. For tests, and for a process that has just
    applied migrations in-band."""
    global _installed
    _installed = None
    _linked.clear()
