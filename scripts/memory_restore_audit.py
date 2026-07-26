#!/usr/bin/env python3
"""Restore facts that were consulted by a real run and then archived.

Of the inactive rows, the overwhelming majority carry superseded_by and are
explained by consolidation. A smaller cohort has no superseded_by, and the
current decay predicate matches zero rows — so it came from an older, looser
predicate. That cohort is NOT restored here: reactivating thousands of rows of
unknown provenance is a larger change than the one being fixed.

What is restored is the evidence-backed subset: inactive, no superseded_by, and
present in fact_access_log — an agent actually read it, then the system retired
it. Every restore writes a memory_facts_audit row so the action is reversible
and attributable.

Dry-run by default.
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from robothor.db.connection import get_connection  # noqa: E402

CANDIDATES = """
SELECT f.id, f.tenant_id, left(f.fact_text, 200) AS txt, count(l.*) AS reads
FROM memory_facts f
JOIN fact_access_log l ON l.fact_id = f.id
WHERE NOT f.is_active AND f.superseded_by IS NULL
GROUP BY f.id, f.tenant_id, f.fact_text
ORDER BY reads DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually restore (default: dry run)")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(CANDIDATES)
        rows = cur.fetchall()[: args.limit]

        print(f"evidence-backed wrongly-archived candidates: {len(rows)}")
        for fid, tenant, txt, reads in rows[:10]:
            print(f"  {fid:>8}  reads={reads:<3} {txt[:60]}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")

        if not args.apply:
            print("\ndry run — nothing changed. Re-run with --apply to restore.")
            return 0

        ids = [r[0] for r in rows]
        for fid, tenant, txt, reads in rows:
            cur.execute(
                "INSERT INTO memory_facts_audit (fact_id, tenant_id, fact_text, reason, snapshot) "
                "VALUES (%s,%s,%s,'restore:audit-2026-07',%s::jsonb)",
                (fid, tenant, txt, json.dumps({"reads": reads, "source": "restore_audit"})),
            )
        cur.execute(
            "UPDATE memory_facts SET is_active = TRUE, updated_at = NOW() WHERE id = ANY(%s)",
            (ids,),
        )
        restored = cur.rowcount
        conn.commit()
        print(f"\nrestored {restored} fact(s); manifest written with reason='restore:audit-2026-07'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
