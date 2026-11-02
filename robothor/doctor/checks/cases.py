"""Cases past due that nobody owns, or that wait on a person.

A case is a CRM task (``docs/CRM_DATA_FABRIC.md``, "Cases"). An overdue task
an agent owns is that agent's to finish, and its own heartbeat sees it. What
nobody sees is an overdue task with no assigned agent, or one flagged
``requires_human`` whose question nobody has answered (answering clears the
flag). This check counts both kinds and names the oldest of each, so the
operator has a place to start. Not repairable by ``--fix``: someone has to
pick the case up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, fail, ok, skip

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext

_QUERY = """
SELECT requires_human IS TRUE AS needs_person,
       count(*),
       (array_agg(id::text ORDER BY due_at ASC))[1]
  FROM crm_tasks
 WHERE deleted_at IS NULL
   AND status IS DISTINCT FROM 'DONE'
   AND due_at < NOW()
   AND (assigned_to_agent IS NULL OR requires_human IS TRUE)
 GROUP BY 1
"""


def _rows(ctx: DoctorContext) -> list[tuple[Any, ...]]:
    with ctx.db() as conn:
        cursor = conn.cursor()
        cursor.execute(_QUERY)
        return list(cursor.fetchall())


async def _cases_overdue(ctx: DoctorContext) -> Result:
    """Open tasks past ``due_at`` with no assigned agent, or waiting on a person."""
    try:
        rows = await ctx.run_blocking(lambda: _rows(ctx))
    except Exception as error:  # noqa: BLE001 - a missing table or database is a skip
        return skip(f"could not read crm_tasks ({type(error).__name__})")
    if not rows:
        return ok("no overdue cases without an owner")
    parts: list[str] = []
    total = 0
    for needs_person, count, oldest in sorted(rows, key=lambda row: bool(row[0])):
        total += int(count)
        kind = "needs a person" if needs_person else "unowned"
        parts.append(f"{int(count)} {kind} (oldest {oldest})")
    noun = "case" if total == 1 else "cases"
    return fail(
        f"{total} overdue {noun}: {', '.join(parts)} — assign an owner, or answer the question"
    )


CHECKS: tuple[Check, ...] = (
    Check(
        id="crm.cases_overdue",
        title="No overdue case is left without an owner",
        category="crm",
        severity="recommended",
        run=_cases_overdue,
    ),
)
