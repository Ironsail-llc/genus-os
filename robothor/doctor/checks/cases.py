"""Cases past due that nobody owns, or that wait on a person.

A case is a CRM task (``docs/CRM_DATA_FABRIC.md``, "Cases"). An overdue task
an agent owns is that agent's to finish, and its own heartbeat sees it. What
nobody sees is an overdue task with no assigned agent, or one flagged
``requires_human`` whose question nobody has answered (answering clears the
flag). This check counts both kinds and names the oldest of each, with its
tenant, so the operator has a place to start. It counts every tenant the
connection can see: all of them when RLS is off or the role bypasses it, one
when ``app.tenant_id`` is bound, and the detail says which. A task snoozed until a future
``follow_up_at`` is not overdue, the same rule ``list_tasks`` applies. Not repairable by ``--fix``: someone has to
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
       (array_agg(id::text ORDER BY due_at ASC))[1],
       (array_agg(tenant_id ORDER BY due_at ASC))[1]
  FROM crm_tasks
 WHERE deleted_at IS NULL
   AND status IS DISTINCT FROM 'DONE'
   AND due_at < NOW()
   AND (assigned_to_agent IS NULL OR requires_human IS TRUE)
   AND (follow_up_at IS NULL OR follow_up_at <= NOW())
 GROUP BY 1
"""


def _rows(ctx: DoctorContext) -> tuple[str, list[tuple[Any, ...]]]:
    """The RLS scope and the counts, read in one transaction.

    Migration 081 FORCEs row-level security on ``crm_tasks``; with RLS on, every
    checkout binds ``app.tenant_id`` and the GROUP BY sees that one tenant. The
    scope is read first, on the same connection, so the answer can say which
    tenants it covers (the pattern ``identity.owner_account`` uses). ``true`` is
    missing_ok: the setting is absent when RLS is off.
    """
    with ctx.db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        scope_row = cursor.fetchone()
        cursor.execute(_QUERY)
        rows = list(cursor.fetchall())
    scope = str(scope_row[0]) if scope_row and scope_row[0] else ""
    return scope, rows


async def _cases_overdue(ctx: DoctorContext) -> Result:
    """Open tasks past ``due_at`` with no assigned agent, or waiting on a person."""
    try:
        scope, rows = await ctx.run_blocking(lambda: _rows(ctx))
    except Exception as error:  # noqa: BLE001 - a missing table or database is a skip
        return skip(f"could not read crm_tasks ({type(error).__name__})")
    # A scoped read cannot see another tenant's rows: say what it covered.
    where = f"in RLS scope {scope}" if scope else "in any tenant"
    if not rows:
        return ok(f"no overdue cases without an owner {where}")
    parts: list[str] = []
    total = 0
    for needs_person, count, oldest, tenant in sorted(rows, key=lambda row: bool(row[0])):
        total += int(count)
        kind = "needs a person" if needs_person else "unowned"
        parts.append(f"{int(count)} {kind} (oldest {oldest} in tenant {tenant})")
    noun = "case" if total == 1 else "cases"
    return fail(
        f"{total} overdue {noun} {where}: {', '.join(parts)} — assign an owner, "
        "or answer the question"
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
