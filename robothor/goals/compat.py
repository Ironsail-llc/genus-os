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


def reset_probe() -> None:
    """Forget the cached answer. For tests, and for a process that has just
    applied migrations in-band."""
    global _installed
    _installed = None
