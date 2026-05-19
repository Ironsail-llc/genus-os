#!/usr/bin/env python3
"""Audit existing crm_tasks.autonomy_budget values against the Phase-1 validator.

Read-only diagnostic. Connects to the configured PostgreSQL, walks every task
with a non-empty `autonomy_budget`, runs `robothor.engine.autonomy.validate_budget`
on each, and prints the violators.

The validator is enforced at write time in `robothor.crm.dal` (not as a DB
constraint), so the failure mode this catches is: a legacy row whose budget
was written before Phase 1 will now be rejected the next time it is updated
via `create_task` / `update_task` / the bridge POST. Run this before flipping
the planner default on (Phase 2) and any time you tighten the validator —
a clean report means the planner won't start surfacing
``{"error": ...}`` responses against pre-existing data.

Not related to migration 067's ``crm_task_history.metadata->>'kind'`` CHECK
constraint — that one is on a *different* column, and a separate audit
(scan `metadata->>'kind'` against the enum in docs/TASK_HISTORY_KIND.md)
gates promoting it from NOT VALID to validated.

Run::

    python scripts/audit_autonomy_budgets.py
    python scripts/audit_autonomy_budgets.py --tenant default --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.engine.autonomy import validate_budget


def _scan(tenant_id: str | None) -> list[dict[str, Any]]:
    """Return a list of {task_id, title, reason, budget} for every violator.

    Non-dict autonomy_budget values are themselves a violation — the column
    is JSONB and the validator's contract is "object with these keys."
    A legacy row carrying a string, list, or scalar would previously have
    been silently coerced to ``{}`` and passed; now it surfaces with
    ``reason="not a dict: <typename>"``.
    """
    where = "WHERE autonomy_budget IS NOT NULL AND autonomy_budget <> '{}'::jsonb AND deleted_at IS NULL"
    params: list[Any] = []
    if tenant_id:
        where += " AND tenant_id = %s"
        params.append(tenant_id)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, title, tenant_id, autonomy_budget FROM crm_tasks {where} "
            f"ORDER BY created_at ASC",
            params,
        )
        violators: list[dict[str, Any]] = []
        for row in cur.fetchall():
            task_id, title, tid, budget = row[0], row[1], row[2], row[3]
            if not isinstance(budget, dict):
                violators.append(
                    {
                        "task_id": str(task_id),
                        "title": title,
                        "tenant_id": tid,
                        "reason": f"not a dict: {type(budget).__name__}",
                        "budget": budget,
                    }
                )
                continue
            ok, reason = validate_budget(budget)
            if not ok:
                violators.append(
                    {
                        "task_id": str(task_id),
                        "title": title,
                        "tenant_id": tid,
                        "reason": reason,
                        "budget": budget,
                    }
                )
    return violators


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tenant",
        default=None,
        help=f"Scope to a single tenant_id (default: all tenants; platform default is {DEFAULT_TENANT!r})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a human-readable report",
    )
    args = parser.parse_args()

    violators = _scan(args.tenant)

    if args.json:
        print(json.dumps(violators, indent=2, default=str))
        return 1 if violators else 0

    if not violators:
        scope = f"tenant {args.tenant!r}" if args.tenant else "all tenants"
        print(f"Clean — no autonomy_budget violations in {scope}.")
        return 0

    print(f"Found {len(violators)} task(s) with malformed autonomy_budget:")
    print()
    for v in violators:
        print(f"  task: {v['task_id']}  tenant: {v['tenant_id']}")
        print(f"  title:  {v['title']}")
        print(f"  reason: {v['reason']}")
        print(f"  budget: {json.dumps(v['budget'], default=str)}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
