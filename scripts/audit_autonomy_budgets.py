#!/usr/bin/env python3
"""Audit existing crm_tasks.autonomy_budget values against the Phase-1 validator.

Read-only diagnostic. Connects to the configured PostgreSQL, walks every task
with a non-empty `autonomy_budget`, runs `robothor.engine.autonomy.validate_budget`
on each, and prints the violators.

Use this before promoting migration 067's CHECK constraint from `NOT VALID` to
validated. If the report is empty, run::

    ALTER TABLE crm_task_history VALIDATE CONSTRAINT crm_task_history_metadata_kind_check;

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
    """Return a list of {task_id, title, reason, budget} for every violator."""
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
            ok, reason = validate_budget(budget if isinstance(budget, dict) else {})
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
        scope = args.tenant or "all tenants"
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
