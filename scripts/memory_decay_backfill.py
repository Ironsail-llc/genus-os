#!/usr/bin/env python3
"""Backfill decay shadow columns from the access log, in small transactions.

Writes ONLY to the *_shadow columns. access_count is read by facts._blend_rank,
so backfilling it in place would silently re-rank retrieval at the same moment
it changed decay; keeping them separate lets each be measured on its own.

Batched deliberately. A single UPDATE over the matching rows takes an exclusive
lock long enough to queue the engine's own inserts and supersedes behind it —
observed in practice, blocking live writes for minutes. Small committed batches
give the engine room between them.

Idempotent: re-running recomputes the same values. Safe to interrupt.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from robothor.db.connection import get_connection  # noqa: E402

BATCH_SQL = """
WITH agg AS (
    SELECT fact_id, count(*) AS n, max(accessed_at) AS last_at
    FROM fact_access_log
    GROUP BY fact_id
),
todo AS (
    SELECT f.id, agg.n, agg.last_at
    FROM memory_facts f
    JOIN agg ON f.id = agg.fact_id
    WHERE f.access_count_shadow IS DISTINCT FROM agg.n
    ORDER BY f.id
    LIMIT %s
    FOR UPDATE OF f SKIP LOCKED
)
UPDATE memory_facts f
SET access_count_shadow  = todo.n,
    last_accessed_shadow = GREATEST(f.created_at, todo.last_at)
FROM todo
WHERE f.id = todo.id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=250)
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between batches")
    ap.add_argument("--max-batches", type=int, default=200)
    args = ap.parse_args()

    total = 0
    for i in range(args.max_batches):
        with get_connection() as conn:
            cur = conn.cursor()
            # Never wait behind the engine; skip contended rows and catch them
            # on a later pass rather than holding a queue open.
            cur.execute("SET LOCAL lock_timeout = '3s'")
            try:
                cur.execute(BATCH_SQL, (args.batch,))
            except Exception as e:
                conn.rollback()
                print(f"batch {i}: contended, backing off ({e})")
                time.sleep(1.0)
                continue
            n = cur.rowcount
            conn.commit()
        total += n
        if n == 0:
            print(f"done: {total} rows backfilled")
            return 0
        print(f"batch {i}: {n} rows (total {total})", flush=True)
        time.sleep(args.pause)

    print(f"stopped at max-batches: {total} rows backfilled so far")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
