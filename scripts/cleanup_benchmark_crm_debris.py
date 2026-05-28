#!/usr/bin/env python3
"""Resolve CRM tasks created by benchmark sub-agent runs.

Until L1+L2 of the benchmark sandbox plan landed (2026-05-28), benchmark
sub-agents could call ``create_task`` against the real CRM, polluting the
queue with fictional items from the suite prompts ("Review LLC docs for
Bob", "$25k wire to FakeVendorCo", etc.). The real ``main`` heartbeat then
picked them up the next morning and acted on them, creating a feedback loop.

This script finds every still-open task whose row was created during a
``trigger_detail LIKE 'benchmark:%'`` run and soft-deletes it (matching the
existing semantics of ``robothor.crm.dal.delete_task``), so the queue
returns to a true reflection of operator work.

Default is ``--dry-run``; mutations require ``--apply``. Re-runnable —
if a future benchmark misconfiguration creates new debris it will be
picked up the same way.

Run::

    python scripts/cleanup_benchmark_crm_debris.py            # dry-run summary
    python scripts/cleanup_benchmark_crm_debris.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from robothor.crm.dal import delete_task
from robothor.db.connection import get_connection

_BENCH_CREATED_SQL = """
SELECT (s.tool_output->>'id')::uuid AS task_id,
       r.trigger_detail              AS trigger_detail,
       t.tenant_id                   AS tenant_id,
       t.status                      AS status,
       t.title                       AS title
  FROM agent_run_steps s
  JOIN agent_runs      r ON r.id = s.run_id
  JOIN crm_tasks       t ON t.id = (s.tool_output->>'id')::uuid
 WHERE s.tool_name      = 'create_task'
   AND s.tool_output ? 'id'
   AND r.trigger_detail LIKE 'benchmark:%'
   AND t.deleted_at     IS NULL
"""


def _fetch_debris() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(_BENCH_CREATED_SQL)
        for task_id, trigger_detail, tenant_id, status, title in cur.fetchall():
            rows.append(
                {
                    "task_id": str(task_id),
                    "trigger_detail": str(trigger_detail or ""),
                    "tenant_id": str(tenant_id or ""),
                    "status": str(status or ""),
                    "title": str(title or "")[:80],
                }
            )
    return rows


def _print_summary(rows: list[dict[str, str]]) -> None:
    by_trigger: Counter[str] = Counter(r["trigger_detail"] for r in rows)
    by_status: Counter[str] = Counter(r["status"] for r in rows)
    print(f"Found {len(rows)} undeleted task(s) created by benchmark runs.\n")
    if not rows:
        return
    print("By status:")
    for s, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {s}")
    print("\nBy benchmark trigger (top 15):")
    for tr, n in by_trigger.most_common(15):
        print(f"  {n:>4}  {tr}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually soft-delete the tasks. Without this, prints a dry-run summary.",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print the title of each affected task.",
    )
    args = ap.parse_args()

    rows = _fetch_debris()
    _print_summary(rows)

    if args.verbose:
        print("\nTasks:")
        for r in rows:
            print(f"  {r['task_id']}  [{r['status']:<11}]  {r['title']}")

    if not args.apply:
        print("\n(dry-run — pass --apply to soft-delete the tasks above)")
        return 0

    print(f"\nApplying: soft-deleting {len(rows)} task(s)...")
    deleted = 0
    failed = 0
    for r in rows:
        ok = delete_task(r["task_id"], tenant_id=r["tenant_id"])
        if ok:
            deleted += 1
        else:
            failed += 1
            print(f"  ! could not delete {r['task_id']} ({r['title']})")
    print(f"Deleted: {deleted}    Failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
