"""Budget stops: which budgets, caps and stops fired in the last day.

Every budget, cap or control that refuses, ends or degrades a run records a
``budget_stop`` step on it -- including a spend budget that only observed and
let the run continue. This check counts them, so an operator learns that a
limit was reached without opening each run. It is a reading, never a verdict:
``info`` severity, and a pass either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.doctor.model import Check, Result, info, skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from robothor.doctor.context import DoctorContext

__all__ = ["CHECKS"]

_SQL = """
    SELECT COALESCE(tool_name, 'unknown') AS stop,
           COALESCE(tool_input->>'mode', 'unknown') AS mode,
           count(*) AS n
      FROM agent_run_steps
     WHERE step_type = 'budget_stop'
       AND created_at >= now() - interval '24 hours'
     GROUP BY 1, 2
     ORDER BY 3 DESC, 1, 2
"""


async def _recent(ctx: DoctorContext) -> Result:
    """Budget stops recorded in the last 24 hours, by stop and mode.

    ``observe`` means the budget was reached and the run carried on; ``enforce``
    means it stopped or refused the run. Open the Runs view and filter the steps
    by ``budget_stop`` to see which runs, or read docs/BUDGETS_AND_STOPS.md for
    what each stop is and the setting that controls it. Nothing to fix here.
    """

    def _query() -> list[Any]:
        with ctx.db() as conn:
            cursor = conn.cursor()
            cursor.execute(_SQL)
            return list(cursor.fetchall())

    try:
        rows = await ctx.run_blocking(_query)
    except Exception as exc:  # noqa: BLE001 - unreachable is a skip, not a verdict
        return skip(f"could not read run steps: {type(exc).__name__}")
    total = sum(int(row[2]) for row in rows)
    if not total:
        return info("0 budget stops in the last 24 h")
    parts = ", ".join(f"{row[0]} ({row[1]}): {int(row[2])}" for row in rows)
    noun = "budget stop" if total == 1 else "budget stops"
    return info(f"{total} {noun} in the last 24 h -- {parts}")


CHECKS: tuple[Check, ...] = (
    Check(
        id="budget_stops.recent",
        title="Budget stops in the last 24 hours",
        category="budget_stops",
        severity="info",
        run=_recent,
    ),
)
